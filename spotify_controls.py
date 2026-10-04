"""Invoke only the requested recording's Spotify desktop row, in a bounded worker.

No global media keys, screen coordinates, account tokens, or developer app keys.
The caller still verifies actual playback through the Windows media session.
"""
import json
import math
from pathlib import Path
import re
import subprocess
from app_paths import worker_command, worker_environment
import sys
import time

from spotify_api import text_key


def complete_track(track):
    if not isinstance(track, dict):return False
    duration = track.get('duration')
    artists = track.get('artists')
    return bool(re.fullmatch(r'[A-Za-z0-9]{22}', str(track.get('id', '')))
                and all(isinstance(track.get(key), str) and 0 < len(track[key]) <= 4000
                        for key in ('title', 'album'))
                and isinstance(artists, list) and 0 < len(artists) <= 200
                and all(isinstance(name, str) and 0 < len(name) <= 512 for name in artists)
                and isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and math.isfinite(duration) and 0 < duration <= 604800)


def _duration_matches(value, duration):
    lengths = []
    for hours, minutes, seconds in re.findall(r'(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])', value):
        if int(seconds) < 60 and (not hours or int(minutes) < 60):
            lengths.append(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds))
    return len(lengths) == 1 and abs(lengths[0] - duration) <= 1


def _play_label(name, track):
    # Exact localized Play labels only: never invoke Pause, album shuffle, save,
    # context menus or a similarly named recording's controls.
    if name in ('재생하기', '재생', 'Play', '再生'):return True
    artists = ', '.join(track['artists'])
    return text_key(name) in {text_key(value) for value in (
        f"{artists}의 {track['title']} 재생하기",
        f"Play {track['title']} by {artists}",
        f"{artists}の{track['title']}を再生",
    )}


def row_matches(row, track):
    """Keep even hidden/paused duplicate rows in the ambiguity count."""
    cells = row.GetChildren()
    if len(cells) < 3:return False
    expected = text_key(track['title'] + ' ' + ', '.join(track['artists']))
    return text_key(cells[1].Name) == expected and _duration_matches(cells[-1].Name, track['duration'])


def row_play_button(row, track):
    """Require the title/credits cell and duration, not a substring of row text."""
    if not row_matches(row, track):return None
    if row.IsOffscreen:return None
    cells = row.GetChildren()
    # Chromium can insert presentation groups inside the first grid cell.
    # Stay in that cell; its sibling cells also have Save and Options buttons.
    pending = [(child, 0) for child in cells[0].GetChildren()]
    buttons = []
    inspected = 0
    while pending:
        child, depth = pending.pop()
        inspected += 1
        if inspected > 32:return None
        if child.ControlTypeName == 'ButtonControl':
            if child.IsEnabled and _play_label(child.Name, track):buttons.append(child)
        elif depth < 4:
            pending.extend((descendant, depth + 1) for descendant in child.GetChildren())
    return buttons[0] if len(buttons) == 1 else None


def page_controls(window, deadline):
    """Do not scan every saved playlist or every descendant of the album rows."""
    pending = [(window, 0)]
    while pending and time.monotonic() < deadline:
        control, depth = pending.pop()
        yield control
        kind = control.ControlTypeName
        if depth >= 30 or kind in ('DataGridControl', 'TextControl', 'ButtonControl',
                                  'HyperlinkControl', 'ImageControl', 'EditControl'):continue
        pending.extend((child, depth + 1) for child in reversed(control.GetChildren()))


def find_requested_button(window, track, auto, deadline, diagnostic=None):
    """Match one row inside the exact album grid; ambiguity never starts music."""
    matches = []
    for index, control in enumerate(page_controls(window, deadline)):
        if diagnostic is not None:diagnostic['nodes'] = index + 1
        if index >= 1600 or time.monotonic() >= deadline:return None
        if (control.ControlTypeName != 'DataGridControl'
                or text_key(control.Name) != text_key(track['album'])):continue
        if diagnostic is not None:diagnostic['album_found'] = True
        for row in control.GetChildren():
            if time.monotonic() >= deadline:return None
            if row_matches(row, track):matches.append((control, row))
    if diagnostic is not None:diagnostic['matching_rows'] = len(matches)
    if len(matches) != 1:return None
    # Re-read the row just before use; Spotify can replace its tree while loading.
    grid, row = matches[0]
    if text_key(grid.Name) != text_key(track['album']) or time.monotonic() >= deadline:return None
    button = row_play_button(row, track)
    if diagnostic is not None:
        diagnostic['play_button_found'] = button is not None
        diagnostic['row_offscreen'] = row.IsOffscreen
    return (grid, row, button) if button is not None else None


def invoke_requested_track(track, timeout=6, diagnostic=None):
    import uiautomation as auto
    from screen_lyrics import process_is_spotify
    deadline = time.monotonic() + timeout
    with auto.UIAutomationInitializerInThread():
        while time.monotonic() < deadline:
            windows = [window for window in auto.GetRootControl().GetChildren()
                       if process_is_spotify(window.ProcessId) and not window.IsOffscreen]
            if diagnostic is not None:diagnostic['spotify_windows'] = len(windows)
            matches = []
            for window in windows:
                candidate = find_requested_button(window, track, auto, deadline, diagnostic)
                if candidate is not None:matches.append(candidate)
            if len(matches) == 1 and time.monotonic() < deadline:
                # InvokePattern targets this element without changing keyboard
                # focus or dispatching a global play/pause key.
                grid, row, button = matches[0]
                pattern = button.GetInvokePattern()
                if (pattern and time.monotonic() < deadline
                        and text_key(grid.Name) == text_key(track['album'])
                        and row_play_button(row, track) is not None):
                    return bool(pattern.Invoke(waitTime=0))
            time.sleep(.15)
    return False


class TrackPlayRequest:
    """One cancellable process per click, so stuck accessibility cannot play late."""
    def __init__(self, track):
        self.process = None
        self.result = {}
        if not complete_track(track):return
        # The Windows venv executable is a redirector. Terminate the actual
        # interpreter so cancellation cannot leave its child controlling Spotify.
        environment = worker_environment(Path(__file__).resolve().parent)
        try:
            self.process = subprocess.Popen(
                worker_command(Path(__file__).resolve().parent, 'spotify_controls'),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            payload = {key: track[key] for key in ('id', 'title', 'artists', 'album', 'duration')}
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            self.process.stdin.close()
        except (OSError, ValueError):
            self.close()

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdin and not process.stdin.closed:process.stdin.close()
            if process.stdout:
                try:self.result = json.loads(process.stdout.read(16384).splitlines()[-1])
                except (OSError, ValueError, IndexError):self.result = {'invoked': False}
                process.stdout.close()


if __name__ == '__main__':
    diagnostic = {}
    try:
        raw = sys.stdin.buffer.read(100001)
        if len(raw) > 100000:raise ValueError('request size')
        track = json.loads(raw)
        success = complete_track(track) and invoke_requested_track(track, diagnostic=diagnostic)
    except Exception as error:
        success = False
        diagnostic['error_type'] = type(error).__name__
    print(json.dumps({'invoked': bool(success), **diagnostic}))
    sys.exit(0 if success else 1)
