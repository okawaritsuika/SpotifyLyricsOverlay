"""Follow the desktop Spotify process without depending on its playback/window state."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from windows import SingleInstance


POLL_SECONDS = 2
EXIT_GRACE_SECONDS = 3
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
LOGGER = logging.getLogger('spotify_lifecycle')


def spotify_running():
    """Check desktop processes in this login session, including minimized Spotify.

    A failed enumeration is an error, never evidence that Spotify exited.
    No playback session or visible window is required.
    """
    if sys.platform != 'win32':return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.szExeFile.casefold() == 'spotify.exe':
                candidate = wintypes.DWORD()
                if kernel.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(candidate)):
                    if candidate.value == session.value:return True
                else:
                    error = ctypes.get_last_error()
                    # The process may have ended between snapshot and lookup.
                    if error != 87:raise ctypes.WinError(error)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:raise ctypes.WinError(error)  # ERROR_NO_MORE_FILES
        return False
    finally:
        kernel.CloseHandle(snapshot)


class SpotifySession:
    """Debounce process disappearance and emit one transition per Spotify run."""
    def __init__(self):
        self.running = False
        self.absent_since = None

    def observe(self, running, now):
        if running:
            self.absent_since = None
            if not self.running:
                self.running = True
                return 'started'
        elif self.running:
            if self.absent_since is None:self.absent_since = now
            elif now - self.absent_since >= EXIT_GRACE_SECONDS:
                self.running = False
                self.absent_since = None
                return 'stopped'
        return ''

    def unknown(self):
        self.absent_since = None


class SpotifyLifecycleMonitor:
    """Ask the UI to close normally when a previously observed Spotify exits."""
    def __init__(self, messages, enabled=False, observed_running=False):
        self.messages = messages
        self.enabled = bool(enabled)
        self.session = SpotifySession()
        # The watcher observed Spotify before launching us. It may already have
        # exited by the time imports, saved data and the UI finish loading.
        self.session.running = bool(observed_running)
        self.lock = threading.Lock()
        self.generation = 0
        self.failed = False
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='spotify-lifecycle')

    def start(self):self.thread.start()

    def stop(self):self.stop_event.set()

    def configure(self, enabled):
        with self.lock:
            if self.enabled == bool(enabled):return
            self.enabled = bool(enabled)
            self.generation += 1
            self.session = SpotifySession()
            self.failed = False

    def poll(self):
        with self.lock:
            if not self.enabled:return
            generation = self.generation
        try:
            running = spotify_running()
        except OSError as error:
            with self.lock:
                if generation != self.generation:return
                self.session.unknown()
                if not self.failed:
                    self.messages.put(('spotify_lifecycle_error', str(error)))
                    self.failed = True
            return
        with self.lock:
            if generation != self.generation or self.stop_event.is_set():return
            self.failed = False
            if self.session.observe(running, time.monotonic()) == 'stopped':
                self.messages.put(('spotify_exit', None))

    def _run(self):
        while not self.stop_event.is_set():
            self.poll()
            self.stop_event.wait(POLL_SECONDS)


def _directory_key(directory):
    return hashlib.sha256(os.path.normcase(str(Path(directory).resolve())).encode()).hexdigest()[:24]


def startup_name(directory):
    return 'SpotifyLyricsOverlay-' + _directory_key(directory)


def launch_command(directory, watcher=False):
    """Use this installation and this data directory, even when paths have spaces."""
    executable = Path(sys.executable)
    directory = str(Path(directory).resolve())
    if getattr(sys, 'frozen', False):
        command = [str(executable)]
        if watcher:command.append('--watch-spotify')
    else:
        windowless = executable.with_name('pythonw.exe')
        if windowless.is_file():executable = windowless
        base = Path(__file__).resolve().parent
        command = [str(executable), str(base / ('spotify_lifecycle.py' if watcher else 'app.py'))]
        if watcher:command.append('--watch')
    command.extend(['--data-dir', directory])
    if not watcher:command.append('--spotify-auto-start')
    return command


def configure_startup(directory, enabled):
    """Register/remove only this installation's per-user login watcher."""
    import winreg
    name = startup_name(directory)
    if enabled:
        command = subprocess.list2cmdline(launch_command(directory, watcher=True))
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def _watcher_directory(directory):
    return Path(directory).resolve() / '.spotify-watcher'


def _launch(directory, watcher=False):
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    return subprocess.Popen(launch_command(directory, watcher),
        cwd=str(base), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ,'PYINSTALLER_RESET_ENVIRONMENT':'1'} if getattr(sys,'frozen',False) else None,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)


def ensure_watcher(directory):
    with SingleInstance(_watcher_directory(directory)) as instance:
        if not instance.acquired:return None
    return _launch(directory, watcher=True)


def _enabled(directory):
    try:
        settings = json.loads((Path(directory) / 'settings.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    return isinstance(settings, dict) and settings.get('spotify_lifecycle') is True


def _overlay_running(directory):
    with SingleInstance(directory) as instance:
        return not instance.acquired


def _exit_marker(directory):
    return Path(directory) / '.spotify-exit'


def mark_spotify_exit(directory):
    """Mark automatic shutdown before the app starts potentially slow cleanup."""
    marker = _exit_marker(directory)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def clear_spotify_exit(directory):
    """A newly acquired app instance has completed the previous shutdown handoff."""
    try:_exit_marker(directory).unlink()
    except FileNotFoundError:pass


def _closing_for_spotify(directory):
    return _exit_marker(directory).is_file()


def watch(directory):
    """Remain small and idle between launches; manual overlay close is respected."""
    directory = Path(directory).resolve()
    with SingleInstance(_watcher_directory(directory)) as instance:
        if not instance.acquired:return
        session = SpotifySession()
        failed = False
        pending_launch = False
        while _enabled(directory):
            try:
                running = spotify_running()
                transition = session.observe(running, time.monotonic())
                if transition == 'started':pending_launch = True
                elif transition == 'stopped':pending_launch = False
                # Spotify may restart while the previous overlay is still
                # saving data and stopping workers. A marked automatic close
                # remains a handoff until that instance releases its mutex.
                closing = _closing_for_spotify(directory)
                if closing and running:pending_launch = True
                if pending_launch and running:
                    if not _overlay_running(directory):
                        clear_spotify_exit(directory)
                        _launch(directory)
                        LOGGER.info('Started overlay for Spotify: %s', directory)
                        pending_launch = False
                    elif not closing:
                        pending_launch = False
                failed = False
            except OSError:
                session.unknown()
                if not failed:LOGGER.exception('Spotify lifecycle check failed')
                failed = True
            time.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', action='store_true', required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(args.data_dir / 'spotify_lifecycle.log',
                                  maxBytes=128 * 1024, backupCount=1, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    try:watch(args.data_dir)
    except (OSError, ValueError):LOGGER.exception('Spotify watcher stopped')


if __name__ == '__main__':
    main()
