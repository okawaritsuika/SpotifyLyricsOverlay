"""Read and control Spotify's own volume slider without changing the OS mixer."""
import math
import ctypes
from ctypes import wintypes
import threading
import time
from contextlib import contextmanager

from screen_lyrics import process_is_spotify
from spotify_controls import page_controls


VOLUME_LABELS = frozenset(('볼륨 변경', '음량 변경', 'Change volume', 'Volume', '音量を変更', '音量'))
MUTE_LABELS = frozenset(('음소거하기', '음소거', 'Mute', 'ミュート', 'ミュートする'))
UNMUTE_LABELS = frozenset(('음소거 해제하기', '음소거 해제', 'Unmute', 'ミュートを解除', 'ミュート解除'))


def _spotify_windows(auto):
    # Avoid activating accessibility providers for every unrelated desktop app.
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = (callback, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.IsWindowVisible.argtypes = (wintypes.HWND,)
    handles, processes = [], {}
    @callback
    def visit(handle, _):
        if not user.IsWindowVisible(handle):return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value not in processes:processes[pid.value] = process_is_spotify(pid.value)
        if processes[pid.value]:handles.append(handle)
        return True
    user.EnumWindows(visit, 0)
    return [auto.ControlFromHandle(handle) for handle in handles]


def _find_controls(auto, stopped, deadline):
    matches = []
    for window in _spotify_windows(auto):
        if stopped.is_set():return None, None
        try:spotify = process_is_spotify(window.ProcessId)
        except Exception:continue  # Unrelated windows may close during enumeration.
        if not spotify:continue
        sliders, buttons = [], []
        for index, control in enumerate(page_controls(window, deadline)):
            if stopped.is_set():return None, None
            if index >= 1600 or time.monotonic() >= deadline:
                raise ValueError('Spotify 음량을 확인하지 못했습니다. 잠시 후 다시 시도합니다.')
            kind = control.ControlTypeName
            if kind == 'SliderControl' and control.Name in VOLUME_LABELS:
                sliders.append(control)
            elif kind == 'ButtonControl' and control.Name in MUTE_LABELS | UNMUTE_LABELS:
                buttons.append(control)
        if time.monotonic() >= deadline:
            raise ValueError('Spotify 음량을 확인하지 못했습니다. 잠시 후 다시 시도합니다.')
        if sliders:
            if len(sliders) != 1 or len(buttons) != 1:
                raise ValueError('Spotify 음량 컨트롤을 하나로 확인할 수 없습니다.')
            matches.append((sliders[0], buttons[0]))
    if len(matches) != 1:
        raise ValueError('Spotify 데스크톱 창을 열어 음량 조절을 연결해 주세요.')
    return matches[0]


def _volume_range(slider):
    if not slider.IsEnabled:raise ValueError('Spotify에서 현재 음량을 조절할 수 없습니다.')
    pattern = slider.GetRangeValuePattern()
    if pattern is None or pattern.IsReadOnly:
        raise ValueError('Spotify 음량 슬라이더에 접근할 수 없습니다.')
    minimum, maximum, value = pattern.Minimum, pattern.Maximum, pattern.Value
    if (not all(math.isfinite(number) for number in (minimum, maximum, value))
            or maximum <= minimum or not minimum <= value <= maximum):
        raise ValueError('Spotify 음량 범위를 확인하지 못했습니다.')
    return pattern, minimum, maximum, value


def _read_controls(slider, mute):
    pattern, minimum, maximum, value = _volume_range(slider)
    name = mute.Name
    if name not in MUTE_LABELS | UNMUTE_LABELS:
        raise ValueError('Spotify 음소거 상태가 변경되었습니다. 다시 연결합니다.')
    percent = max(0, min(100, round((value-minimum) * 100 / (maximum-minimum))))
    return {'available': True, 'volume': percent, 'step': _step_percent(pattern, minimum, maximum),
            'muted': name in UNMUTE_LABELS, 'error': ''}


def _step_percent(pattern, minimum, maximum):
    step = getattr(pattern, 'SmallChange', 0)
    if not math.isfinite(step) or step <= 0:return 1
    return max(1, min(100, round(step*100/(maximum-minimum))))


@contextmanager
def _automation_thread(auto):
    # UIA providers may stall on a background STA without a message pump.
    auto.comtypes.CoInitializeEx(0)
    try:yield
    finally:auto.comtypes.CoUninitialize()


def _refresh_controls(auto, slider):
    # Chromium can retain an old AX value after changing its hidden range input.
    # Refresh the already-visible volume bar, without focusing the Spotify window
    # or sending another volume/mute command.
    bar = slider.GetParentControl().GetParentControl()
    if bar.IsOffscreen:return
    pattern = bar.GetPattern(auto.PatternId.ScrollItemPattern)
    if pattern is not None:pattern.ScrollIntoView(waitTime=0)


def _apply(commands, stopped):
    if stopped.is_set():return None
    import uiautomation as auto
    # UIA objects are created, used and released in this worker's COM context.
    with _automation_thread(auto):
        slider, mute = _find_controls(auto, stopped, time.monotonic()+8)
        if stopped.is_set():return None
        _refresh_controls(auto, slider)
        state = _read_controls(slider, mute)
        for kind, value in commands:
            if stopped.is_set():return None
            # Check the owner and exact label again before issuing any command.
            if not process_is_spotify(slider.ProcessId) or slider.Name not in VOLUME_LABELS:
                raise ValueError('Spotify 음량 컨트롤이 변경되었습니다. 다시 연결합니다.')
            if kind == 'volume':
                pattern, minimum, maximum, _ = _volume_range(slider)
                step = _step_percent(pattern, minimum, maximum)
                value = max(0, min(100, round(value/step)*step))
                if stopped.is_set():return None
                if not pattern.SetValue(minimum+(maximum-minimum)*value/100, waitTime=0):
                    raise ValueError('Spotify가 음량 변경 요청을 처리하지 못했습니다.')
                expected = lambda result: (result['volume'] == value
                                           and result['muted'] == (value == 0))
            else:
                state = _read_controls(slider, mute)
                if (not mute.IsEnabled or not process_is_spotify(mute.ProcessId)
                        or mute.Name not in MUTE_LABELS | UNMUTE_LABELS):
                    raise ValueError('Spotify 음소거 버튼에 접근할 수 없습니다.')
                pattern = mute.GetInvokePattern()
                if stopped.is_set():return None
                if pattern is None or not pattern.Invoke(waitTime=0):
                    raise ValueError('Spotify가 음소거 요청을 처리하지 못했습니다.')
                expected = lambda result: result['muted'] != state['muted']
            # Chromium may publish the new slider/button value after SetValue.
            deadline = time.monotonic()+2
            settled = None
            while True:
                if stopped.is_set():return None
                _refresh_controls(auto, slider)
                result = _read_controls(slider, mute)
                now = time.monotonic()
                if expected(result) and (not result['muted'] or result['volume'] == 0):
                    if settled is None:settled = now
                    elif now-settled >= .3:break
                else:settled = None
                if stopped.is_set():return None
                if now >= deadline:
                    raise ValueError('Spotify 음량 변경을 확인하지 못했습니다. 다시 시도해 주세요.')
                if stopped.wait(.03):return None
            state = result
        return state


class SpotifyVolumeController:
    """Nonblocking commands; on_state receives dictionaries on the worker thread.

    Send callbacks through a queue before touching Tk widgets. A callback's
    revision includes every accepted volume/mute command up to that revision.
    Read results superseded by newer commands are discarded. Closing permanently
    cancels queued commands; create a new controller when reopening the popup.
    """
    def __init__(self, on_state=None):
        self.on_state = on_state
        self._condition = threading.Condition()
        self._stopped = threading.Event()
        self._thread = None
        self._commands = []
        self._refresh = False
        self._revision = 0
        self._state = {'available': False, 'volume': 100, 'muted': False,
                       'error': '', 'busy': False, 'revision': 0}

    def start(self):
        # comtypes initializes its first importing thread as STA. Import here
        # so the dedicated worker can use MTA for its accessibility requests.
        import uiautomation
        with self._condition:
            if self._stopped.is_set():return False
            if self._thread is None:
                self._refresh = True
                self._thread = threading.Thread(target=self._run, name='SpotifyVolume', daemon=True)
                self._thread.start()
            return True

    def request_refresh(self):
        with self._condition:
            if self._stopped.is_set():return False
            self._refresh = True
            self._condition.notify()
            return True

    def set_volume(self, value):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):return False
        return self._enqueue('volume', round(value))

    def toggle_mute(self):
        return self._enqueue('mute', None)

    def _enqueue(self, kind, value):
        with self._condition:
            if self._stopped.is_set():return False
            if kind == 'volume' and self._commands and self._commands[-1][0] == 'volume':
                self._commands[-1] = (kind, value)
            else:self._commands.append((kind, value))
            self._revision += 1
            self._state = {**self._state, 'busy': True, 'revision': self._revision}
            self._condition.notify()
            return True

    def get_state(self):
        with self._condition:return dict(self._state)

    def close(self):
        with self._condition:
            self._stopped.set()
            self._commands.clear()
            self._refresh = False
            self._condition.notify()

    def _run(self):
        import uiautomation as auto
        # Keep the apartment alive between polls: Chromium's UIA connection
        # must not be torn down while the shared automation client still lives.
        with _automation_thread(auto):self._run_commands()

    def _run_commands(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped.is_set() or self._refresh or self._commands)
                if self._stopped.is_set():return
                commands, self._commands = self._commands, []
                revision = self._revision
                self._refresh = False
            try:state = _apply(commands, self._stopped)
            except Exception as error:
                state = {'available': False, 'volume': 100, 'muted': False, 'error': str(error)}
            with self._condition:
                if self._stopped.is_set():return
                if state is None or revision != self._revision:continue
                self._state = {**state, 'busy': False, 'revision': revision}
                published = dict(self._state)
            if self.on_state:
                try:self.on_state(published)
                except Exception:pass  # A closed UI must not strand the worker.
