"""Private Spotify analysis audio with read support for earlier WAV caches."""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import wave

from recording_context import recording_key


SAMPLE_RATE = 16000
CAPTURE_NAME = 'capture.sloa'
WAV_NAME = 'capture.wav'
INFO_NAME = 'capture-info.json'
REALTIME_PAUSE_NAME = 'realtime-paused'
_MAGIC = b'SLOA\x00\x01\r\n'
_HEADER = struct.Struct('<8sIH')  # Marker, sample rate, channel count. Payload is signed 16-bit LE PCM.
_MAX_SECONDS = 900


def _check_format(rate, channels):
    if not (type(rate) is int and 8000 <= rate <= 192000 and type(channels) is int and channels in (1, 2)):
        raise ValueError('분석용 음원의 샘플 형식을 확인할 수 없습니다.')


@contextmanager
def write_capture_audio(path, rate=SAMPLE_RATE, channels=1):
    """Write the private header, then accept frame-aligned PCM packets."""
    _check_format(rate, channels)
    with Path(path).open('wb') as output:
        output.write(_HEADER.pack(_MAGIC, rate, channels))
        yield output


@contextmanager
def _capture_audio(source):
    with (BytesIO(source) if isinstance(source, bytes) else Path(source).open('rb')) as stream:
        header = stream.read(_HEADER.size)
        size = stream.seek(0, os.SEEK_END)
        if header.startswith(_MAGIC) and len(header) == _HEADER.size:
            _, rate, channels = _HEADER.unpack(header)
            _check_format(rate, channels)
            frame_size = channels * 2
            frames, trailing = divmod(size - _HEADER.size, frame_size)
            if trailing or not 0 < frames <= rate * _MAX_SECONDS:
                raise ValueError('분석용 음원 길이를 확인할 수 없습니다.')
            stream.seek(_HEADER.size)
            yield (frames, rate, channels), lambda count: stream.read(count * frame_size)
        elif header.startswith(b'RIFF'):
            stream.seek(0)
            with wave.open(stream, 'rb') as audio:
                frames, rate, channels = audio.getnframes(), audio.getframerate(), audio.getnchannels()
                _check_format(rate, channels)
                if (audio.getsampwidth() != 2 or not 0 < frames <= rate * _MAX_SECONDS
                        or size < 44 + frames * channels * 2):
                    raise ValueError('분석용 음원 길이를 확인할 수 없습니다.')
                yield (frames, rate, channels), audio.readframes
        else:
            raise ValueError('분석용 음원 형식을 확인할 수 없습니다.')


def capture_frames(path):
    """Read validated frame count, sample rate and channel count without loading samples."""
    with _capture_audio(path) as (format_info, _):
        return format_info


def load_capture_samples(source):
    """Read a private or legacy capture file or immutable preview bytes as mono 16 kHz float32."""
    import numpy as np

    def packet(read_frames, count, channels):
        data = read_frames(count)
        if len(data) != count * channels * 2:
            raise ValueError('분석용 음원 데이터가 끝까지 저장되지 않았습니다.')
        return np.frombuffer(data, dtype='<i2')

    with _capture_audio(source) as ((frames, rate, channels), read_frames):
        if rate == SAMPLE_RATE and channels == 1:
            return packet(read_frames, frames, channels).astype(np.float32) / 32768

        import av
        layout = 'mono' if channels == 1 else 'stereo'
        resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
        blocks = []
        for offset in range(0, frames, 4096):
            samples = packet(read_frames, min(4096, frames - offset), channels)
            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format='s16', layout=layout)
            frame.sample_rate = rate
            blocks.extend(output.to_ndarray().ravel() for output in resampler.resample(frame))
        blocks.extend(output.to_ndarray().ravel() for output in resampler.resample(None))
        if not blocks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(blocks).astype(np.float32) / 32768


def capture_path(folder):
    folder = Path(folder)
    private = folder / CAPTURE_NAME
    return private if private.exists() else folder / WAV_NAME


def recording_directory(cache, key):
    return Path(cache) / recording_key(*key.split('\0'))


def read_capture(folder, duration):
    try:
        folder = Path(folder)
        info = json.loads((folder / INFO_NAME).read_text(encoding='utf-8'))
        path = capture_path(folder)
        frames, rate, channels = capture_frames(path)
        if path.name == CAPTURE_NAME and info.get('sample_frames') != frames:
            return None
        if (rate == SAMPLE_RATE and channels == 1 and abs(info['duration'] - duration) <= 2
                and 0 <= info['start_position'] <= 3
                and 0 <= info['clip_tail'] <= 1
                and info['start_position'] + frames / SAMPLE_RATE - info['clip_tail'] >= duration - 2):
            return info
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, wave.Error):
        pass
    return None


def find_capture(cache, key, duration):
    folder = recording_directory(cache, key)
    for second in sorted(range(round(duration) - 2, round(duration) + 3),
                         key=lambda value: abs(value - duration)):
        candidate = folder / str(second)
        if read_capture(candidate, duration) is not None:
            return candidate
    return None


def save_capture(cache, key, duration, info, samples):
    if (not 0 < len(samples) <= _MAX_SECONDS * SAMPLE_RATE
            or not 0 <= info['start_position'] <= 3
            or info.get('capture_end_reason') in ('seeked', 'app_closed')
            or info['start_position'] + len(samples) / SAMPLE_RATE - info['clip_tail'] < duration - 2):
        return
    folder = recording_directory(cache, key)
    destination = folder / str(round(duration))
    if read_capture(destination, duration) is not None:
        return destination
    if (destination / WAV_NAME).exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    # Stage on the same volume without adding the recording hash to the
    # temporary path, which can already be deep in a Windows user profile.
    with tempfile.TemporaryDirectory(prefix='.capture-', dir=cache) as temporary:
        staged = Path(temporary) / 'audio'
        staged.mkdir()
        with write_capture_audio(staged / CAPTURE_NAME) as output:
            output.write((samples.clip(-1, 32767 / 32768) * 32768).astype('<i2').tobytes())
        saved = {**info, 'duration': duration, 'sample_frames': len(samples)}
        (staged / INFO_NAME).write_text(json.dumps(saved), encoding='utf-8')
        try:
            os.rename(staged, destination)
        except OSError:
            if read_capture(destination, duration) is None:
                if not destination.exists():
                    raise
                if (destination / WAV_NAME).exists():
                    return
                destination.rename(Path(temporary) / 'invalid')
                staged.rename(destination)
    return destination


def discard_private_capture(path, cache, duration):
    """Remove one verified internal recording payload, preserving its timing metadata and legacy files."""
    path, cache = Path(path).absolute(), Path(cache).absolute()
    try:
        recording, seconds, filename = path.relative_to(cache).parts
        if (filename != CAPTURE_NAME or re.fullmatch(r'[a-f0-9]{64}', recording) is None
                or seconds != str(round(duration)) or path.resolve() != cache.resolve() / recording / seconds / filename):
            return False
        with path.open('rb') as stream:
            if stream.read(len(_MAGIC)) != _MAGIC:
                return False
        info = read_capture(path.parent, duration)
        if info is not None and info['duration'] == duration:
            path.unlink()
            return True
    except (FileNotFoundError, TypeError, ValueError, OverflowError):
        pass
    return False


def discard_retry_capture(cache, key, duration):
    """Remove the private retry audio for this exact recording and duration."""
    path = recording_directory(cache, key) / str(round(duration)) / CAPTURE_NAME
    return discard_private_capture(path, cache, duration)
