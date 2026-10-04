"""Offline song dictionary with independent, atomic public-data updates.

Catalog files contain public facts only. Favorites, local paths, Spotify account
state and manual recording mappings belong to the user's separate library.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile
from song_aliases import search_aliases

from catalog_sources import (CatalogCancelled, IMAS_URL, SERIES, catalog_queries, check_cancel,
                             enrich_performers, enrich_reviewed_releases, fetch_bytes, fetch_musicbrainz,
                             key, normalize_musicbrainz, notify, parse_imasparql,
                             reclassify_unknown_series, reclassify_reviewed_kinds)


ROOT = Path(__file__).resolve().parent
RESOURCE_PATH = ROOT / 'resources' / 'song_catalog.json'
MAX_CATALOG_BYTES = 96 * 1024 * 1024
MAX_SONGS = 40000
MAX_PERFORMERS = 10000
SERIES_LABELS = {'765': '765 프로', 'million': '밀리언 라이브',
                 'cinderella': '신데렐라 걸즈', 'sidem': 'SideM',
                 'shiny': '샤이니 컬러즈', 'gakuen': '학원 아이돌마스터',
                 'other': '기타 · 시리즈 미확인'}


def _string(value, maximum=4000):
    return isinstance(value, str) and len(value) <= maximum and '\0' not in value


def _strings(value, maximum=1000):
    return (isinstance(value, list) and len(value) <= maximum
            and all(_string(item) for item in value))


def _duration(value):
    # Check bounds before any float conversion, including unusually large ints.
    return value is None or (not isinstance(value, bool)
        and isinstance(value, (int, float)) and 0 < value <= 86400)


def _url(value):
    if not _string(value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _valid_song(value):
    if (not isinstance(value, dict) or not _string(value.get('id'), 1000)
            or not value['id'].strip() or not _string(value.get('title'))
            or not value['title'].strip() or not _strings(value.get('artists'), 300)
            or not isinstance(value.get('series'), list) or not value['series']
            or any(series not in SERIES for series in value['series'])
            or not _url(value.get('source_url', value.get('url', '')))):
        return False
    if any(field in value and not _strings(value[field])
           for field in ('aliases', 'performer_aliases', 'performer_ids', 'groups', 'missing', 'artists_original')):
        return False
    if 'album' in value and not _string(value['album']):
        return False
    if any(field in value and not _string(value[field])
           for field in ('first_release_date', 'first_release_date_original', 'artist_credit', 'artist_credit_original')):
        return False
    if ('artist_source_urls' in value and (not _strings(value['artist_source_urls'], 10)
            or not all(_url(url) for url in value['artist_source_urls']))
            or 'artist_source_kind' in value and value['artist_source_kind'] != 'official_label'):
        return False
    if ('first_release_date_source_url' in value and not _url(value['first_release_date_source_url'])
            or 'first_release_date_source_kind' in value
            and value['first_release_date_source_kind'] not in ('official_label', 'musicbrainz')):
        return False
    if not _duration(value.get('duration')):
        return False
    for field in ('releases', 'recordings'):
        entries = value.get(field, [])
        if not isinstance(entries, list) or len(entries) > 1000:
            return False
        for entry in entries:
            if (not isinstance(entry, dict) or any(name in entry and not _string(entry[name])
                    for name in ('id', 'mbid', 'title', 'album', 'date', 'date_original', 'url', 'source_url'))
                    or any(name in entry and not _strings(entry[name]) for name in ('artists', 'isrcs', 'artists_original'))
                    or 'artist_source_urls' in entry and (not _strings(entry['artist_source_urls'], 10)
                        or not all(_url(url) for url in entry['artist_source_urls']))
                    or 'artist_source_kind' in entry and entry['artist_source_kind'] != 'official_label'
                    or 'formats' in entry and not _strings(entry['formats'], 10)
                    or any(name in entry and not _url(entry[name])
                           for name in ('date_source_url', 'format_source_url'))
                    or 'date_source_kind' in entry and entry['date_source_kind'] not in ('official_label', 'musicbrainz')
                    or not _duration(entry.get('duration'))):
                return False
    if 'kind' in value and value['kind'] not in ('song', 'instrumental', 'spoken', 'unknown'):
        return False
    return True


def read_catalog(path):
    """Reject a malformed snapshot as a whole, preserving the last working one."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or not _string(value.get('generated_at'))
                or not isinstance(value.get('sources'), list)
                or len(value['sources']) > 100
                or any(not isinstance(s, dict) or not _string(s.get('id', '')) for s in value['sources'])
                or not isinstance(value.get('songs'), list)
                or not 0 < len(value['songs']) <= MAX_SONGS
                or any(not _valid_song(song) for song in value['songs'])
                or len({song['id'] for song in value['songs']}) != len(value['songs'])):
            return None
        performers = value.get('performers', [])
        if (not isinstance(performers, list) or len(performers) > MAX_PERFORMERS
                or any(not isinstance(p, dict) or not _string(p.get('id'))
                       or not _string(p.get('name')) or p.get('kind') not in ('idol', 'group')
                       or not _strings(p.get('aliases', [])) or not _strings(p.get('members', []))
                       for p in performers)):
            return None
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _reviewed_unit_alias(performers):
    """Connect one documented unit on fresh copies, without a global name rule."""
    people = deepcopy(performers)
    identity = 'imas:ニュージェネレーション'
    expected = {'name': 'ニュージェネレーション', 'kind': 'group', 'series': ['cinderella'],
                'members': ['島村卯月', '渋谷凛', '本田未央'],
                'members_ids': ['imas:Shimamura_Uzuki', 'imas:Shibuya_Rin', 'imas:Honda_Mio']}
    matches = [person for person in people if person.get('id') == identity]
    if len(matches) != 1:return people, None
    person = matches[0]
    if any(person.get(field) != value for field, value in expected.items()):return people, None
    names = {key(value) for value in ('ニュージェネレーション', 'new generations',
                                      'newgenerations', 'ニュージェネレーションズ', '뉴 제너레이션즈')}
    for other in people:
        if other is person:continue
        owned = [other.get('name', ''), other.get('ko', ''), other.get('display', '')] + other.get('aliases', [])
        if names.intersection(key(value) for value in owned if isinstance(value, str)):return people, None
    sources = person.get('source_urls', [])
    if not isinstance(sources, list) or not all(isinstance(value, str) for value in sources):return people, None
    person['aliases'] = list(dict.fromkeys(person.get('aliases', []) + ['new generations']))
    person['source_urls'] = list(dict.fromkeys(sources + [
        'https://imas-cinderella.com/character/',
        'https://cinderella-library.idolmaster-official.jp/history/detail/?p=20150422_2']))
    return people, identity


class SongCatalog:
    def __init__(self, resource_path=None, update_path=None):
        self.resource_path = Path(resource_path) if resource_path else RESOURCE_PATH
        self.update_path = Path(update_path) if update_path else None
        self.songs = []
        self.performers = []
        self.sources = []
        self.generated_at = ''
        self.status = 'empty'
        self._index = []
        self.reload()

    def reload(self):
        base = read_catalog(self.resource_path)
        update = read_catalog(self.update_path) if self.update_path else None
        chosen = update or base
        if chosen is None:
            # Existing in-memory data remains usable after a broken update file.
            self.status = 'retained' if self.songs else 'empty'
            return False
        # Update files are complete snapshots. Merge by stable ID to keep older
        # bundled entries if a source has disappeared or a user returns offline.
        songs = {s['id']: s for s in base['songs']} if base else {}
        songs.update({s['id']: s for s in chosen['songs']})
        reclassify_unknown_series(songs.values())
        reclassify_reviewed_kinds(songs.values())
        corrected_artists = enrich_reviewed_releases(songs.values())
        self.songs = sorted(songs.values(), key=lambda song: (key(song['title']), song['id']))
        self.performers, reviewed_unit = _reviewed_unit_alias(
            chosen.get('performers', base.get('performers', []) if base else []))
        derived_targets = {song['id']: song for song in corrected_artists}
        if reviewed_unit:
            derived_targets.update((song['id'], song) for song in self.songs
                if song['artists'] == ['new generations']
                and not set(song.get('performer_ids', [])) - {reviewed_unit})
        if derived_targets:
            enrich_performers(derived_targets.values(), deepcopy(self.performers),
                              names_directory=ROOT / 'resources' / 'artist_names')
        self.sources = chosen['sources']
        self.generated_at = chosen['generated_at']
        self.status = 'updated' if update else 'bundled'
        self._index = []
        for song in self.songs:
            performer = key(' '.join(song['artists'] + song.get('groups', [])
                                    + song.get('performer_aliases', [])
                                    + song.get('performer_ids', [])))
            full = key(' '.join([song['title'], song.get('album', ''), performer]
                               + song.get('aliases', []) + list(search_aliases(song))))
            self._index.append((song, full, performer))
        return True

    def search(self, query='', series='', performer=''):
        words = [key(word) for word in str(query).split() if word.strip()]
        person = key(performer)
        return [song for song, text, people in self._index
                if (not series or series in song['series'])
                and (not person or person in people)
                and all(word in text for word in words)]


def _write_atomic(destination, payload, cancel=None):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError('카탈로그 파일 크기를 초과했습니다.')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='song-catalog-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancel)
        if read_catalog(temporary) is None:
            raise ValueError('새 카탈로그 검증에 실패하여 기존 파일을 유지합니다.')
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_catalog(destination, progress=None, cancel=None, *, source_directory=None,
                    resume=False, fetcher=fetch_bytes):
    """Refresh public facts; partial source failures retain their previous entries.

    `source_directory` and `resume` are build-tool options. The desktop UI only
    supplies destination/progress/cancel and always performs fresh HTTP requests.
    A cancelled refresh never replaces the working catalog.
    """
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    previous = read_catalog(destination) or read_catalog(RESOURCE_PATH) or {
        'schema_version': 1, 'generated_at': '', 'sources': [], 'songs': [], 'performers': []}
    songs = {s['id']: deepcopy(s) for s in previous['songs']}
    sources = {s.get('id'): deepcopy(s) for s in previous['sources'] if s.get('id')}
    performers = deepcopy(previous.get('performers', []))
    errors = []
    successful = []
    directory = Path(source_directory) if source_directory else None
    try:
        check_cancel(cancel)
        notify(progress, '아이돌·유닛 사전 업데이트 중…')
        try:
            archive = directory / 'imasparql-source.zip' if directory else None
            data = archive.read_bytes() if resume and archive and archive.exists() else fetcher(IMAS_URL, cancel=cancel)
            incoming, imported_performers = parse_imasparql(data, fetched_at=now)
            if not imported_performers:
                raise ValueError('아이돌·유닛 데이터가 비어 있습니다.')
            for song in incoming:
                song['type'] = song['kind']
                songs[song['id']] = song
            # Removed RDF entries may be aliases or a temporary upstream omission.
            combined = {p['id']: p for p in performers}
            combined.update({p['id']: p for p in imported_performers})
            performers = list(combined.values())
            sources['imasparql'] = {'id': 'imasparql', 'title': 'im@sparql 공개 RDF',
                                   'url': 'https://github.com/imas/imasparql', 'license': 'MIT',
                                   'fetched_at': now, 'status': 'ok', 'recordings': len(incoming),
                                   'performers': len(imported_performers),
                                   'scope': '아이돌·유닛 관계와 제한된 녹음 메타데이터; 전곡 보장 없음'}
            successful.append('imasparql')
            notify(progress, f'아이돌·유닛 {len(imported_performers):,}개 확인')
        except CatalogCancelled:
            raise
        except (OSError, ValueError, KeyError, BadZipFile, ParseError) as exc:
            errors.append('im@sparql: ' + str(exc)[:300])
            sources.setdefault('imasparql', {'id': 'imasparql', 'title': 'im@sparql'})['status'] = 'retained_after_error'
        check_cancel(cancel)
        try:
            cached = directory / 'musicbrainz-recordings.json' if directory else None
            if resume and cached and cached.exists():
                document = json.loads(cached.read_text(encoding='utf-8'))
                recordings, queries = document['recordings'], document['queries']
            else:
                recordings, queries = fetch_musicbrainz(
                    cache_directory=directory / '.cache' if directory else None,
                    resume=resume, progress=progress, cancel=cancel, fetcher=fetcher,
                    queries=catalog_queries(performers))
            incoming = normalize_musicbrainz(recordings, fetched_at=now)
            if not incoming:
                raise ValueError('공식 음반에 연결된 MusicBrainz 녹음이 없습니다.')
            for song in incoming:
                song['type'] = song['kind']
                song['recordings'][0]['mbid'] = song['id'].removeprefix('mb:')
                songs[song['id']] = song
            sources['musicbrainz'] = {'id': 'musicbrainz', 'title': 'MusicBrainz 핵심 메타데이터',
                                     'url': 'https://musicbrainz.org', 'license': 'CC0-1.0',
                                     'fetched_at': now, 'status': 'ok',
                                     'recordings': len(incoming), 'queries': queries,
                                     'scope': '아이마스 음반·283 Production·初星学園·전 시리즈 개별 아이돌·검증된 대표 유닛 크레딧 검색; 공식 상태 발매와 명시적 프랜차이즈·검토한 정확 녹음 정체성 확인, 전곡 보장 없음',
                                     'license_url': 'https://musicbrainz.org/doc/About/Data_License'}
            successful.append('musicbrainz')
        except CatalogCancelled:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append('MusicBrainz: ' + str(exc)[:300])
            sources.setdefault('musicbrainz', {'id': 'musicbrainz', 'title': 'MusicBrainz'})['status'] = 'retained_after_error'
        check_cancel(cancel)
        if not successful:
            return {'updated': False, 'cancelled': False, 'songs': len(previous['songs']),
                    'errors': errors, 'message': '출처 연결 실패로 기존 카탈로그를 유지했습니다.'}
        result_songs = list(songs.values())
        reclassify_unknown_series(result_songs)
        reclassify_reviewed_kinds(result_songs)
        enrich_reviewed_releases(result_songs)
        enrich_performers(result_songs, performers, names_directory=ROOT / 'resources' / 'artist_names')
        payload = {'schema_version': 1, 'generated_at': now, 'sources': list(sources.values()),
                   'songs': result_songs, 'performers': performers,
                   'coverage': {'complete': False, 'spotify_checked': False,
                                'genres_complete': False,
                                'note': '동명 녹음을 합치지 않습니다. 곡·녹음 관계와 장르가 미확인인 항목을 포함합니다.'}}
        notify(progress, f'곡·녹음 {len(result_songs):,}개를 검증하고 저장하는 중…')
        _write_atomic(destination, payload, cancel)
        return {'updated': True, 'cancelled': False, 'songs': len(result_songs),
                'added': len(set(songs) - {s['id'] for s in previous['songs']}),
                'performers': len(performers), 'sources_updated': successful,
                'errors': errors, 'generated_at': now}
    except CatalogCancelled:
        return {'updated': False, 'cancelled': True, 'songs': len(previous['songs']),
                'errors': [], 'message': '취소되어 기존 카탈로그를 유지했습니다.'}
