"""Run local pyshiro HSMM refinement in the user's separate Python runtime."""

import base64
from dataclasses import dataclass
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Thread

from app_paths import prepare_external_processes


_RATE = 16_000
_SAMPLES_PER_MS = _RATE // 1000
_PADDING_MS = 900
_MAX_WINDOW_MS = 20_000


class PyshiroUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RefinedBoundary:
    start_ms: int
    end_ms: int
    window_start_ms: int
    window_end_ms: int
    score: float


# Load the user's installation and checkpoint once. stdout carries only JSON.
_WORKER = r'''
import base64
from contextlib import redirect_stdout
import json
import math
import sys

def send(**response):
    print(json.dumps(response, allow_nan=False), flush=True)

try:
    with redirect_stdout(sys.stderr):
        import numpy as np
        import pyshiro
        from pyshiro.labels import HOP_TIME, segments_to_phoneme_intervals

        model = pyshiro.load_hsmm(sys.argv[1])
        phonemap = pyshiro.load_phonemap(sys.argv[2])
        if 'pau' not in phonemap or model.nstream != 3 or model.ndim != 12:
            raise ValueError('incompatible pyshiro checkpoint and phonemap')
        for entry in phonemap.values():
            if len(entry['states']) != 3 or any(
                not (0 <= state['dur'] < model.nduration)
                or len(state['out']) != model.nstream
                or any(not 0 <= output < model.nstate for output in state['out'])
                for state in entry['states']
            ):
                raise ValueError('incompatible pyshiro checkpoint and phonemap states')
        table = pyshiro.load_table()
except Exception as error:
    send(error=f'{type(error).__name__}: {error}')
    raise SystemExit(1)

send(ready=True)
for line in sys.stdin:
    request = json.loads(line)
    phones = ['pau', *request['phones'], 'pau']
    try:
        with redirect_stdout(sys.stderr):
            # The upstream table uses uppercase vowels for ー. Preserve them
            # when the table covers the same phone sequence as OpenJTalk.
            kana = pyshiro.text_to_phonemes(request['reading'], table)
            if [p.lower() if p in {'A', 'I', 'U', 'E', 'O'} else p for p in kana] == phones:
                phones = kana
            supported = all(phone in phonemap for phone in phones)
            if supported:
                audio = np.frombuffer(base64.b64decode(request['pcm'], validate=True), dtype='<f4')
                streams = pyshiro.extract_mfcc(audio, 16000)
                states = pyshiro.build_state_sequence(phones, phonemap, len(streams[0]))
                segments, score = pyshiro.forced_align_2pass(model, streams, states, nodur_phonemes={'pau'})
                intervals = segments_to_phoneme_intervals(phones, segments)[1:-1]
        if not supported or not intervals or any(start >= end for start, end, _ in intervals):
            send(candidate=None)
            continue
        normalized_score = score / len(streams[0])
        if not math.isfinite(normalized_score):
            send(candidate=None)
            continue
        send(candidate=[round(intervals[0][0] * HOP_TIME * 1000),
                        round(intervals[-1][1] * HOP_TIME * 1000), normalized_score])
    except RuntimeError:
        send(candidate=None)
    except Exception as error:
        send(error=f'{type(error).__name__}: {error}')
'''


def _read_output(stream, responses):
    try:
        for line in stream:
            responses.put(json.loads(line))
    except (ValueError, OSError):
        pass
    finally:
        responses.put(None)


class PyshiroRefiner:
    def __init__(self, python_executable, checkpoint, phonemap):
        paths = [Path(value).resolve() for value in (python_executable, checkpoint, phonemap)]
        for path in paths:
            if not path.is_file():
                raise PyshiroUnavailable(f'pyshiro file missing: {path}')
        self._command = [str(paths[0]), '-I', '-X', 'utf8', '-u', '-c', _WORKER, str(paths[1]), str(paths[2])]
        self._process = None
        self._responses = None

    def _receive(self, timeout):
        try:
            response = self._responses.get(timeout=timeout)
        except Empty:
            raise PyshiroUnavailable('pyshiro timed out') from None
        if not isinstance(response, dict):
            raise PyshiroUnavailable('pyshiro process stopped')
        if 'error' in response:
            raise PyshiroUnavailable(response['error'])
        return response

    def _start(self):
        if self._process is not None:
            return
        prepare_external_processes()
        self._process = subprocess.Popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self._responses = Queue()
        Thread(target=_read_output, args=(self._process.stdout, self._responses), daemon=True).start()
        if not self._receive(30).get('ready'):
            raise PyshiroUnavailable('pyshiro failed to initialize')

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()

    def refine(self, audio, phones, start_ms, end_ms, *, reading='', start_limit_ms=0, end_limit_ms=None):
        """Return a candidate in recording-relative milliseconds for later CTC validation."""
        import numpy as np

        duration_ms = len(audio) * 1000 // _RATE
        upper = duration_ms if end_limit_ms is None else min(duration_ms, end_limit_ms)
        window_start = max(0, start_limit_ms, start_ms - _PADDING_MS)
        window_end = min(upper, end_ms + _PADDING_MS)
        if not phones or not window_start <= start_ms < end_ms <= window_end:
            return None
        if window_end - window_start > _MAX_WINDOW_MS:
            return None
        clip = np.asarray(audio[window_start * _SAMPLES_PER_MS:window_end * _SAMPLES_PER_MS], dtype='<f4')
        if clip.ndim != 1 or not np.isfinite(clip).all() or np.max(np.abs(clip)) < 1e-5:
            return None

        try:
            self._start()
            request = json.dumps({'pcm': base64.b64encode(clip.tobytes()).decode('ascii'),
                                  'phones': list(phones), 'reading': reading})
            self._process.stdin.write(request + '\n')
            self._process.stdin.flush()
            candidate = self._receive(30).get('candidate')
        except OSError as error:
            self.close()
            raise PyshiroUnavailable(str(error)) from error
        except PyshiroUnavailable:
            self.close()
            raise
        if candidate is None:
            return None
        start, end, score = candidate
        if not 0 <= start < end <= window_end - window_start:
            return None
        return RefinedBoundary(window_start + start, window_start + end, window_start, window_end, score)
