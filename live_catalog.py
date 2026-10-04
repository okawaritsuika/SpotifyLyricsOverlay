"""A curated, source-backed live history independent of playback and lyrics.

The snapshot deliberately covers only verified events. A missing entry means
unverified, never that the song has not been performed. Song associations are by
normalized work title and series, not by studio recording or singer.
"""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


RESOURCE_PATH = Path(__file__).resolve().parent / 'resources' / 'live_catalog.json'
WORK_ALIASES_PATH = Path(__file__).resolve().parent / 'resources' / 'live_work_aliases.json'
MAX_BYTES = 16 * 1024 * 1024
SERIES = frozenset(('765', 'million', 'cinderella', 'sidem', 'shiny', 'gakuen', 'other'))


def title_key(value):
    """Normalize typography only; never discard arrangement/version qualifiers."""
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    value = value.translate(str.maketrans({'’': "'", 'ʼ': "'", '‘': "'", '〜': '~', '–': '-',
                                         '“': '"', '”': '"'}))
    return re.sub(r'\s+', '', value)


def work_key(value, artists=()):
    """Recognize explicit full/game-length and credited solo labels.

    This is a work association, never evidence that a studio version was sung.
    A solo suffix is removed only when its name exactly matches a recording
    artist credit. Unknown arrangements, remixes and year labels stay intact.
    """
    value = title_key(value)
    solo = re.search(r'[\[(]([^\[\]()]+?)(?:solover\.|ソロver\.|ソロ・リミックス)[\])]$', value)
    if solo and solo[1] in {title_key(artist) for artist in (artists or ())}:
        value = value[:solo.start()]
    return re.sub(r'\((?:m@sterversion|gameversion|gamever\.|gamesize)\)$', '', value)


def _row_keys(row):
    return {work_key(row[field]) for field in ('title', 'song_title', 'performance_title')
            if row.get(field)}


def _song_series(song):
    if song.get('kind') in ('instrumental', 'spoken', 'unknown'):
        return set()
    return set(song.get('series') or ()) - {'other'}


def _text(value, limit=2000, empty=False):
    return (isinstance(value, str) and len(value) <= limit and '\0' not in value
            and (empty or bool(value.strip())))


def _url(value):
    if not _text(value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _reviewed_work_aliases():
    """Read exact vocal recording identities verified against official tracks."""
    def identifier(value):
        return isinstance(value, str) and bool(re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value))

    try:
        with WORK_ALIASES_PATH.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return {}
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('entries'), list)):
            return {}
        index = {}
        for entry in value['entries']:
            if (not isinstance(entry, dict) or not identifier(entry.get('recording_id'))
                    or entry['recording_id'] in index or not identifier(entry.get('release_id'))
                    or any(not _text(entry.get(field)) for field in ('recording_title', 'release_title', 'work_title'))
                    or not isinstance(entry.get('artists'), list) or not entry['artists']
                    or any(not _text(item) for item in entry['artists'])
                    or not isinstance(entry.get('series'), list) or not entry['series']
                    or any(item not in SERIES - {'other'} for item in entry['series'])
                    or not isinstance(entry.get('source_urls'), list) or not entry['source_urls']
                    or any(not _url(item) for item in entry['source_urls'])):
                return {}
            index[entry['recording_id']] = entry
        return index
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _song_work_key(song, aliases):
    reviewed = aliases.get(str(song.get('id', '')).removeprefix('mb:'))
    if (reviewed and song.get('source_id') == 'musicbrainz' and song.get('kind') == 'song'
            and song.get('title') == reviewed['recording_title']
            and song.get('artists') == reviewed['artists'] and song.get('series') == reviewed['series']
            and any(isinstance(release, dict) and release.get('id') == reviewed['release_id']
                    and release.get('title') == reviewed['release_title'] for release in song.get('releases', []))):
        return work_key(reviewed['work_title'])
    return work_key(song.get('title'), song.get('artists'))


def _valid_event(event):
    if (not isinstance(event, dict) or not _text(event.get('id'), 200)
            or not _text(event.get('title')) or not _url(event.get('source_url'))
            or not isinstance(event.get('series'), list) or not event['series']
            or any(series not in SERIES for series in event['series'])):
        return False
    stamp = event.get('date')
    if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', stamp):
        return False
    try:
        date.fromisoformat(stamp)
    except ValueError:
        return False
    for field in ('venue', 'source_name', 'note', 'setlist_coverage'):
        if field in event and not _text(event[field], empty=True):
            return False
    for field in ('date_source_url', 'setlist_source_url'):
        if field in event and not _url(event[field]):
            return False
    if 'source_kind' in event and event['source_kind'] not in ('official', 'fan_database'):
        return False
    for field in ('series_names',):
        if field in event and (not isinstance(event[field], list)
                               or any(not _text(item) for item in event[field])):
            return False
    if 'official_sources' in event and (not isinstance(event['official_sources'], list)
            or any(not isinstance(item, dict) or not _url(item.get('url'))
                   or not _text(item.get('name')) for item in event['official_sources'])):
        return False
    songs = event.get('songs')
    if not isinstance(songs, list) or not songs or len(songs) > 300:
        return False
    for song in songs:
        if not isinstance(song, dict) or not _text(song.get('title')):
            return False
        for field in ('song_title', 'performance_title', 'note'):
            if field in song and not _text(song[field]):
                return False
        if 'artists' in song and (not isinstance(song['artists'], list)
                                  or len(song['artists']) > 100
                                  or any(not _text(item) for item in song['artists'])):
            return False
        if 'series' in song and (not isinstance(song['series'], list) or not song['series']
                                  or any(brand not in SERIES for brand in song['series'])):
            return False
    return True


def read_catalog(path):
    """Reject an incomplete or malformed replacement as a whole."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('coverage') != 'partial'
                or not _text(value.get('verified_at'), 40)
                or not isinstance(value.get('events'), list)
                or not 0 < len(value['events']) <= 10000
                or any(not _valid_event(event) for event in value['events'])
                or len({event['id'] for event in value['events']}) != len(value['events'])):
            return None
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


class LiveCatalog:
    def __init__(self, resource_path=None, update_path=None):
        self.resource_path = Path(resource_path) if resource_path else RESOURCE_PATH
        self.update_path = Path(update_path) if update_path else None
        self.events = []
        self.coverage = 'partial'
        self.verified_at = ''
        self.generated_at = ''
        self.status = 'unavailable'
        self.note = '출처에서 확인한 일부 공연입니다. 기록이 없으면 아직 미확인입니다.'
        self.collection_report = {}
        self._by_title = {}
        self._work_aliases = {}
        self.reload()

    def reload(self):
        value = (read_catalog(self.update_path) if self.update_path else None) or read_catalog(self.resource_path)
        if value is None:
            return False
        events = sorted(deepcopy(value['events']), key=lambda event: (event['date'], event['id']), reverse=True)
        index = {}
        for event in events:
            # Several reprises in one setlist still represent one event.
            keys = {}
            for song in event['songs']:
                if song.get('performance_kind') == 'dance_without_vocals':
                    continue
                for key in _row_keys(song):
                    keys.setdefault(key, set()).update(song.get('series', event['series']))
            for key, series in keys.items():
                index.setdefault(key, []).append((event, frozenset(series)))
        self.events = events
        self._by_title = index
        self._work_aliases = _reviewed_work_aliases()
        self.verified_at = value['verified_at']
        self.generated_at = value.get('generated_at', self.verified_at)
        self.note = value.get('note') or self.note
        self.collection_report = deepcopy(value.get('collection_report', {}))
        self.status = 'ready'
        return True

    def _matching_events(self, song):
        if not isinstance(song, dict):
            return '', ()
        key = _song_work_key(song, self._work_aliases)
        if not key:
            return key, ()
        series = _song_series(song)
        # Same-title songs from another brand are not a confirmed association.
        if not series:
            return key, ()
        return key, [event for event, performed_series in self._by_title.get(key, ())
                     if series.intersection(performed_series)]

    def count_for_song(self, song):
        """Count distinct vocal events within this partial, work-based history."""
        return len(self._matching_events(song)[1])

    def for_song(self, song):
        """Return event copies, with an explicit non-recording matching label."""
        key, events = self._matching_events(song)
        matches = []
        for event in events:
            result = deepcopy(event)
            result['match_basis'] = ('title_and_series' if key == title_key(song.get('title'))
                                     else 'known_work_title_and_series')
            result['recording_version_unverified'] = True
            matches.append(result)
        return matches

    def songs_for_event(self, event, songs):
        """Keep setlist order, with all dictionary candidates and unmatched rows.

        Callers must present multiple recordings as candidates. The official
        performance credit is preserved separately from dictionary artists.
        """
        event_id = event.get('id') if isinstance(event, dict) else event
        selected = next((item for item in self.events if item['id'] == event_id), None)
        if selected is None:
            return []
        index = {}
        for song in songs:
            if isinstance(song, dict) and _song_series(song).intersection(selected['series']):
                index.setdefault(_song_work_key(song, self._work_aliases), []).append(song)
        result = []
        for position, performance in enumerate(selected['songs'], 1):
            matches = {}
            for key in sorted(_row_keys(performance)):
                for song in index.get(key, ()):
                    if _song_series(song).intersection(performance.get('series', selected['series'])):
                        matches.setdefault(song['id'], song)
            result.append({'position': position, 'performance': deepcopy(performance),
                           'matches': deepcopy(list(matches.values())),
                           'match_basis': 'work_title_and_series',
                           'recording_version_unverified': True})
        return result
