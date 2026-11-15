"""Own one disposable audio worker. The UI process never imports an ASR model."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from storage import atomic_json
from app_paths import frozen, runtime_dir, worker_command, worker_environment
from audio_capture_cache import REALTIME_PAUSE_NAME, discard_retry_capture


MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')
FINAL_GPU_MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json')
ANALYSIS_LOCK = threading.Lock()
REALTIME_LOCK = threading.Lock()


def ready(base):
    from process_audio import ensure_supported
    ensure_supported()
    if frozen():
        from optional_runtime import installed
        missing=[] if installed('audio') else ['optional runtime']
    else:
        missing = [name for name in ('faster_whisper','stable_whisper','torch','torchaudio')
                   if importlib.util.find_spec(name) is None]
    model = (runtime_dir() if frozen() else Path(base)) / 'models' / 'whisper-small'
    if missing or not all((model/name).is_file() for name in MODEL_FILES):
        raise ValueError('AI 음원 분석은 선택 설치입니다. 가사창 우클릭 → AI 음원 분석 설치…에서 다운로드하세요.')


class AudioJob:
    def __init__(self, base, song, playback, messages, max_start_position=3, memory_only=False, start_index=None, realtime_device='cpu', defer_lyrics=False,
                 incremental=False,confirmed_times=None,analyzed_ranges=None,capture_from=0,alignment=None,
                 capture_window=None,row_kinds=None,capture_cache=None,cached_capture=None,
                 playlist_analysis=False,realtime_enabled=True):
        self.song, self.messages = song, messages
        self.memory_only=memory_only
        self.memory_lines=[line.original for line in song.lines] if memory_only and not defer_lyrics else None
        self.memory_readings=None
        self.waiting_for_lyrics=defer_lyrics
        self.capture_complete=False
        self.capture_started=False
        self.playlist_analysis=bool(playlist_analysis)
        self.analysis_committed=None
        self.capture_cache=capture_cache
        self.capture_duration=playback.duration
        self.capture_start_position=playback.position()
        self.incremental=bool(incremental)
        self.skip_final=start_index is not None and not self.incremental
        self.temp = tempfile.TemporaryDirectory(prefix='spotify-lyrics-audio-')
        self.cancelled = threading.Event()
        self.realtime_enabled=realtime_enabled
        self.realtime_stop=threading.Event();self.realtime_process=None;self.realtime_thread=None
        self.process_lock = threading.Lock()
        folder = Path(self.temp.name)
        runtime = runtime_dir() if frozen() else Path(base)
        request = {'key': playback.key, 'duration': playback.duration,
                   'max_start_position':max_start_position,
                   'lines': [line.original for line in song.lines] if song else [],
                   'model': str(runtime / 'models' / 'whisper-small')}
        if capture_cache is not None:request['capture_cache']=str(Path(capture_cache).resolve())
        if cached_capture is not None:request['cached_capture']=str(Path(cached_capture).resolve())
        if playlist_analysis:request['playlist_analysis']=True
        if alignment:
            from japanese_alignment import alignment_options
            request.update(alignment_options(alignment,runtime))
        if song and not defer_lyrics:
            self._set_readings(song,request,memory_only)
        if memory_only and not defer_lyrics:
            request.pop('lines');request['memory_lines']=True
        if self.skip_final:request.update(allow_partial=True,start_index=start_index)
        if self.incremental:
            request.update(incremental=True,allow_partial=True,capture_from=capture_from,
                           analyzed_ranges=analyzed_ranges or [])
            if confirmed_times is not None:request['confirmed_times']=confirmed_times
            if start_index is not None:request['start_index']=start_index
        if defer_lyrics:
            request.pop('lines');request.update(allow_partial=True,defer_lyrics=True)
        if capture_window is not None:
            request.update(capture_window=capture_window,row_kinds=row_kinds)
        request['realtime']=(not playlist_analysis and cached_capture is None and capture_window is None
                             and all((Path(request['model'])/name).is_file() for name in MODEL_FILES))
        if realtime_device in ('auto','cuda'):
            request['realtime_device']=realtime_device
            request['final_device']=realtime_device
            accelerated=runtime/'models'/'whisper-medium'
            if all((accelerated/name).is_file() for name in MODEL_FILES):
                request['realtime_model']=str(accelerated)
            final_model=runtime/'models'/'whisper-turbo'
            if all((final_model/name).is_file() for name in FINAL_GPU_MODEL_FILES):
                request['final_model']=str(final_model)
            request['live_window_seconds']=8.0
        if request.get('romaji_realtime'):
            request['live_window_seconds']=max(6.0,request.get('live_window_seconds',0))
        request['final_cpu_percent']=20 if realtime_device in ('auto','cuda') else 1 if request['realtime'] else 3
        self.playback_key=playback.key
        atomic_json(folder / 'request.json', request)
        # Windows venv python.exe is a redirector that can leave its child alive
        # when terminated. Start the actual interpreter with this venv's packages.
        self.environment = worker_environment(base)
        self.command = worker_command(base, 'audio_worker', optional=True)
        self.request_path = folder/'request.json'
        self.analysis_request_path=self.request_path
        self.request=request
        try:
            if request['realtime'] and not realtime_enabled:
                self.request_path.with_name(REALTIME_PAUSE_NAME).touch()
            self.process = self._launch([str(self.request_path)])
        except Exception:
            self.temp.cleanup()
            raise
        self.thread = threading.Thread(target=self._read, name='audio-job-results', daemon=True)
        with self.process_lock:
            self._start_realtime_locked()
        self.thread.start()

    def _set_readings(self,song,request,memory_only):
        from japanese_alignment import DEFAULT_PROFILE
        if not request.get('alignment_engine') or request.get('romaji_profile',DEFAULT_PROFILE)=='baseline':
            return
        readings=[line.reading or None for line in song.lines]
        if any(readings):
            if memory_only:self.memory_readings=readings
            else:request['line_readings']=readings

    def bind_lyrics(self,song,memory_only=False,start_index=None,analysis_context=None):
        """Attach lyrics to the already running capture without losing its prefix."""
        with self.process_lock:
            if not self.waiting_for_lyrics or self.capture_complete or self.cancelled.is_set():return False
            request={**self.request}
            request.pop('defer_lyrics',None)
            if analysis_context:
                request.update({key:analysis_context[key] for key in
                                ('confirmed_times','analyzed_ranges','capture_from') if key in analysis_context})
            lines=[line.original for line in song.lines]
            if memory_only:request['memory_lines']=True
            else:request['lines']=lines
            self._set_readings(song,request,memory_only)
            if start_index is not None:request['start_index']=start_index
            # The capture child may still be reading its initial request. Keep
            # it immutable; only later model children receive the bound request.
            bound_path=self.request_path.with_name('bound-request.json')
            atomic_json(bound_path,request)
            self.song=song;self.memory_only=memory_only
            self.memory_lines=lines if memory_only else None
            self.analysis_request_path=bound_path
            self.waiting_for_lyrics=False
            self._start_realtime_locked()
            return True

    def _start_realtime_locked(self):
        if (not self.realtime_enabled or not self.request['realtime'] or self.waiting_for_lyrics
                or self.capture_complete or self.cancelled.is_set()
                or self.realtime_thread and self.realtime_thread.is_alive()):
            return
        self.realtime_stop.clear()
        self.realtime_process=None
        self.realtime_thread=threading.Thread(target=self._read_realtime,name='audio-realtime-results',daemon=True)
        self.realtime_thread.start()

    def set_realtime_enabled(self, enabled):
        with self.process_lock:
            if self.realtime_enabled == enabled or not self.request['realtime'] or self.capture_complete or self.cancelled.is_set():
                return
            marker = self.request_path.with_name(REALTIME_PAUSE_NAME)
            if enabled:
                marker.unlink(missing_ok=True)
            else:
                marker.touch()
                self.realtime_stop.set()
            self.realtime_enabled=enabled
            if enabled:self._start_realtime_locked()
        if not enabled:self._stop_realtime()

    def _launch(self, arguments):
        memory_lines=self.memory_only and self.memory_lines is not None
        # Capture has a short device deadline; lowering it with the model
        # workers can lose audio while GPU/model startup or disk writes run.
        priority='BELOW_NORMAL_PRIORITY_CLASS' if arguments[0] in ('--analyze','--realtime') else 'NORMAL_PRIORITY_CLASS'
        process=subprocess.Popen(self.command+arguments, stdin=subprocess.PIPE if memory_lines else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding='utf-8',
            env=self.environment,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            | getattr(subprocess, priority, 0))
        if memory_lines:
            try:
                payload=({'lines':self.memory_lines,'line_readings':self.memory_readings}
                         if self.memory_readings else self.memory_lines)
                json.dump(payload,process.stdin,ensure_ascii=False)
                process.stdin.close()
            except Exception:
                process.terminate();process.wait()
                process.stdin.close();process.stdout.close()
                raise
        return process

    def _read(self):
        terminal = False
        analysis_locked = False
        try:
            for phase in ('capture', 'analyze'):
                captured = False
                for line in self.process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if self.cancelled.is_set():
                        continue
                    if event.get('kind')=='progress' and event.get('state')=='recording':
                        self.capture_started=True
                    if event.get('kind') == 'captured':
                        self.capture_started=True
                        captured = True
                        continue
                    if event.get('kind') in ('result', 'error'):
                        terminal = True
                    if event.get('kind')=='progress' and event.get('state') in ('discarded','unmatched'):terminal=True
                    self.messages.put(('audio', (self, event)))
                code = self.process.wait()
                if phase != 'capture' or not captured or code or terminal:
                    break
                self.process.stdout.close()
                with self.process_lock:
                    self.capture_complete=True
                    unbound=self.waiting_for_lyrics
                if unbound:
                    terminal=True
                    self.messages.put(('audio',(self,{'kind':'progress','state':'discarded',
                        'text':'곡이 끝날 때까지 가사를 찾지 못해 임시 음원을 정리했습니다.'})))
                    break
                self._stop_realtime()
                if self.skip_final:
                    terminal=True
                    self.messages.put(('audio',(self,{'kind':'progress','state':'partial',
                        'text':'중간 재생의 임시 추적 완료 · 다음 처음 재생에서 전체 싱크를 분석합니다.'})))
                    break
                self.messages.put(('audio',(self,{'kind':'progress','text':'앞 곡 분석이 끝나면 이어서 분석합니다.','state':'queued'})))
                while not self.cancelled.is_set():
                    if ANALYSIS_LOCK.acquire(timeout=.2):
                        analysis_locked=True
                        break
                with self.process_lock:
                    if self.cancelled.is_set():
                        break
                    # WinRT bundles an older MSVC runtime. Load ASR in a fresh
                    # process after capture exits to avoid native DLL collisions.
                    self.process = self._launch(['--analyze',str(self.analysis_request_path)])
            if not terminal and not self.cancelled.is_set():
                self.messages.put(('audio', (self, {'kind':'error', 'text':f'분석 프로세스가 종료되었습니다 (코드 {code}). 설치 상태와 출력 장치를 확인하세요.',
                    'diagnostic':{'stage':phase,'error_type':'WorkerExit','exit_code':code}})))
        except Exception as error:
            if not self.cancelled.is_set():
                self.messages.put(('audio', (self, {'kind':'error','text':f'분석 실행 실패 ({type(error).__name__}). 설치 상태를 확인하세요.',
                    'diagnostic':{'stage':phase,'error_type':type(error).__name__}})))
        finally:
            with self.process_lock:self.capture_complete=True
            self.cleanup_errors=[]
            def cleanup(stage,operation):
                try:operation()
                except Exception as error:
                    failure={'stage':stage,'error':type(error).__name__}
                    if getattr(error,'winerror',None) is not None:failure['winerror']=error.winerror
                    self.cleanup_errors.append(failure)
                    if not isinstance(error,OSError):raise
            # Every later resource and the UI completion signal must survive an
            # earlier cleanup failure. Unexpected errors remain observable.
            try:
                cleanup('realtime',self._stop_realtime)
            finally:
                try:
                    if analysis_locked:cleanup('analysis_lock',ANALYSIS_LOCK.release)
                finally:
                    try:
                        cleanup('stdout',self.process.stdout.close)
                    finally:
                        try:
                            cleanup('temporary',self.temp.cleanup)
                        finally:
                            finished={'kind':'finished'}
                            if self.cleanup_errors:
                                finished.update(cleanup_errors=self.cleanup_errors,temporary_directory=str(self.temp.name))
                            self.messages.put(('audio',(self,finished)))

    def _read_realtime(self):
        locked=False
        try:
            while not self.realtime_stop.is_set():
                if REALTIME_LOCK.acquire(timeout=.1):locked=True;break
            if not locked:return
            with self.process_lock:
                if self.realtime_stop.is_set():return
                self.realtime_process=self._launch(['--realtime',str(self.analysis_request_path)])
            for line in self.realtime_process.stdout:
                if self.cancelled.is_set() or self.realtime_stop.is_set():continue
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('kind') in ('realtime','realtime_status'):
                    self.messages.put(('audio',(self,event)))
            code=self.realtime_process.wait()
            if code and not self.realtime_stop.is_set():
                self.messages.put(('audio',(self,{'kind':'realtime_status','state':'failed',
                    'text':'실시간 음원 대조가 중단됐습니다. 종료 후 전체 분석은 계속합니다.'})))
        except OSError:
            if not self.realtime_stop.is_set():self.messages.put(('audio',(self,{
                'kind':'realtime_status','state':'failed','text':'실시간 분석 프로세스를 시작하지 못했습니다.'})))
        finally:
            try:
                if self.realtime_process:
                    try:self.realtime_process.wait()
                    finally:self.realtime_process.stdout.close()
            finally:
                if locked:REALTIME_LOCK.release()

    def _stop_realtime(self):
        with self.process_lock:
            self.realtime_stop.set()
            if self.realtime_process and self.realtime_process.poll() is None:self.realtime_process.terminate()
            thread=self.realtime_thread
        if thread:thread.join(timeout=5)

    def cancel(self):
        self.cancelled.set()
        self._stop_realtime()
        with self.process_lock:
            if self.process.poll() is None:
                self.process.terminate()

    def finish_capture(self):
        """Finish the current safe audio prefix, then keep the queued analysis."""
        if self.cancelled.is_set() or self.capture_complete:return
        if not self.incremental or self.waiting_for_lyrics:
            self.cancel();return
        self.request_path.with_name('finish-capture').touch()

    def release_retry_capture(self):
        if self.capture_cache:
            try:discard_retry_capture(self.capture_cache, self.playback_key, self.capture_duration)
            except OSError:return False
        return True

    def close(self):
        self.cancel()
        self.thread.join(timeout=5)
