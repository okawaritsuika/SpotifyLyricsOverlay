"""Review and manage saved sync reanalysis from the overlay."""
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk


TITLE = '싱크 일괄 재분석 관리'
MODES = {
    '전체 재분석': 'all',
    '자동 생성 싱크만 재분석': 'automatic',
    '미확정·누락된 가사만 재분석': 'incomplete',
    '선택한 곡만 재분석': 'selected',
    '구버전 엔진으로 분석된 곡만 재분석': 'old',
}
ENGINES = {'검증된 기본 엔진': 'default', 'RomajiASR + pyshiro 실험': 'experimental'}
ENGINE_NAMES = {
    'stable_ts_legacy': 'stable-ts', 'romaji_ctc': 'RomajiASR CTC',
    'hybrid_japanese': 'RomajiASR + pyshiro', 'supplied': '입력 싱크',
}
STATES = {'idle': '대기', 'queued': '대기열', 'retry': '재시도 대기', 'running': '분석 중', 'paused': '일시정지',
          'cancelled': '취소됨', 'finished': '분석 종료', 'done': '비교 가능', 'failed': '실패',
          'skipped': '건너뜀', 'applied': '적용됨', 'discarded': '후보 폐기', 'restored': '복원됨'}
QUALITIES = {'high': '높음', 'medium': '보통', 'low': '낮음'}
AUDIO_KINDS = {'local': '로컬 음원', 'capture': '보관 녹음', 'missing': '음원 확보 필요'}
ROW_STATES = {'missing': '미확정', 'retained': '기존 유지', 'conflict': '후보 충돌', 'candidate': '신규 후보'}
METRICS = (('timed', '싱크가 있는 가사'), ('missing', '시작 시각 누락'), ('quality', '정렬 품질'),
           ('reversals', '가사 순서 오류'), ('estimated', '추정·보간된 가사'),
           ('seconds', '분석 시간'), ('engine', '사용 엔진'))
CATALOG_COLUMNS = (('title', '곡 제목', 175), ('artist', '아티스트', 120),
                   ('identifier', 'Spotify / 로컬 ID', 160), ('path', '연결된 음원', 180),
                   ('engine', '기존 엔진', 115), ('updated', '기존 분석 시각', 135),
                   ('timed', '기존 완성도', 95), ('eligible', '분석 가능 여부', 120),
                   ('manual', '수동 싱크', 85), ('status', '상태', 90))
ROW_COLUMNS = (('number', '줄', 45), ('text', '가사 원문', 235), ('before', '기존 (초)', 85),
               ('after', '신규 (초)', 85), ('delta', '차이 (초)', 85),
               ('status', '후보 상태', 118), ('quality', '품질', 70))


def time_text(value, *, signed=False):
    if value is None:
        return '미확정'
    return f'{value:+.3f}' if signed else f'{value:.3f}'


def date_text(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M')
    if value:
        return str(value).replace('T', ' ')[:16]
    return '—'


def metric_text(key, value):
    if value is None or value == '':
        return '—'
    if key == 'seconds' and isinstance(value, (int, float)):
        return f'{value:.2f}초'
    if key == 'engine':
        return ENGINE_NAMES.get(value, value)
    if key == 'quality':
        if isinstance(value, dict):
            return ' · '.join(f'{label} {value.get(grade, 0)}' for grade, label in QUALITIES.items())
        return QUALITIES.get(value, str(value))
    return str(value)


class SyncReanalysisDialog:
    def __init__(self, app, service):
        self.app, self.service = app, service
        self.items = {}
        self.snapshot = None
        self.result = None
        self.result_id = None
        self.poll_id = None
        window = self.window = tk.Toplevel(app.root)
        window.title(TITLE)
        width = min(1400, max(800, window.winfo_screenwidth() - 100))
        height = min(840, max(580, window.winfo_screenheight() - 120))
        window.geometry(f'{width}x{height}')
        window.minsize(800, 580)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.bind('<Destroy>', self.destroyed)

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill='both', expand=True)
        config = ttk.Frame(frame)
        config.pack(fill='x', pady=(0, 6))
        self.mode = self.choice(config, '재분석 범위', MODES, 29)
        self.mode.bind('<<ComboboxSelected>>', self.update_controls)
        self.engine = self.choice(config, '정렬 엔진', ENGINES, 23)
        self.force = tk.BooleanVar(window, value=False)
        ttk.Checkbutton(config, text='강제 재분석', variable=self.force).pack(side='left')

        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        self.start_button = self.button(actions, '재분석 시작', self.start)
        self.pause_button = self.button(actions, '일시정지', self.service.pause)
        self.resume_button = self.button(actions, '이어서 실행', self.service.resume)
        self.cancel_button = self.button(actions, '취소', self.service.cancel)
        self.retry_button = self.button(actions, '실패한 곡 재시도', self.service.retry_failed)
        self.refresh_button = self.button(actions, '목록 새로고침', self.refresh_inventory)
        self.scope = tk.StringVar(window)
        ttk.Label(frame, textvariable=self.scope).pack(anchor='w', pady=(5, 2))
        self.progress = ttk.Progressbar(frame, mode='determinate')
        self.progress.pack(fill='x', pady=(0, 4))
        self.progress_text = tk.StringVar(window)
        ttk.Label(frame, textvariable=self.progress_text).pack(anchor='w')
        self.summary = tk.StringVar(window)
        ttk.Label(frame, textvariable=self.summary).pack(anchor='w', pady=(0, 6))

        panes = ttk.Panedwindow(frame, orient='vertical')
        panes.pack(fill='both', expand=True)
        catalog = ttk.Labelframe(panes, text='기존 분석곡', padding=6)
        panes.add(catalog, weight=2)
        self.tracks = self.table(catalog, CATALOG_COLUMNS, height=6, stretch=('title', 'path'))
        self.tracks.bind('<<TreeviewSelect>>', self.select_tracks)
        self.tracks.bind('<Control-a>', self.select_all_tracks)
        self.path = tk.StringVar(window)
        self.detail = tk.StringVar(window)
        path_bar = ttk.Frame(catalog)
        path_bar.pack(fill='x', pady=(5, 0))
        ttk.Label(path_bar, text='연결된 음원').pack(side='left', padx=(0, 6))
        ttk.Entry(path_bar, textvariable=self.path, state='readonly').pack(side='left', fill='x', expand=True)
        ttk.Label(catalog, textvariable=self.detail).pack(fill='x', pady=(4, 0))

        comparison = ttk.Labelframe(panes, text='기존 싱크와 재분석 후보 비교', padding=6)
        panes.add(comparison, weight=3)
        self.comparison_text = tk.StringVar(window)
        ttk.Label(comparison, textvariable=self.comparison_text).pack(fill='x', pady=(0, 4))
        comparison_body = ttk.Frame(comparison)
        comparison_body.pack(fill='both', expand=True)
        metrics = ttk.Frame(comparison_body)
        metrics.pack(side='left', fill='y', padx=(0, 8))
        self.metrics = self.table(metrics, (('metric', '비교 항목', 131), ('before', '기존', 150),
                                            ('after', '신규', 150)), height=len(METRICS), selectmode='none')
        self.rows = self.table(comparison_body, ROW_COLUMNS, height=len(METRICS), stretch=('text',))
        self.rows.bind('<<TreeviewSelect>>', self.update_review_controls)

        apply_bar = ttk.Frame(frame)
        apply_bar.pack(fill='x', pady=(6, 2))
        self.apply_all = self.button(apply_bar, '전체 적용', lambda: self.apply())
        self.apply_improved = self.button(apply_bar, '개선 후보만 적용', lambda: self.apply(improvements_only=True))
        self.apply_tracks = self.button(apply_bar, '선택 곡 적용', lambda: self.apply(ids=self.tracks.selection()))
        self.apply_rows = self.button(apply_bar, '선택 가사 줄 적용', self.apply_selected_rows)
        self.keep_button = self.button(apply_bar, '기존 싱크 유지', self.keep)
        self.discard_button = self.button(apply_bar, '선택 후보 폐기', self.discard)
        restore_bar = ttk.Frame(frame)
        restore_bar.pack(fill='x', pady=(2, 0))
        self.restore_tracks = self.button(restore_bar, '선택 곡 이전 결과 복원', lambda: self.restore(self.tracks.selection()))
        self.restore_all = self.button(restore_bar, '일괄 롤백', self.restore)
        ttk.Button(restore_bar, text='닫기', command=self.close).pack(side='right')
        self.notice = tk.StringVar(window)
        ttk.Label(frame, textvariable=self.notice, wraplength=max(750, width - 26)).pack(fill='x', pady=(4, 0))
        self.refresh()
        self.poll_id = window.after(500, self.poll)

    def choice(self, parent, label, choices, width):
        ttk.Label(parent, text=label).pack(side='left', padx=(0, 5))
        widget = ttk.Combobox(parent, values=tuple(choices), width=width, state='readonly')
        widget.current(0)
        widget.pack(side='left', padx=(0, 12))
        return widget

    def button(self, parent, label, action):
        button = ttk.Button(parent, text=label, command=lambda: self.invoke(action))
        button.pack(side='left', padx=(0, 5))
        return button

    def table(self, parent, columns, *, height, stretch=(), selectmode='extended'):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[name for name, _, _ in columns], show='headings',
                            height=height, selectmode=selectmode)
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=40, stretch=name in stretch)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        return tree

    def show(self):
        self.window.deiconify()
        self.window.lift()

    def close(self):
        self.window.destroy()

    def destroyed(self, event):
        if event.widget is not self.window:
            return
        if self.poll_id is not None:
            self.window.after_cancel(self.poll_id)
            self.poll_id = None
        if getattr(self.app, 'sync_reanalysis_dialog', None) is self:
            self.app.sync_reanalysis_dialog = None

    def invoke(self, action):
        try:
            action()
            self.refresh()
        except Exception as error:
            self.notice.set(str(error))

    def poll(self):
        self.poll_id = None
        try:
            snapshot = self.service.snapshot()
            if snapshot != self.snapshot:
                self.refresh(snapshot)
        except Exception as error:
            self.notice.set(str(error))
        finally:
            self.poll_id = self.window.after(500, self.poll)

    def refresh_inventory(self):
        refresh_async = getattr(self.service, 'refresh_async', None)
        if refresh_async:
            refresh_async()
            self.notice.set('')
        else:
            self.service.refresh()
            self.notice.set('저장된 분석곡과 연결 음원을 다시 확인했습니다.')

    def refresh(self, snapshot=None):
        self.snapshot = snapshot if snapshot is not None else self.service.snapshot()
        items = {str(item['id']): item for item in self.service.list_items()}
        for track in self.items.keys() - items.keys():
            self.tracks.delete(track)
        for position, (track, item) in enumerate(items.items()):
            if track in self.items:
                if item != self.items[track]:
                    self.tracks.item(track, values=self.track_values(item))
            else:
                self.tracks.insert('', position, iid=track, values=self.track_values(item))
        self.items = items
        self.update_progress()
        self.select_tracks()

    @staticmethod
    def track_values(item):
        timed, total = item.get('timed'), item.get('total')
        completion = f'{timed}/{total}' if timed is not None and total is not None else '—'
        if item.get('eligible'):
            availability = '가능'
        elif item.get('audio_kind') == 'missing' or not item.get('path'):
            availability = '음원 필요'
        else:
            availability = '조건 미충족'
        status = item.get('status', 'idle')
        return (item.get('title') or '—', item.get('artist') or '—', item.get('identifier') or '—',
                Path(item['path']).name if item.get('path') else '—', metric_text('engine', item.get('engine')),
                date_text(item.get('updated')), completion, availability, '있음' if item.get('manual') else '—',
                STATES.get(status, status))

    def update_progress(self):
        data = self.snapshot
        total, remaining = data['total'], data['remaining']
        self.progress.configure(maximum=max(1, total), value=max(0, total - remaining))
        state = STATES.get(data['state'], data['state'])
        running = f' · 현재 {data["running"]}' if data['running'] else ''
        self.progress_text.set(f'{state} · 전체 {total}곡 · 완료 {data["done"]} · 남음 {remaining}'
                               f' · 오류 {data["failed"]} · 건너뜀 {data["skipped"]}{running}'
                               + (f' · {data["worker_error"]}' if data.get('worker_error') else ''))
        self.summary.set(f'완성도 증가 {data.get("more_timed", data["improved"])}곡 · 감소 {data["regressed"]}곡'
                         f' · 시작 시각 복구 {data["recovered"]}줄 · 새 누락 {data["new_missing"]}줄'
                         f' · 적용 {data["applied"]}곡 · 복원 {data["restored"]}곡')

    def select_all_tracks(self, _):
        self.tracks.selection_set(self.tracks.get_children())
        return 'break'

    def select_tracks(self, _=None):
        selected = self.tracks.selection()
        focus = self.tracks.focus()
        track = focus if focus in selected else next(iter(selected), None)
        item = self.items.get(track, {})
        self.path.set(item.get('path') or '')
        self.detail.set(' · '.join(str(value) for value in (item.get('identifier'),
                                                            AUDIO_KINDS.get(item.get('audio_kind')),
                                                            item.get('reason'), item.get('error')) if value))
        result = self.service.compare(track) if track is not None else None
        if track != self.result_id or result != self.result:
            self.result_id, self.result = track, result
            self.show_comparison(item)
        self.update_controls()

    def show_comparison(self, item):
        for tree in (self.metrics, self.rows):
            tree.delete(*tree.get_children())
        if not self.result:
            self.comparison_text.set('재분석이 완료된 곡을 선택하세요.')
            return
        label = '개선 가능성 있음' if self.result['improved'] else '검토 필요'
        if self.result.get('apply_blocked'):
            label = '재생 시간축 확인 필요 · 비교만 가능'
        self.comparison_text.set(f'{item.get("title", "")} · {label} · 싱크 완성도와 정렬 지표 기준')
        before, after = self.result['before'], self.result['after']
        for key, label in METRICS:
            left, right = metric_text(key, before.get(key)), metric_text(key, after.get(key))
            if key == 'engine':
                left = ' · '.join([left, *(str(before[field]) for field in ('version', 'profile') if before.get(field))])
                right = ' · '.join([right, *(str(after[field]) for field in ('version', 'profile') if after.get(field))])
            self.metrics.insert('', 'end', values=(label, left, right))
        for row in self.result['rows']:
            index, original, candidate = row['index'], row.get('before'), row.get('after')
            delta = time_text(candidate - original, signed=True) if original is not None and candidate is not None else '—'
            status = ROW_STATES.get(row.get('status'), row.get('status') or '—')
            if row.get('protected'):
                status = '기존 싱크 보호'
            self.rows.insert('', 'end', iid=str(index), values=(index + 1, row.get('text', ''),
                             time_text(original), time_text(candidate), delta, status,
                             metric_text('quality', row.get('quality'))))

    @staticmethod
    def enabled(widget, enabled):
        widget.state(['!disabled' if enabled else 'disabled'])

    def update_controls(self, _=None):
        data = self.snapshot
        preview = self.service.preview(MODES[self.mode.get()], self.tracks.selection())
        loading = data.get('loading', False)
        error = data.get('inventory_error')
        if loading:
            self.scope.set('저장된 분석곡과 연결 음원 확인 중…')
        elif error:
            self.scope.set(f'분석곡 목록을 읽지 못했습니다. {error}')
        else:
            self.scope.set(f'대상 {preview["target"]}곡 · 실제 오디오 분석 가능 {preview["eligible"]}곡')
        active = bool(data['running']) or data['state'] == 'running'
        paused_with_pending = data['state'] == 'paused' and data['remaining'] > 0
        self.enabled(self.start_button, not loading and not active and not paused_with_pending
                     and preview['eligible'] > 0)
        self.enabled(self.pause_button, data['state'] == 'running')
        self.enabled(self.resume_button, data['state'] in ('paused', 'cancelled') and data['remaining'] > 0)
        self.enabled(self.cancel_button, data['state'] in ('running', 'paused'))
        self.enabled(self.retry_button, not active and data['failed'] > 0)
        self.enabled(self.refresh_button, not loading and not active)
        self.update_review_controls()

    def update_review_controls(self, _=None):
        if self.snapshot is None:
            return
        available = not self.snapshot['running'] and self.snapshot['state'] != 'running'
        candidates = {track for track, item in self.items.items()
                      if item.get('has_candidate', item.get('status') in ('done', 'applied', 'restored'))}
        applicable = {track for track in candidates if self.items[track].get('can_apply', True)}
        restorable = {track for track, item in self.items.items()
                      if item.get('can_restore', item.get('status') == 'applied')}
        selected = self.tracks.selection()
        selected_candidates = candidates.intersection(selected)
        for button in (self.apply_all, self.apply_improved):
            self.enabled(button, available and bool(applicable))
        self.enabled(self.apply_tracks, available and bool(applicable.intersection(selected)))
        for button in (self.keep_button, self.discard_button):
            self.enabled(button, available and bool(selected_candidates))
        self.enabled(self.apply_rows, available and bool(self.selected_rows()))
        self.enabled(self.restore_tracks, available and bool(restorable.intersection(selected)))
        self.enabled(self.restore_all, available and self.snapshot['applied'] > 0)

    def selected_rows(self):
        if not self.result or self.result.get('apply_blocked'):
            return ()
        selected = set(self.rows.selection())
        return tuple(row['index'] for row in self.result['rows'] if str(row['index']) in selected
                     and not row.get('protected') and row.get('after') is not None
                     and row.get('after') != row.get('before'))

    def start(self):
        self.service.start(MODES[self.mode.get()], self.tracks.selection(), force=self.force.get(),
                           engine=ENGINES[self.engine.get()])
        self.notice.set('재분석 결과는 비교할 수 있는 후보로 저장됩니다.')

    def apply(self, **options):
        count = self.service.apply(**options)
        self.notice.set(f'재분석 후보를 {count}곡에 적용했습니다.')
        self.notify_changed(count)

    def apply_selected_rows(self):
        self.apply(ids=(self.result_id,), line_indices=self.selected_rows())

    def keep(self):
        self.notice.set('기존 싱크를 유지했습니다. 저장된 후보는 나중에 다시 비교할 수 있습니다.')

    def discard(self):
        count = self.service.discard(self.tracks.selection())
        self.notice.set(f'{count}곡의 재분석 후보를 폐기했습니다.')

    def restore(self, ids=None):
        count = self.service.restore(ids)
        self.notice.set(f'{count}곡의 이전 싱크를 복원했습니다.')
        self.notify_changed(count)

    def notify_changed(self, count):
        changed = getattr(self.app, 'sync_reanalysis_changed', None)
        if count and changed:
            changed()
