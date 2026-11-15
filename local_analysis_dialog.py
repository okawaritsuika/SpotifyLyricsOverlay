"""Register a local recording and review the saved analysis from the overlay."""
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from app_paths import runtime_dir, worker_command, worker_environment
from audio_job import ANALYSIS_LOCK
from audio_sync import merge_candidates
from japanese_alignment import CTC, HYBRID, LEGACY
from local_analysis import AnalysisStore, alignment_quality, automatic_alignment_engine
from timing_quality import timing_origins

ENGINES = {'stable-ts': LEGACY, 'RomajiASR': CTC, 'RomajiASR + pyshiro': HYBRID}
ENGINE_NAMES = {**{engine: name for name, engine in ENGINES.items()}, 'supplied': '입력 싱크'}
QUALITY_NAMES = {'high': '높음', 'medium': '보통', 'low': '낮음'}
REASON_NAMES = {'reading_unavailable': '독음 준비 실패',
                'unsupported_model_phones': '모델 미지원 음소',
                'ambiguous_ctc_placement': '반복 구절 후보 충돌',
                'outside_recorded_or_allowed_range': '허용 시각 범위 밖',
                'ctc_pyshiro_disagreement': 'CTC와 pyshiro 결과 불일치'}
STATES = {'queued': '대기', 'running': '분석 중', 'failed': '실패', 'done': '분석 완료'}
MODELS = {'ECAPA': 'ecapa', 'Sony BYOL': 'sony', 'Sony Contrastive': 'sony-contrastive'}
SEPARATORS = {'없음': None, 'UNMIXX': 'unmixx', 'Singer-Informed': 'singer-informed'}


def voice_row(job, index):
    return next((row for row in job['result'].get('voice', {}).get('rows', []) if row['index'] == index), {})


def overlap_text(overlap):
    return {'no_vocal': '보컬 없음', 'solo': '1명 추정', 'overlap': '2명 이상 추정',
            'uncertain': '인원수 미확정', 'varies': '구간별 다름'}.get(overlap.get('status'), '')


def singer_text(job, index, known=()):
    if known:
        return ' + '.join(known)
    voice = job['result'].get('voice', {})
    names = voice.get('names', {})
    manual = job.get('singer_reviews', {}).get(str(index))
    if manual is not None:
        return '사용자 확정 · ' + (' + '.join(names.get(item, item) for item in manual) or '무가창')
    if job['spec'].get('kind') != 'group' or job['status'] != 'done':
        return ''
    row = voice_row(job, index)
    singers = ' + '.join(names.get(item, item) for item in row.get('singers', []))
    if not singers:
        return ''
    label = '가창 추정' if row.get('status') == 'estimated' else '일부 가창 추정'
    return f'{label} · {singers}'


def protected_candidates(times, effective, protected, bounds, duration):
    """Keep existing source rows and their recording-clock bounds."""
    anchors = list(bounds)
    anchors.extend((index, time) for index, time in enumerate(effective) if protected[index] and time is not None)
    return [stamp if stamp is not None and not protected[index] and 0 <= stamp < duration
            and all(stamp > value if row < index else stamp < value for row, value in anchors)
            else None for index, stamp in enumerate(times)]


def time_text(value):
    return '' if value is None else f'{value:.3f}'


def diagnostic_onset(alignment, index):
    times = alignment.get('times', [])
    details = alignment.get('line_alignment', [])
    stamp = times[index] if index < len(times) else None
    if stamp is not None:
        return stamp
    if index < len(details):
        detail = details[index]
        return detail.get('start') if detail.get('start') is not None else detail.get('ctc_start')


class LocalAnalysisDialog:
    def __init__(self, app, base):
        self.app, self.base = app, base
        self.store = AnalysisStore(app.store.directory)
        self.song = app.song
        self.playback = app.playback
        self.lines = [line.original for line in self.song.lines]
        self.replies = queue.Queue()
        self.cancelled = threading.Event()
        self.worker = None
        self.preview = None
        self.last_update = None
        self.last_timing = None
        self.window = tk.Toplevel(app.root)
        self.window.title('로컬 음원 분석 · ' + self.playback.title)
        self.window.geometry('1140x640')
        self.window.protocol('WM_DELETE_WINDOW', self.hide)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill='both', expand=True)
        config = ttk.Frame(frame)
        config.pack(fill='x', pady=(0, 8))
        ttk.Label(config, text='정렬 엔진').pack(side='left')
        auto = f'자동 · {ENGINE_NAMES[automatic_alignment_engine(self.lines)]}'
        self.engines = {auto: 'auto', **ENGINES}
        preferred = app.settings.get('local_alignment_engine', (app.settings.get('audio_alignment') or {}).get('engine'))
        self.engine = tk.StringVar(value=ENGINE_NAMES.get(preferred, auto))
        selector = ttk.Combobox(config, textvariable=self.engine, values=tuple(self.engines),
                                state='readonly', width=23)
        selector.pack(side='left', padx=(6, 18))
        selector.bind('<<ComboboxSelected>>', lambda _: app.safe(self.save_engine))
        self.runtime_status = tk.StringVar(value='모델 및 GPU 확인 중…')
        ttk.Label(config, textvariable=self.runtime_status).pack(side='left')
        self.ctc_recovery = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text='CTC 누락 복구', variable=self.ctc_recovery).pack(side='right', padx=8)
        self.voice_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(config, text='가창 분석 옵션', variable=self.voice_enabled,
                        command=self.toggle_voice).pack(side='right')
        voice_config = self.voice_config = ttk.Frame(frame)
        identities = ttk.Frame(voice_config)
        identities.pack(fill='x', pady=(0, 8))
        ttk.Label(identities, text='가창 분석용 캐릭터 ID (쉼표 구분)').pack(side='left')
        self.ids = ttk.Entry(identities)
        self.ids.pack(side='left', fill='x', expand=True, padx=(6, 0))
        from artist_names import name_key, split_artist_affiliation
        from group_credits import RESOURCE_PATH, _catalog
        catalog = _catalog(RESOURCE_PATH)
        if catalog:
            credits = [catalog.idol_names.get(name_key(re.sub(r'\s*[（(]CV\b.*$', '',
                                                             split_artist_affiliation(artist)[0], flags=re.I)))
                       for artist in app.displayed_artists()]
            if len(credits) > 1 and all(credits):
                self.ids.insert(0, ', '.join(dict.fromkeys(credits)))
        voice_options = ttk.Frame(voice_config)
        voice_options.pack(fill='x', pady=(0, 8))
        voice_settings = app.settings.get('character_voice') or {}
        ttk.Label(voice_options, text='가창 모델').pack(side='left')
        self.voice_model = tk.StringVar(value=next((label for label, model in MODELS.items()
                                                   if model == voice_settings.get('singer_model')), 'ECAPA'))
        ttk.Combobox(voice_options, textvariable=self.voice_model, values=tuple(MODELS), state='readonly',
                     width=19).pack(side='left', padx=(6, 18))
        ttk.Label(voice_options, text='복수 가창 분리').pack(side='left')
        self.separator = tk.StringVar(value=next((label for label, separator in SEPARATORS.items()
                                                  if separator == voice_settings.get('separator')), '없음'))
        ttk.Combobox(voice_options, textvariable=self.separator, values=tuple(SEPARATORS), state='readonly',
                     width=19).pack(side='left', padx=6)
        self.overlap_experiment = tk.BooleanVar(value=voice_settings.get('strategy') == 'overlap-assignment')
        ttk.Checkbutton(voice_options, text='인원수·스템 할당 실험 (UNMIXX)',
                        variable=self.overlap_experiment).pack(side='left', padx=10)
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(0, 8))
        self.submit = ttk.Button(actions, text='분석 권한이 있는 음원 선택…', command=lambda: app.safe(self.choose))
        self.submit.pack(side='left')
        self.retry = ttk.Button(actions, text='이어서 분석', command=lambda: app.safe(self.resume))
        self.retry.pack(side='left', padx=6)
        self.rescore = ttk.Button(actions, text='미확정 싱크 재분석',
                                  command=lambda: app.safe(lambda: self.launch(review_alignment=True)))
        self.rescore.pack(side='left')
        self.cancel = ttk.Button(actions, text='분석 취소', command=self.cancelled.set)
        self.cancel.pack(side='left', padx=6)
        self.only_uncertain = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text='미확정 줄만 보기', variable=self.only_uncertain,
                        command=lambda: self.refresh(update_status=False)).pack(side='right')
        self.status = tk.StringVar(value='이 녹음에 연결된 로컬 음원을 선택하세요.')
        ttk.Label(frame, textvariable=self.status, wraplength=1090).pack(fill='x', pady=(0, 8))
        body = ttk.Frame(frame)
        body.pack(fill='both', expand=True)
        self.rows = ttk.Treeview(body, columns=('lyric', 'saved', 'candidate', 'delta', 'review',
                                                'quality', 'count', 'singer'), show='headings')
        for column, label, width in (('lyric', '가사 원문', 236), ('saved', '기존 싱크 (초)', 146),
                                     ('candidate', '초기 후보 (초)', 94), ('delta', '기존과 차이', 88),
                                     ('review', '재분석 (초)', 83), ('quality', '품질 / 상태', 125),
                                     ('count', '가창 인원', 90), ('singer', '가창 파트', 162)):
            self.rows.heading(column, text=label)
            self.rows.column(column, width=width, stretch=column in ('lyric', 'singer'))
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.rows.yview)
        self.rows.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.rows.pack(fill='both', expand=True)
        review = ttk.Frame(frame)
        review.pack(fill='x', pady=(8, 0))
        for text, kind in (('기존 듣기', 'existing'), ('초기 후보 듣기', 'alignment'),
                           ('재분석 후보 듣기', 'alignment_review')):
            ttk.Button(review, text=text, command=lambda kind=kind:
                       app.safe(lambda: self.preview_audio(kind))).pack(side='left', padx=(0, 4))
        for text, kind, all_rows in (('전체 초기 후보 저장', 'alignment', True),
                                     ('선택한 재분석 후보 저장', 'alignment_review', False),
                                     ('선택한 초기 후보 저장', 'alignment', False)):
            ttk.Button(review, text=text, command=lambda kind=kind, all_rows=all_rows:
                       app.safe(lambda: self.apply(kind, all_rows=all_rows))).pack(side='right', padx=(4, 0))
        ttk.Button(voice_options, text='가창자 수정·확정', command=lambda: app.safe(self.review)).pack(side='left', padx=4)
        ttk.Button(voice_options, text='확정 지우기', command=lambda: app.safe(lambda: self.review(clear=True))).pack(side='left')
        ttk.Button(voice_options, text='가창 미확정 재분석',
                   command=lambda: app.safe(lambda: self.launch(refresh=True))).pack(side='left', padx=4)
        self.evidence = tk.StringVar()
        ttk.Label(frame, textvariable=self.evidence, wraplength=1090).pack(fill='x', pady=(6, 0))
        self.rows.bind('<<TreeviewSelect>>', lambda _: self.show_evidence())
        self.job = app.current_local_analysis()
        self.refresh()
        threading.Thread(target=self.probe, name='local-audio-runtime-probe', daemon=True).start()
        self.poll()

    def toggle_voice(self):
        if self.voice_enabled.get():
            self.voice_config.pack(fill='x', before=self.submit.master)
        else:
            self.voice_config.pack_forget()

    def save_engine(self):
        self.app.settings['local_alignment_engine'] = self.engines[self.engine.get()]
        self.app.save_settings()

    def worker_args(self):
        command = worker_command(self.base, 'local_analysis_worker', optional=True)
        command += ['--data-dir', str(self.store.directory.resolve()), '--runtime-dir', str(runtime_dir())]
        return command

    def worker_options(self):
        return {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT,
                'encoding': 'utf-8', 'errors': 'replace', 'env': worker_environment(self.base, prefer_inherited=True),
                'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}

    def probe(self):
        try:
            reply = subprocess.run(self.worker_args() + ['--probe-alignment'], timeout=25, **self.worker_options())
            if reply.returncode:
                raise ValueError('선택 오디오 런타임을 확인하지 못했습니다.')
            data = json.loads(reply.stdout.strip().splitlines()[-1])
            gpu = ', '.join(data['romaji_gpu']) or 'CPU'
            romaji = f'RomajiASR 준비 ({gpu})' if data['romaji'] else 'RomajiASR 미설치'
            stable = f"stable-ts 준비 ({'CUDA 감지' if data['stable_gpu'] else 'CPU'})" if data['stable'] else 'stable-ts 미설치'
            self.replies.put(('runtime', f'{romaji} · {stable}'))
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as error:
            self.replies.put(('runtime', str(error)))

    def choose(self, path=None):
        path = path or filedialog.askopenfilename(parent=self.window, title='현재 재생과 같은 버전의 로컬 음원 선택')
        if not path:
            return
        participants = [item.strip() for item in self.ids.get().split(',') if item.strip()] if self.voice_enabled.get() else []
        spec = {'kind': 'group' if participants else 'lyrics',
                'title': self.playback.title, 'artist': self.playback.artist, 'album': self.playback.album,
                'song_id': self.song.id, 'recording': self.playback.key,
                'expected_duration': self.playback.duration, 'lines': self.lines,
                'line_readings': [line.reading or None for line in self.song.lines],
                'alignment': {'engine': self.engines[self.engine.get()]}}
        if self.ctc_recovery.get() and spec['alignment']['engine'] in ('auto', CTC):
            spec['alignment']['profile'] = 'recovery'
        if participants:
            spec['participants'] = participants
            spec['recompute_alignment'] = True
            spec['voice'] = {'singer_model': MODELS[self.voice_model.get()],
                             'separator': SEPARATORS[self.separator.get()], 'strategy': None}
            if self.overlap_experiment.get():
                spec['voice'].update(strategy='overlap-assignment',
                                     verify_targets=spec['voice']['separator'] == 'singer-informed')
            if self.current_recording():
                times = self.app.direct_times()
                if not any(value is not None for value in times):
                    offset = self.app.timing.get('offset', 0)
                    times = [round(value + offset, 3) if value is not None and
                             0 <= value + offset < self.playback.duration else None
                             for value in self.app.timing['times']]
                if any(value is not None for value in times):
                    spec['times'] = list(times)
        self.launch(path, spec)

    def current_recording(self):
        app = self.app
        return (app.song and app.is_bound() and app.playback.key == self.playback.key
                and abs(app.playback.duration - self.playback.duration) <= 2
                and [line.original for line in app.song.lines] == self.lines)

    def resume(self):
        if self.job and self.job['status'] != 'done':
            self.launch()

    def launch(self, path=None, spec=None, *, refresh=False, review_alignment=False):
        if self.worker:
            return
        command = self.worker_args()
        child_options = self.worker_options()
        job_id = self.job['id'] if self.job else None
        if not path and job_id is None:
            raise ValueError('먼저 로컬 음원을 분석하세요.')
        if refresh and self.job['spec']['kind'] != 'group':
            raise ValueError('저장된 그룹 가창 분석이 없습니다.')
        self.cancelled.clear()
        self.status.set('미확정 싱크 재분석 중…' if review_alignment else '등록 및 분석 대기 중…')
        review_engine = self.engines[self.engine.get()] if review_alignment else 'auto'
        review_recovery = review_alignment and self.ctc_recovery.get() and review_engine in ('auto', CTC)
        self.submit.state(['disabled'])
        self.retry.state(['disabled'])

        def run():
            try:
                current = self.store.register(Path(path), spec) if path else job_id
                self.replies.put(('job', current))
                while not self.cancelled.is_set():
                    if ANALYSIS_LOCK.acquire(timeout=.2):
                        break
                else:
                    return
                try:
                    if self.cancelled.is_set():
                        return
                    options = ['--refresh-voice', '--uncertain-only'] if refresh else []
                    if review_alignment:
                        options.append('--review-alignment')
                        if review_engine != 'auto':
                            options.extend(['--review-engine', review_engine])
                        if review_recovery:
                            options.append('--recover-missing')
                    with subprocess.Popen(command + ['--job', current, '--retry', '--wait-for-queue', *options],
                                          **child_options) as child:
                        last = ''
                        while True:
                            try:
                                last, _ = child.communicate(timeout=.25)
                                break
                            except subprocess.TimeoutExpired:
                                if self.cancelled.is_set():
                                    child.terminate()
                                    child.communicate()
                                    return
                        if child.returncode:
                            try:
                                error = json.loads(last.strip().splitlines()[-1]).get('error')
                            except (ValueError, IndexError):
                                error = None
                            self.replies.put(('error', error or f'분석 프로세스가 종료되었습니다 ({child.returncode}).'))
                finally:
                    ANALYSIS_LOCK.release()
            except (OSError, ValueError) as error:
                self.replies.put(('error', str(error)))
            finally:
                self.replies.put(('finished', self.cancelled.is_set()))

        self.worker = threading.Thread(target=run, name='local-audio-analysis', daemon=True)
        self.worker.start()

    def poll(self):
        error = None
        while True:
            try:
                action, value = self.replies.get_nowait()
            except queue.Empty:
                break
            if action == 'job':
                self.job = self.store.get(value)
                self.last_update = None
            elif action == 'error':
                error = value
            elif action == 'runtime':
                self.runtime_status.set(value)
            else:
                self.worker = None
                if value:
                    error = '분석을 취소했습니다.'
        if self.job:
            self.job = self.store.get(self.job['id'])
            if self.job and self.last_update != self.job['updated']:
                self.refresh()
        if self.last_timing != self.timing_signature():
            self.refresh(update_status=False)
        if self.preview and self.preview.poll() is not None:
            output, _ = self.preview.communicate()
            if self.preview.returncode:
                try:
                    error = json.loads(output.strip().splitlines()[-1]).get('error')
                except (ValueError, IndexError):
                    error = '선택한 오디오 구간을 재생하지 못했습니다.'
            self.preview = None
        if error:
            self.status.set(error)
        self.submit.state(['disabled' if self.worker else '!disabled'])
        self.retry.state(['!disabled' if self.job and not self.worker and self.job['status'] != 'done' else 'disabled'])
        alignment = (self.job or {}).get('result', {}).get('alignment', {})
        self.rescore.state(['!disabled' if alignment.get('times') and alignment.get('engine') != 'supplied'
                            and not self.worker else 'disabled'])
        self.cancel.state(['!disabled' if self.worker and not self.cancelled.is_set() else 'disabled'])
        self.window.after(350, self.poll)

    def existing_sync(self):
        if not self.current_recording():
            return [None] * len(self.lines), [''] * len(self.lines), ()
        app = self.app
        times, _ = app.effective_times()
        origins = timing_origins(app.timing)
        offset = app.timing.get('offset', 0)
        bounds = app.direct_map_cache[5] if any(value is not None for value in app.direct_times()) else ()
        times = [round(value + offset, 3) if value is not None and source not in ('추정', '자동 대기')
                 else None for value, source in zip(times, app.time_sources)]
        sources = ['수동' if origin == 'manual' and app.timing.get('source') != 'LRCLIB' else source
                   for origin, source in zip(origins, app.time_sources)]
        return times, sources, bounds

    def timing_signature(self):
        return (self.current_recording(), self.app.timing, self.app.time_cache)

    def refresh(self, *, update_status=True):
        job = self.job or {'status': 'queued', 'result': {}, 'spec': {}, 'path': '', 'error': None}
        self.last_update = job.get('updated')
        result = job['result']
        alignment = result.get('alignment', {})
        times = alignment.get('times', [])
        details = alignment.get('line_alignment', [])
        review = result.get('alignment_review', {})
        review_times = review.get('times', [])
        status = [STATES[job['status']], Path(job['path']).name]
        if alignment:
            status.append(f"싱크 후보 {alignment['matched']}/{len(self.lines)}줄")
            actual = ENGINE_NAMES.get(alignment.get('engine'), alignment.get('engine'))
            requested = ENGINE_NAMES.get(alignment.get('requested_engine'), alignment.get('requested_engine'))
            device = alignment.get('runtime', {}).get('device')
            if actual:
                status.append(f"실행 {actual}" + (f" · {device.upper()}" if device else ''))
            if requested:
                requested_device = alignment.get('requested_device')
                status.append(f"요청 {requested}" + (f" · {requested_device.upper()}" if requested_device else ''))
            if alignment.get('runtime', {}).get('fallback_reason'):
                status.append('장치 전환 · ' + alignment['runtime']['fallback_reason'])
        if alignment.get('fallback_reason'):
            status.append(alignment['fallback_reason'])
        if alignment.get('refinement_warning'):
            status.append(alignment['refinement_warning'])
        if alignment.get('alignment_error'):
            status.append('기존 싱크로 가창 분석 · ' + alignment['alignment_error'])
        if alignment.get('alignment_warning'):
            status.append(alignment['alignment_warning'])
        if review:
            status.append(f"재분석 후보 {review['matched']}/{len(review['attempted'])}줄")
            ran = ', '.join(dict.fromkeys(ENGINE_NAMES.get(info['engine'], info['engine'])
                                          for info in review.get('engines', [])))
            if ran:
                status.append(f'재분석 실행 {ran}')
            warning = next((info['fallback_reason'] for info in review.get('engines', [])
                            if info.get('fallback_reason')), '')
            if warning:
                status.append(warning)
        clock_reason = result.get('playlist_clock', {}).get('reason')
        if clock_reason == 'recording_tags_unverified':
            status.append('재생목록 녹음 버전 미확인')
        elif clock_reason == 'playlist_clock_mismatch':
            status.append('재생목록 음원과 기존 재생 시각 불일치')
        missing = result.get('voice', {}).get('missing_profiles', [])
        if missing:
            status.append('가창 프로필 미확보 ' + ', '.join(missing))
        if result.get('voice', {}).get('strategy') == 'overlap-assignment' and not result['voice'].get('calibration_sha256'):
            status.append('인원수·신원 보정 자료 미확보')
        separation_error = next((segment['separation_error'] for row in result.get('voice', {}).get('rows', [])
                                 for segment in row.get('subsegments', []) if segment.get('separation_error')), None)
        if separation_error:
            status.append(separation_error)
        if job['error']:
            status.append(job['error'])
        if update_status and self.job:
            self.status.set(' · '.join(status))
        selected = self.rows.selection()
        self.rows.delete(*self.rows.get_children())
        labels = {'candidate': '검토 후보', 'confirmed': '모델 간 일치', 'conflict': '충돌',
                  'missing': '매칭 실패', 'retained': '기존 싱크'}
        current = self.current_recording()
        existing, sources, _ = self.existing_sync()
        self.last_timing = self.timing_signature()
        for index, line in enumerate(self.lines):
            stamp = times[index] if index < len(times) else None
            detail = details[index] if index < len(details) else {}
            quality = alignment_quality(detail, stamp)
            if self.only_uncertain.get() and quality != 'low':
                continue
            state = labels.get(detail.get('status'), '')
            if times and detail.get('status') != 'retained':
                state = ' · '.join(filter(None, (QUALITY_NAMES.get(quality), state)))
            known = self.app.current_line_singers(index) if current else ()
            singers = singer_text(job, index, known)
            row = voice_row(job, index)
            if not singers and job['status'] == 'done' and job['spec']['kind'] == 'group':
                singers = '미확정' if row.get('ranked') else '판정 불가'
            saved = existing[index]
            saved_label = f'{saved:.3f} · {sources[index]}' if saved is not None else ''
            proposed = diagnostic_onset(alignment, index)
            difference = f'{proposed - saved:+.3f}초' if proposed is not None and saved is not None else ''
            refined = review_times[index] if index < len(review_times) else None
            self.rows.insert('', 'end', iid=str(index),
                             values=(line, saved_label, time_text(proposed), difference, time_text(refined), state,
                                     overlap_text(row.get('overlap', {})), singers))
        self.rows.selection_set([item for item in selected if self.rows.exists(item)])
        self.show_evidence()

    def show_evidence(self):
        selection = self.rows.selection()
        if len(selection) != 1 or not self.job:
            self.evidence.set('')
            return
        index = int(selection[0])
        row = voice_row(self.job, index)
        result = self.job['result']
        voice = result.get('voice', {})
        names = voice.get('names', {})
        labels = {'estimated': '참여 추정', 'present': '참여 추정', 'below_threshold': '미참여 추정',
                  'absent': '미참여 추정', 'uncertain': '미확정'}
        count = overlap_text(row.get('overlap', {}))
        details = [count] if count else []
        for key, label in (('alignment', '초기'), ('alignment_review', '재분석')):
            alignment = result.get(key, {})
            lines = alignment.get('line_alignment', [])
            if index < len(lines) and lines[index]:
                line = lines[index]
                engine = ENGINE_NAMES.get(line.get('engine') or alignment.get('engine'))
                reason = line.get('reason')
                reading = f"독음 {line['reading']}" if line.get('reading') else ''
                recovered = '누락 구절 복구' if line.get('recovered') else ''
                text = ' · '.join(filter(None, (engine, recovered, REASON_NAMES.get(reason, reason), reading)))
                if text:
                    details.append(f'{label} {text}')
        for candidate, score in row.get('scores', {}).items():
            state = labels.get(row.get('decisions', {}).get(candidate), '미확정')
            value = f'{score:.3f}' if score is not None else '점수 없음'
            details.append(f'{names.get(candidate, candidate)} {value} ({state})')
        if not count and row.get('estimated_count') is not None:
            details.append(f"추정 {row['estimated_count']}명")
        adjacent = row.get('adjacent_evidence', {}).get('scores', {})
        if any(score is not None for score in adjacent.values()):
            ranked = sorted(((score, names.get(candidate, candidate)) for candidate, score in adjacent.items()
                             if score is not None), reverse=True)
            details.append('인접 구간 참고 ' + ', '.join(f'{name} {score:.3f}' for score, name in ranked))
        for segment in row.get('subsegments', []):
            singers = ' + '.join(names.get(item, item) for item in segment.get('singers', []))
            identity = singers or overlap_text(segment.get('overlap', {}))
            if identity and len(row['subsegments']) > 1:
                details.append(f"{segment['start_ms']/1000:.2f}–{segment['end_ms']/1000:.2f}초 {identity}")
        assignment = next((segment['assignment'] for segment in row.get('subsegments', [])
                           if segment.get('assignment')), None)
        if assignment:
            details.append('스템 할당 후보 ' + ' / '.join(names.get(name, name) for name in assignment['stems']))
        separation = next((segment['separation'] for segment in row.get('subsegments', [])
                           if segment.get('separation')), {})
        for number, stem in enumerate(separation.get('stems', []), 1):
            ranked = stem.get('ranked', [])
            label = names.get(ranked[0], ranked[0]) if ranked else '식별 불가'
            details.append(f'분리 {number} {label}')
        for candidate, target in separation.get('targets', {}).items():
            score = target['target_similarity']
            if score is not None:
                details.append(f"{names.get(candidate, candidate)} 분리 {score:.3f}, 잔여 변화 {target['residual_gain']:.2f}")
        self.evidence.set(' · '.join(details))

    def review(self, clear=False):
        selection = self.rows.selection()
        if len(selection) != 1 or not self.job or self.job['spec']['kind'] != 'group':
            raise ValueError('가창자를 확인할 가사 줄 하나를 선택하세요.')
        index = int(selection[0])
        if self.current_recording() and self.app.current_line_singers(index):
            raise ValueError('이 줄은 기존 수동 파트 표의 가창자를 사용합니다.')
        singers = None
        if not clear:
            initial = self.job.get('singer_reviews', {}).get(str(index), voice_row(self.job, index).get('singers', []))
            choices = ', '.join(self.job['spec']['participants'])
            answer = simpledialog.askstring('가창자 확정', f'캐릭터 ID를 쉼표로 구분하세요. 빈 값은 무가창입니다.\n{choices}',
                                            parent=self.window, initialvalue=', '.join(initial))
            if answer is None:
                return
            singers = [item.strip() for item in answer.split(',') if item.strip()]
        self.store.review_singers(self.job['id'], index, singers)

    def preview_audio(self, kind):
        selection = self.rows.selection()
        if len(selection) != 1 or not self.job:
            raise ValueError('미리 들을 가사 줄 하나를 선택하세요.')
        index = int(selection[0])
        result = self.job['result']
        stamp = self.existing_sync()[0][index] if kind == 'existing' else diagnostic_onset(result.get(kind, {}), index)
        if stamp is None:
            raise ValueError('선택한 줄에 재생할 시작 시각이 없습니다.')
        start = max(0, stamp - 1.5)
        end = min(result.get('duration', self.playback.duration), stamp + 3)
        if end <= start:
            raise ValueError('선택한 시각이 로컬 파일 범위를 벗어났습니다.')
        self.stop_preview()
        self.preview = subprocess.Popen(self.worker_args() + ['--preview', self.job['id'], str(start), str(end)],
                                        **self.worker_options())

    def stop_preview(self):
        if self.preview:
            if self.preview.poll() is None:
                self.preview.terminate()
            self.preview.communicate()
            self.preview = None

    def apply(self, kind='alignment', *, all_rows=False):
        if not self.current_recording():
            raise ValueError('현재 재생과 가사가 분석 당시와 다릅니다.')
        result = (self.job or {}).get('result', {})
        clock = result.get('playlist_clock', {})
        if clock.get('reason') == 'playlist_clock_mismatch':
            raise ValueError('현재 재생과 시각이 일치하는 로컬 음원을 다시 선택하세요.')
        if all_rows and clock.get('reason') == 'recording_tags_unverified':
            raise ValueError('녹음 버전이 미확인입니다. 각 구절을 들어 보고 후보를 선택하세요.')
        alignment = result.get(kind, {})
        times = alignment.get('times', []) if alignment.get('engine') != 'supplied' else []
        if all_rows:
            details = alignment.get('line_alignment', [])
            selected = {index for index, stamp in enumerate(times)
                        if alignment_quality(details[index] if index < len(details) else {}, stamp) != 'low'}
        else:
            selected = {int(item) for item in self.rows.selection()}
        if not times or not selected:
            raise ValueError('들어 보고 확인한 싱크 후보를 선택하세요.')
        app = self.app
        candidates = [stamp if index in selected else None for index, stamp in enumerate(times)]
        existing, sources, bounds = self.existing_sync()
        offset = app.timing.get('offset', 0)
        # A playlist candidate already displayed at the same time may be saved
        # without changing that playback's effective sync.
        protected = [value is not None and (source != 'MP3 음원 싱크' or value != candidate)
                     for value, source, candidate in zip(existing, sources, candidates)]
        candidates = protected_candidates(candidates, existing, protected,
                                          [(index, value + offset) for index, value in bounds],
                                          self.playback.duration)
        updated, added = merge_candidates(app.timing, candidates)
        if added:
            app.save_current_timing(updated)
            app.timing = updated
            app.render()
            if app.editor:
                app.editor_refresh()
            self.refresh()
        self.status.set(f'빈 구절 {added}개에 적용했습니다.')

    def hide(self):
        self.stop_preview()
        self.window.withdraw()

    def close(self):
        self.cancelled.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.stop_preview()
        self.window.destroy()
