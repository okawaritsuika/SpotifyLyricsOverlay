"""Offline song dictionary with independent, atomic public-data updates.

Public snapshots and locally added songs use separate files. Favorites, local
paths and manual recording mappings belong to the user's separate library.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import unicodedata
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile
from app_paths import prefer_catalog_update
from song_aliases import search_aliases
from spotify_catalog import parse_track_id
from storage import atomic_json

from catalog_sources import (CatalogCancelled, IMAS_URL, SERIES, catalog_queries, check_cancel,
                             enrich_performers, enrich_reviewed_releases, fetch_bytes, fetch_musicbrainz,
                             key, normalize_musicbrainz, notify, parse_imasparql,
                             reclassify_unknown_series, reclassify_reviewed_kinds)


ROOT = Path(__file__).resolve().parent
RESOURCE_PATH = ROOT / 'resources' / 'song_catalog.json'
SPOTIFY_PLAYLIST_PATH = ROOT / 'resources' / 'spotify_playlist_catalog.json'
MAX_CATALOG_BYTES = 96 * 1024 * 1024
MAX_SONGS = 40000
MAX_PERFORMERS = 10000
PERSONAL_CATALOG_NAME = 'personal_song_catalog.json'
PERSONAL_SONG_ID_PREFIX = 'personal:'
MAX_PERSONAL_SONGS = 2000
_PERSONAL_LOCK = Lock()
_PERSONAL_FIELDS = {'title', 'artists', 'album', 'release_date', 'spotify_url', 'source_url', 'series', 'kind'}
SERIES_LABELS = {'765': '765 프로', 'million': '밀리언 라이브',
                 'cinderella': '신데렐라 걸즈', 'sidem': 'SideM',
                 'shiny': '샤이니 컬러즈', 'gakuen': '학원 아이돌마스터',
                 'other': '기타 · 시리즈 미확인'}


def _personal_text(value, label, maximum=200, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(
            unicodedata.category(char) in ('Cc', 'Cs') for char in value):
        raise ValueError(f'{label} 항목을 확인하세요. 최대 {maximum}자까지 입력할 수 있습니다.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{label} 항목을 입력하세요.')
    return value


def normalize_personal_song(fields):
    if not isinstance(fields, dict) or fields.keys() - _PERSONAL_FIELDS:
        raise ValueError('지원되는 항목만 포함된 곡 정보 한 개를 입력하세요.')
    title = _personal_text(fields.get('title', ''), '곡명', required=True)
    artists = fields.get('artists')
    if not isinstance(artists, list) or not 0 < len(artists) <= 32:
        raise ValueError('가창자 또는 아티스트를 1명 이상 32명 이하로 입력하세요.')
    names = [_personal_text(name, '가창자 또는 아티스트', required=True) for name in artists]
    artists = list({key(name): name for name in names}.values())
    album = _personal_text(fields.get('album', ''), '앨범')
    release_date = _personal_text(fields.get('release_date', ''), '발매일', maximum=10)
    if release_date:
        try:
            if date.fromisoformat(release_date).isoformat() != release_date:
                raise ValueError
        except ValueError as error:
            raise ValueError('발매일은 실제 날짜를 YYYY-MM-DD 형식으로 입력하세요.') from error

    series = fields.get('series', 'other')
    if series not in SERIES:
        raise ValueError('시리즈를 선택하세요.')
    kind = fields.get('kind', 'song')
    if kind not in ('song', 'instrumental', 'spoken', 'unknown'):
        raise ValueError('곡 유형을 확인하세요.')

    spotify = _personal_text(fields.get('spotify_url', ''), 'Spotify 주소', maximum=2048)
    if spotify:
        track_id = parse_track_id(spotify)
        if not track_id:
            raise ValueError('Spotify 곡 주소 또는 트랙 ID를 입력하세요.')
        spotify = 'https://open.spotify.com/track/' + track_id
    source = _personal_text(fields.get('source_url', ''), '출처 주소', maximum=2048)
    if source:
        try:
            parsed = urlsplit(source)
            valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                     and parsed.password is None and parsed.port != 0 and '\\' not in source
                     and not any(char.isspace() for char in source))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('출처 주소는 HTTPS 웹 주소를 입력하세요.')
    return dict(title=title, artists=artists, album=album, release_date=release_date,
                spotify_url=spotify, source_url=source, series=series, kind=kind)


def _personal_json_pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('곡 정보에 같은 항목이 두 번 포함되어 있습니다.')
    return value


def parse_personal_song_json(value):
    if not isinstance(value, str) or len(value) > 32768:
        raise ValueError('JSON으로 작성된 곡 정보 한 개를 붙여 넣으세요.')
    value = value.strip()
    lines = value.splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in ('```json', '```') and lines[-1].strip() == '```':
        value = '\n'.join(lines[1:-1])
    try:
        fields = json.loads(value, object_pairs_hook=_personal_json_pairs)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError('JSON으로 작성된 곡 정보 한 개를 붙여 넣으세요.') from error
    return normalize_personal_song(fields)


def _personal_id(fields):
    identity = fields['spotify_url'] or [key(fields['title']), sorted(map(key, fields['artists'])),
                                        key(fields['album']), fields['release_date'], fields['kind']]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return PERSONAL_SONG_ID_PREFIX + hashlib.sha256(encoded).hexdigest()[:24]


def _personal_catalog_song(fields):
    return dict(id=_personal_id(fields), title=fields['title'], artists=fields['artists'], album=fields['album'],
                series=[fields['series']], kind=fields['kind'], type=fields['kind'],
                first_release_date=fields['release_date'], spotify_url=fields['spotify_url'],
                source_url=fields['source_url'], source_id='personal', personal=True)


def _read_personal_songs(path):
    if path is None:
        return {}
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_personal_json_pairs)
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or not isinstance(value.get('songs'), list) or len(value['songs']) > MAX_PERSONAL_SONGS):
            raise ValueError
        songs = [normalize_personal_song(fields) for fields in value['songs']]
        indexed = {_personal_id(fields): fields for fields in songs}
        if len(indexed) != len(songs):
            raise ValueError
        return indexed
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError('개인 추가곡 파일을 읽을 수 없습니다. 원본은 보존했습니다.') from error


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
    if any(field in value and not _strings(value[field]) for field in
           ('aliases', 'performer_aliases', 'performer_ids', 'groups', 'missing',
            'artists_original', 'spotify_recording_candidates')):
        return False
    if 'album' in value and not _string(value['album']):
        return False
    if 'spotify_url' in value and (not _string(value['spotify_url']) or not re.fullmatch(
            r'https://open\.spotify\.com/track/[A-Za-z0-9]{22}', value['spotify_url'])):
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
            and value['first_release_date_source_kind'] not in ('official_label', 'musicbrainz', 'streaming_service')):
        return False
    if not _duration(value.get('duration')):
        return False
    if 'spotify_recording' in value:
        recording = value['spotify_recording']
        if (not isinstance(recording, dict) or set(recording) != {'title', 'artists', 'album', 'duration'}
                or not _string(recording['title']) or not _string(recording['album'])
                or not _strings(recording['artists'], 300) or not _duration(recording['duration'])):
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
                    or 'date_source_kind' in entry and entry['date_source_kind'] not in ('official_label', 'musicbrainz', 'streaming_service')
                    or not _duration(entry.get('duration'))):
                return False
    if 'kind' in value and value['kind'] not in ('song', 'instrumental', 'spoken', 'unknown'):
        return False
    return True


def valid_catalog(value):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1
            or not _string(value.get('generated_at'))
            or not isinstance(value.get('sources'), list)
            or len(value['sources']) > 100
            or any(not isinstance(s, dict) or not _string(s.get('id', '')) for s in value['sources'])
            or not isinstance(value.get('songs'), list)
            or not 0 < len(value['songs']) <= MAX_SONGS
            or any(not _valid_song(song) for song in value['songs'])
            or len({song['id'] for song in value['songs']}) != len(value['songs'])):
        return False
    performers = value.get('performers', [])
    return (isinstance(performers, list) and len(performers) <= MAX_PERFORMERS
            and all(isinstance(p, dict) and _string(p.get('id'))
                    and _string(p.get('name')) and p.get('kind') in ('idol', 'group')
                    and _strings(p.get('aliases', [])) and _strings(p.get('members', []))
                    for p in performers))


def read_catalog(path):
    """Reject a malformed snapshot as a whole, preserving the last working one."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            return None
        value = json.loads(raw)
        return value if valid_catalog(value) else None
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


def _keep_official_metadata(recording, reviewed):
    """Retain verified editions and links when a newer snapshot lacks them."""
    if reviewed.get('spotify_url') and not recording.get('spotify_url'):
        recording['spotify_url'] = reviewed['spotify_url']
        recording['missing'] = [field for field in recording.get('missing', []) if field != 'spotify']
    official = [release for release in reviewed.get('releases', [])
                if release.get('date_source_kind') in ('official_label', 'streaming_service')]
    if official:
        known = {release.get('id') for release in recording.get('releases', [])}
        recording['releases'] = recording.get('releases', []) + [release for release in official
                                                                 if release.get('id') not in known]


def add_spotify_playlist(songs, snapshot, library):
    """Expose each playlist recording without replacing a user's Spotify link."""
    linked = library.get('spotify', {})
    excluded = set(library.get('automatic_link_exclusions', ()))
    favorites = set(library.get('favorites', ()))
    urls = {song['spotify_url']: song for ident, song in songs.items()
            if song.get('spotify_url') and ident not in excluded and ident not in linked}
    linked_ids = {track.get('id') for ident, track in linked.items()
                  if ident in songs and isinstance(track, dict)}
    excluded_urls = {songs[ident].get('spotify_url') for ident in excluded if ident in songs}

    for track in snapshot['songs']:
        url = track.get('spotify_url', '')
        playlist_song_id = track['id']
        track_id = url.rsplit('/', 1)[-1]
        if not track_id or playlist_song_id != 'spotify:' + track_id:
            continue
        catalog_ids = track.get('spotify_recording_candidates', ())
        if track_id in linked_ids:
            continue
        recording = dict(title=track['title'], artists=track['artists'],
                         album=track.get('album', ''), duration=track.get('duration'))
        if url in urls:
            urls[url]['spotify_recording'] = recording
            continue
        # Keep a personal choice made against the standalone recording stable.
        if playlist_song_id not in linked and playlist_song_id not in excluded and playlist_song_id not in favorites:
            candidate = next((songs[ident] for ident in catalog_ids if ident in songs
                              and ident not in linked and ident not in excluded
                              and not songs[ident].get('spotify_url')), None)
            if candidate is not None:
                candidate['spotify_url'] = url
                candidate['spotify_recording'] = recording
                candidate['missing'] = [field for field in candidate.get('missing', []) if field != 'spotify']
                continue
            if catalog_ids and all(ident in excluded for ident in catalog_ids):
                continue
        if url not in excluded_urls:
            songs[playlist_song_id] = track


class SongCatalog:
    def __init__(self, resource_path=None, update_path=None, *, spotify_library=None, personal_path=None):
        self.resource_path = Path(resource_path) if resource_path else RESOURCE_PATH
        self.update_path = Path(update_path) if update_path else None
        self.spotify_library = spotify_library
        self.personal_path = Path(personal_path) if personal_path else None
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
        if not prefer_catalog_update(base, update):
            update = None
        try:
            personal = _read_personal_songs(self.personal_path)
        except ValueError:
            personal = {}
        chosen = update or base
        if chosen is None:
            if personal and (not self.songs or self.status == 'personal'):
                self._set_songs(_personal_catalog_song(fields) for fields in personal.values())
                self.status = 'personal'
                return True
            # Existing in-memory data remains usable after a broken update file.
            self.status = 'retained' if self.songs else 'empty'
            return False
        # Update files are complete snapshots. Merge by stable ID to keep older
        # bundled entries if a source has disappeared or a user returns offline.
        songs = {s['id']: s for s in base['songs']} if base else {}
        if update:
            for song in update['songs']:
                prior = songs.get(song['id'])
                if prior:
                    _keep_official_metadata(song, prior)
        songs.update({s['id']: s for s in chosen['songs']})
        reclassify_unknown_series(songs.values())
        reclassify_reviewed_kinds(songs.values())
        corrected_artists = enrich_reviewed_releases(songs.values())
        playlist = read_catalog(SPOTIFY_PLAYLIST_PATH) if self.spotify_library is not None else None
        if playlist:
            add_spotify_playlist(songs, playlist, self.spotify_library)
        self.performers, reviewed_unit = _reviewed_unit_alias(
            chosen.get('performers', base.get('performers', []) if base else []))
        derived_targets = {song['id']: song for song in corrected_artists}
        if reviewed_unit:
            derived_targets.update((song['id'], song) for song in songs.values()
                if song['artists'] == ['new generations']
                and not set(song.get('performer_ids', [])) - {reviewed_unit})
        if derived_targets:
            enrich_performers(derived_targets.values(), deepcopy(self.performers),
                              names_directory=ROOT / 'resources' / 'artist_names')
        sources = {source.get('id'): source for source in base['sources']} if base else {}
        sources.update((source.get('id'), source) for source in chosen['sources'])
        if playlist:
            sources.update((source.get('id'), source) for source in playlist['sources'])
        self.sources = list(sources.values())
        self.generated_at = max(chosen['generated_at'], base['generated_at']) if base else chosen['generated_at']
        self.status = 'updated' if update else 'bundled'
        self._merge_personal(songs, personal)
        return True

    def _set_songs(self, songs):
        self.songs = sorted(songs, key=lambda song: (key(song['title']), song['id']))
        self._index = []
        for song in self.songs:
            performer = key(' '.join(song['artists'] + song.get('groups', [])
                                    + song.get('performer_aliases', [])
                                    + song.get('performer_ids', [])))
            full = key(' '.join([song['title'], song.get('album', ''), performer]
                               + song.get('aliases', []) + list(search_aliases(song))))
            self._index.append((song, full, performer))

    def _merge_personal(self, songs, personal):
        for ident, fields in personal.items():
            if ident not in songs:
                songs[ident] = _personal_catalog_song(fields)
        self._set_songs(songs.values())

    def add_personal_song(self, fields):
        if self.personal_path is None:
            raise ValueError('개인 추가곡 저장 위치가 설정되어 있지 않습니다.')
        fields = normalize_personal_song(fields)
        song = _personal_catalog_song(fields)
        ident = song['id']
        with _PERSONAL_LOCK:
            personal = _read_personal_songs(self.personal_path)
            public = {item['id']: item for item in self.songs if not item.get('personal')}
            if ident in public:
                return {'song': public[ident], 'added': False}
            if ident in personal:
                self._merge_personal(public, personal)
                return {'song': _personal_catalog_song(personal[ident]), 'added': False}
            track_id = parse_track_id(song['spotify_url'])
            if track_id:
                linked = (self.spotify_library or {}).get('spotify', {})
                for item in self.songs:
                    choice = linked.get(item['id'])
                    if (parse_track_id(item.get('spotify_url')) == track_id
                            or isinstance(choice, dict) and choice.get('id') == track_id):
                        return {'song': item, 'added': False}
            if len(personal) >= MAX_PERSONAL_SONGS:
                raise ValueError(f'개인 추가곡은 최대 {MAX_PERSONAL_SONGS:,}곡까지 저장할 수 있습니다.')
            personal[ident] = fields
            atomic_json(self.personal_path, {'schema_version': 1, 'songs': list(personal.values())})
            if not public:
                self.status = 'personal'
            self._merge_personal(public, personal)
        return {'song': song, 'added': True}

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

    Cached source pages can resume an interrupted build. A cancelled refresh
    never replaces the working catalog.
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
                prior = songs.get(song['id'])
                if prior:
                    _keep_official_metadata(song, prior)
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
