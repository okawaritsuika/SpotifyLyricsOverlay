"""Read only user-selected audio folders and keep explicit song/file links locally."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import unicodedata
import wave

from storage import atomic_json

try:
    import mutagen
except ImportError:
    mutagen = None


AUDIO_EXTENSIONS = frozenset({'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.ogg', '.oga',
                              '.opus', '.wav', '.wave', '.aiff', '.aif', '.wma', '.ape',
                              '.wv', '.mpc', '.tta'})
MAX_FILES = 20_000
MAX_ENTRIES = 100_000
MAX_DEPTH = 32
MAX_DATABASE_BYTES = 32 * 1024 * 1024


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _text(value):
    if not isinstance(value, str):return ''
    return ' '.join(''.join(char for char in value
                           if char.isspace() or unicodedata.category(char) not in ('Cc', 'Cf')).split())[:2000]


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):return None
    return float(value) if 0 < value <= 604800 else None


def _file_id(path):
    return hashlib.sha256(os.path.normcase(str(path.absolute())).encode('utf-8')).hexdigest()[:32]


def _plain_path(path):
    """Do not enter directory junctions, symlinks, or other reparse points."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        return None
    return info


def _within(path, folder):
    try:path.relative_to(folder)
    except ValueError:return False
    return True


def _safe_path(path, folders, directory=False):
    for folder in folders:
        if not _within(path, folder):continue
        try:
            # Check ancestors first: even lstat(file) follows a junction in its parents.
            current = folder
            for part in ('', *path.relative_to(folder).parts):
                current = current / part
                info = _plain_path(current)
                if info is None:break
                expected_directory = current != path or directory
                if not (stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode)):break
                if current == path:return True
        except OSError:
            continue
    return False


def _safe_file(path, folders):
    return _safe_path(path, folders)


def _values(tags, *keys):
    for key in keys:
        value = tags.get(key)
        if value is None:continue
        value = getattr(value, 'text', value)
        values = value if isinstance(value, (list, tuple)) else [value]
        result = list(dict.fromkeys(_text(item if isinstance(item, str) else str(item))
                                   for item in values[:200]))
        result = [item for item in result if item]
        if result:return result
    return []


def _metadata(path):
    title = album = ''
    isrc = ''
    artists = []
    duration = None
    if mutagen is not None:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:raise ValueError('지원되는 음원 내용을 찾지 못했습니다.')
        tags = audio.tags or {}
        title = next(iter(_values(tags, 'title', 'TIT2', '\xa9nam', 'Title')), '')
        artists = _values(tags, 'artist', 'TPE1', '\xa9ART', 'Author')
        album = next(iter(_values(tags, 'album', 'TALB', '\xa9alb', 'WM/AlbumTitle')), '')
        isrc = next(iter(_values(tags, 'isrc', 'TSRC', 'WM/ISRC')), '')
        duration = _duration(getattr(audio.info, 'length', None))
    elif path.suffix.lower() in ('.wav', '.wave'):
        with wave.open(str(path), 'rb') as audio:
            duration = _duration(audio.getnframes() / audio.getframerate())
    quality = 'tags' if title and artists else 'partial' if title or artists or album else 'filename'
    if not title:
        title = _text(re.sub(r'^\d{1,3}[\s._-]+', '', path.stem)) or _text(path.stem)
    record = {'id': _file_id(path), 'path': str(path), 'title': title, 'artists': artists,
              'album': album, 'duration': duration, 'metadata_quality': quality}
    isrc = isrc.replace('-', '').upper()
    if re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', isrc):
        record['isrc'] = isrc
    return record


def _song_value(song, name, default=None):
    return song.get(name, default) if isinstance(song, dict) else getattr(song, name, default)


def _recording_tags(song):
    artists = _song_value(song, 'artists') or [_song_value(song, 'artist', '')]
    return (_key(_text(_song_value(song, 'title', ''))), _key(_text(_song_value(song, 'album', ''))),
            {_key(_text(name)) for name in artists if _text(name)},
            _text(_song_value(song, 'isrc', '')).replace('-', '').upper())


def _matches_recording_tags(record, tags):
    title, album, artists, isrc = tags
    if isrc and record.get('isrc'):
        return record['isrc'] == isrc
    return (record['metadata_quality'] == 'tags' and bool(title and album and artists)
            and _key(record['title']) == title and _key(record['album']) == album
            and {_key(name) for name in record['artists']} == artists)


def recording_tags_match(path, track):
    """Check whether the local file's tags establish the playlist recording."""
    if mutagen is None:
        return False
    try:
        record = _metadata(path)
    except (OSError, ValueError, mutagen.MutagenError):
        return False
    return _matches_recording_tags(record, _recording_tags(track))


class LocalLibrary:
    """Scans are read-only; opening a file happens only through explicit open_file."""
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'local_library.json'
        self.files = []
        self.links = {}
        self.folders = []
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._load()

    @property
    def metadata_available(self):
        return mutagen is not None

    def _load(self):
        try:
            with self.path.open('rb') as handle:raw = handle.read(MAX_DATABASE_BYTES + 1)
        except FileNotFoundError:return
        try:
            if len(raw) > MAX_DATABASE_BYTES:raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('version') != 1:raise ValueError()
            files, links, folders = value['files'], value['links'], value['folders']
            if not isinstance(files, list) or len(files) > MAX_FILES:raise ValueError()
            if not isinstance(links, dict) or not isinstance(folders, list):raise ValueError()
            if any(not isinstance(folder, str) or not Path(folder).is_absolute() for folder in folders):raise ValueError()
            ids = set()
            for record in files:
                if not isinstance(record, dict):raise ValueError()
                file_path = Path(record['path'])
                if not file_path.is_absolute() or file_path.suffix.lower() not in AUDIO_EXTENSIONS:raise ValueError()
                if record['id'] != _file_id(file_path) or record['id'] in ids:raise ValueError()
                if any(not isinstance(record.get(name), str) for name in ('title', 'album')):raise ValueError()
                if not isinstance(record.get('artists'), list) or any(not isinstance(item, str) for item in record['artists']):raise ValueError()
                if record.get('metadata_quality') not in ('tags', 'partial', 'filename'):raise ValueError()
                if record.get('duration') is not None and _duration(record['duration']) is None:raise ValueError()
                if 'isrc' in record and (not isinstance(record['isrc'], str)
                        or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', record['isrc'])):raise ValueError()
                if not any(_within(file_path, Path(folder)) for folder in folders):raise ValueError()
                ids.add(record['id'])
            if any(not isinstance(song_id, str) or not song_id or not isinstance(ids_, list)
                   or any(not isinstance(file_id, str) or file_id not in ids for file_id in ids_)
                   for song_id, ids_ in links.items()):raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise ValueError('local_library.json을 읽지 못했습니다. 기존 파일을 보존한 채 확인해 주세요.') from error
        self.files, self.links, self.folders = files, links, folders

    def _save(self, files, links, folders):
        value = {'version': 1, 'files': files, 'links': links, 'folders': folders}
        if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_DATABASE_BYTES:
            raise ValueError('로컬 음원 목록이 저장 한도를 넘었습니다.')
        atomic_json(self.path, value)
        self.files, self.links, self.folders = files, links, folders

    def scan(self, folder, progress=None, cancel=None, *, persist=True):
        """progress receives a stats copy; cancellation leaves memory and disk unchanged.

        A completed bounded scan adds/refreshes records. Missing files stay in the
        index so explicit links survive disconnected drives, moves, and rescans.
        """
        if not self._scan_lock.acquire(blocking=False):
            raise ValueError('이미 로컬 음원 폴더를 확인하고 있습니다.')
        try:return self._scan(folder, progress, cancel, persist)
        finally:self._scan_lock.release()

    def _scan(self, folder, progress, cancel, persist):
        folder = Path(os.path.abspath(folder))
        try:info = _plain_path(folder)
        except OSError as error:raise ValueError('선택한 음원 폴더를 열지 못했습니다.') from error
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ValueError('바로가기나 연결 폴더 대신 실제 음원 폴더를 선택해 주세요.')
        stats = {'folder': str(folder), 'scanned': 0, 'visited': 0, 'added': 0, 'updated': 0,
                 'skipped': 0, 'errors': 0, 'cancelled': False, 'truncated': False,
                 'metadata_available': self.metadata_available}
        cancelled = lambda: bool(cancel() if callable(cancel) else cancel.is_set()) if cancel is not None else False
        emit = lambda: progress(dict(stats)) if progress else None
        found = {}
        pending = [(folder, 0)]
        emit()
        while pending and not stats['truncated']:
            if cancelled():stats['cancelled'] = True;break
            directory, depth = pending.pop()
            try:
                if not _safe_path(directory, [folder], directory=True):stats['skipped'] += 1;continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancelled():stats['cancelled'] = True;break
                        stats['visited'] += 1
                        if stats['visited'] > MAX_ENTRIES:
                            stats['truncated'] = True;break
                        if stats['visited'] % 25 == 0:emit()
                        path = Path(entry.path)
                        try:
                            info = _plain_path(path)
                            if info is None:stats['skipped'] += 1;continue
                            if stat.S_ISDIR(info.st_mode):
                                if depth < MAX_DEPTH:pending.append((path, depth + 1))
                                else:stats['skipped'] += 1;stats['truncated'] = True
                                continue
                            if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in AUDIO_EXTENSIONS:continue
                            stats['scanned'] += 1
                            if not info.st_size or not _safe_file(path, [folder]):
                                stats['skipped'] += 1;continue
                            record = _metadata(path)
                            found[record['id']] = record
                        except Exception:
                            # One malformed file or decoder failure cannot abort an album.
                            stats['errors'] += 1;stats['skipped'] += 1
                        if len(found) >= MAX_FILES:
                            stats['truncated'] = True;break
                        if path.suffix.lower() in AUDIO_EXTENSIONS:emit()
            except OSError:
                stats['errors'] += 1
            if stats['cancelled']:break
        if cancelled():stats['cancelled'] = True
        if not stats['cancelled']:
            with self._lock:
                if cancelled():
                    stats['cancelled'] = True;emit();return stats
                records = {record['id']: record for record in self.files}
                for file_id, record in found.items():
                    if file_id not in records:
                        if len(records) >= MAX_FILES:
                            stats['truncated'] = True;continue
                        stats['added'] += 1
                    elif records[file_id] != record:stats['updated'] += 1
                    records[file_id] = record
                files = sorted(records.values(), key=lambda record: (_key(record['title']), record['path']))
                folders = list(dict.fromkeys([*self.folders, str(folder)]))
                if persist:self._save(files, self.links.copy(), folders)
                else:self.files, self.folders = files, folders
        emit()
        return stats

    def link(self, song_id, file_id):
        if not isinstance(song_id, str) or not song_id.strip():raise ValueError('연결할 곡을 선택해 주세요.')
        with self._lock:
            if not any(record['id'] == file_id for record in self.files):raise ValueError('목록에서 음원 파일을 선택해 주세요.')
            links = self.links.copy()
            links[song_id] = list(dict.fromkeys([*links.get(song_id, []), file_id]))
            self._save(self.files, links, self.folders)

    def unlink(self, song_id):
        with self._lock:
            if song_id not in self.links:return
            links = self.links.copy();links.pop(song_id)
            self._save(self.files, links, self.folders)

    def _with_exists(self, record):
        return {**record, 'artists': list(record['artists']),
                'exists': _safe_file(Path(record['path']), [Path(folder) for folder in self.folders])}

    def for_song(self, song_id):
        with self._lock:
            ids = set(self.links.get(song_id, []))
            return [self._with_exists(record) for record in self.files if record['id'] in ids]

    def candidates(self, song):
        title = _text(_song_value(song, 'title', ''))
        if not title:return []
        artists = _song_value(song, 'artists')
        if artists is None:artists = [_song_value(song, 'artist', '')]
        if isinstance(artists, str):artists = [artists]
        artists = {_key(artist) for artist in artists if isinstance(artist, str) and artist.strip()}
        album = _text(_song_value(song, 'album', ''))
        duration = _duration(_song_value(song, 'duration'))
        result = []
        for record in self.files:
            # Keep every version/remix/performer suffix; never match only the base title.
            if _key(record['title']) != _key(title):continue
            file_artists = {_key(artist) for artist in record['artists']}
            if artists and file_artists and not artists.intersection(file_artists):continue
            if duration and record['duration'] and abs(duration - record['duration']) > 2:continue
            candidate = self._with_exists(record)
            if not candidate['exists']:continue
            score = 50
            reasons = ['버전명을 포함한 전체 제목 일치']
            if artists and file_artists:
                score += 25 if artists == file_artists else 15
                reasons.append('가수 일치' if artists == file_artists else '참여 가수 일부 일치 · 확인 필요')
            else:reasons.append('가수 태그 미확인 · 직접 확인 필요')
            if album and record['album']:
                if _key(album) == _key(record['album']):score += 15;reasons.append('앨범 일치')
                else:reasons.append('앨범 다름 · 직접 확인 필요')
            if duration and record['duration']:score += 10;reasons.append('재생 길이 2초 이내')
            if record['metadata_quality'] == 'filename':
                score -= 20;reasons.append('파일명 기준 · 음원 태그 미확인')
            result.append({**candidate, 'score': score, 'reasons': reasons})
        return sorted(result, key=lambda record: (-record['score'], record['path']))

    def exact_recordings(self, song):
        """Select full versions by a verified tag match, never by a bare filename."""
        tags = _recording_tags(song)
        duration = _duration(_song_value(song, 'expected_duration'))
        if not duration:
            return []
        result = []
        for record in self.files:
            if not record['duration'] or abs(record['duration'] - duration) > 2:
                continue
            if _matches_recording_tags(record, tags) and self._with_exists(record)['exists']:
                result.append(record)
        return result

    def open_file(self, file_id):
        """Open one explicitly chosen indexed audio file in the Windows default player."""
        record = next((record for record in self.files if record['id'] == file_id), None)
        if record is None:raise ValueError('재생할 로컬 음원을 선택해 주세요.')
        path = Path(record['path'])
        if path.suffix.lower() not in AUDIO_EXTENSIONS or not self._with_exists(record)['exists']:
            raise ValueError('음원 파일이 없거나 연결 폴더로 바뀌었습니다. 폴더를 다시 확인해 주세요.')
        if not hasattr(os, 'startfile'):raise OSError('기본 플레이어 열기는 Windows에서 사용할 수 있습니다.')
        os.startfile(str(path), 'open')
