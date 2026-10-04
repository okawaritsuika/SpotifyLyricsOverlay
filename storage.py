"""Local library/settings with atomic writes and separate per-song timing."""
import json
import math
import os
from pathlib import Path
import tempfile
import time

from lyrics import Song


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows readers can briefly prevent rename even after our own handle
        # closes. Retry only those errors, using the same fully flushed file.
        for delay in (.01, .02, .04, None):
            try:
                os.replace(temp, path)
                break
            except PermissionError as error:
                if (os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33)
                        or delay is None):
                    raise
                time.sleep(delay)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'{Path(path).name} 파일을 읽지 못했습니다. '
                         '원본 파일을 보존한 상태로 복구가 필요합니다.') from error


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):return False
    try:return math.isfinite(value)
    except OverflowError:return False


def validate_times(times, count):
    if not isinstance(times, list) or len(times) != count:
        raise ValueError('가사 구절 수와 시간표가 일치하지 않습니다.')
    previous = -1.0
    for stamp in times:
        if stamp is None:
            continue
        if not _finite_number(stamp) or stamp < 0 or stamp <= previous:
            raise ValueError('시작 시간은 0 이상이어야 하고 앞 구절보다 뒤에 있어야 합니다.')
        previous = stamp


def validate_offset(offset):
    if not _finite_number(offset):
        raise ValueError('저장된 가사 보정값이 올바르지 않습니다.')


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def library(self):
        value = read_json(self.directory / 'library.json', [])
        try:
            if not isinstance(value, list):raise ValueError()
            for song in value:
                if not isinstance(song, dict):raise ValueError()
                if any(not isinstance(song.get(field), str) for field in ('id', 'title', 'artist', 'album')):
                    raise ValueError()
                if not isinstance(song.get('lines'), list):raise ValueError()
                for line in song['lines']:
                    if (not isinstance(line, dict) or
                            any(not isinstance(line.get(field), str) for field in ('original', 'reading', 'korean'))):
                        raise ValueError()
            return [Song.from_dict(song) for song in value]
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError('library.json 파일 형식을 확인할 수 없습니다. 원본은 보존했습니다.') from error

    def save_library(self, songs):
        atomic_json(self.directory / 'library.json', [s.to_dict() for s in songs])

    def settings(self):
        value = read_json(self.directory / 'settings.json', {})
        if not isinstance(value, dict):
            raise ValueError('settings.json 파일 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        return value

    def save_settings(self, settings):
        atomic_json(self.directory / 'settings.json', settings)

    def timing(self, song):
        value = read_json(self.directory / 'timings' / (song.id + '.json'), {})
        if not isinstance(value, dict):
            raise ValueError('저장된 가사 시간표 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        if value.get('fingerprint') != song.fingerprint:
            return {'times': [None] * len(song.lines), 'offset': 0.0, 'source': ''}
        validate_times(value.get('times'), len(song.lines))
        offset = value.get('offset', 0)
        validate_offset(offset)
        value['offset'] = offset
        if value.get('origins') is None and value.get('source')=='직접 기록 / 음원 분석':
            from timing_quality import recover_legacy_origins
            try:
                draft=read_json(self.directory/'audio_drafts'/(song.id+'.json'),{})
                before=read_json(self.directory/'audio_backups'/(song.id+'.json'),{})
                value,_=recover_legacy_origins(value,draft,before)
            except (ValueError,TypeError,AttributeError):
                pass  # An auxiliary draft must not prevent the saved timeline loading.
        return value

    def save_timing(self, song, timing):
        if not isinstance(timing, dict):
            raise ValueError('저장할 가사 시간표 형식이 올바르지 않습니다.')
        validate_times(timing.get('times'), len(song.lines))
        validate_offset(timing.get('offset', 0))
        atomic_json(self.directory / 'timings' / (song.id + '.json'),
                    {**timing, 'fingerprint': song.fingerprint})
