"""A curated, source-backed live history independent of playback and lyrics.

The snapshot deliberately covers only verified events. A missing entry means
unverified, never that the song has not been performed. Song associations are by
normalized work title and series, not by studio recording or singer.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from app_paths import prefer_catalog_update


RESOURCE_PATH = Path(__file__).resolve().parent / 'resources' / 'live_catalog.json'
LINKS_PATH = RESOURCE_PATH.with_name('live_event_links.json')
WORK_ALIASES_PATH = Path(__file__).resolve().parent / 'resources' / 'live_work_aliases.json'
MAX_BYTES = 16 * 1024 * 1024
SERIES = frozenset(('765', 'million', 'cinderella', 'sidem', 'shiny', 'gakuen', 'other'))
IMAS_DB_WORK = re.compile(r'https://imas-db\.jp/song/detail/\d+\.html\Z')
VALIV_ARTISTS = frozenset(('vα-liv', 'レトラ', '灯里愛夏', '上水流宇宙', '#ヴイアラ'))


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


def _reviewed_live_works():
    """Official works with documented releases spanning series and covers."""
    try:
        rows = json.loads(WORK_ALIASES_PATH.read_text(encoding='utf-8')).get('live_works', [])
        return {title_key(row['title']): (tuple(title_key(album) for album in row['recording_albums']),
                                        frozenset(row.get('event_ids', ())))
                for row in rows if _text(row.get('title')) and row.get('source_urls')
                and all(_url(url) for url in row['source_urls']) and row.get('recording_albums')
                and all(_text(album) for album in row['recording_albums'])
                and all(_text(event_id,200) for event_id in row.get('event_ids', ()))}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}


def _distinct_work(key):
    japanese = sum('\u3040' <= char <= '\u30ff' or '\u3400' <= char <= '\u9fff' for char in key)
    return japanese >= 5 or (len(key) >= 8 and '@' in key)


def _live_candidates(performance, event, index, reviews):
    if performance.get('performance_kind') == 'dance_without_vocals':
        return []
    keys = _row_keys(performance)
    series = set(performance.get('series', event['series']))
    exact = {song['id']: song for key in sorted(keys) for song in index.get(key, ())
             if _song_series(song).intersection(series)}
    linked = {song['id']: song for key in sorted(keys & reviews.keys()) for song in index.get(key, ())
              if (not reviews[key][1] or event['id'] in reviews[key][1])
              and any(album in title_key(song.get('album')) for album in reviews[key][0])}
    if 'other' in series and 'vα-liv' in performance.get('series_names', event.get('series_names', ())):
        linked.update((song['id'], song) for key in sorted(keys) for song in index.get(key, ())
                      if any(title_key(name) in VALIV_ARTISTS for name in song.get('artists', ())))
    if exact or linked:
        exact.update(linked)
        return list(exact.values())

    # A series inherited from the theater or event only identifies the stage.
    # A published work link plus one unconflicted recording family, or a highly
    # distinctive title that resolves to one vocal recording, can recover it.
    if performance.get('series_basis') != 'event_section_brand':
        return []
    source_key = work_key(performance.get('song_title'))
    source_url = performance.get('song_source_url') or ''
    if source_key and IMAS_DB_WORK.fullmatch(source_url):
        songs = index.get(source_key, ())
        brands = {tuple(sorted(_song_series(song))) for song in songs}
        if len(brands) == 1 and (len(songs) == 1 or next(iter(brands))):
            return songs
    key = work_key(performance.get('title'))
    songs = index.get(key, ())
    if len(songs) == 1 and _distinct_work(key):
        return songs
    return []


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


def valid_catalog(value):
    return (isinstance(value, dict) and type(value.get('schema_version')) is int
            and value['schema_version'] == 1 and value.get('coverage') == 'partial'
            and _text(value.get('verified_at'), 40)
            and isinstance(value.get('events'), list) and 0 < len(value['events']) <= 10000
            and all(_valid_event(event) for event in value['events'])
            and len({event['id'] for event in value['events']}) == len(value['events']))


def read_catalog(path):
    """Reject an incomplete or malformed replacement as a whole."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
        return value if valid_catalog(value) else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def reviewed_event_links():
    try:
        value = json.loads(LINKS_PATH.read_text(encoding='utf-8'))
        if isinstance(value, dict) and value.get('schema_version') == 1:
            return value['links'] if isinstance(value.get('links'), list) else []
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return []


def _snapshot_time(value):
    for field in ('generated_at', 'verified_at'):
        stamp = value.get(field)
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            except ValueError:
                pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _keep_live_evidence(earlier, current):
    if earlier['date'] != current['date']:
        return current
    source_links = earlier.get('official_sources', [])
    if earlier.get('source_kind', 'official') == 'official' and current.get('source_kind') == 'fan_database':
        # A curated performance has its own setlist unless the incoming row
        # has already been linked to that official record.
        if not current.get('official_sources'):
            return earlier
        source_links = [*source_links, {'name': earlier.get('source_name') or '공식 자료',
                                       'url': earlier['source_url']}]
    evidence = {link['url']: link for link in (*source_links, *current.get('official_sources', []))}
    merged = {**current, 'official_sources': list(evidence.values())} if evidence else current
    old_date_basis = earlier.get('date_basis', '')
    if ('official' in old_date_basis and 'official' not in current.get('date_basis', '')
            and earlier.get('date_source_url')):
        merged = {**merged, 'date_basis': old_date_basis, 'date_source_url': earlier['date_source_url']}
        for field in ('official_date_source_url', 'date_correction', 'original_page_date_text'):
            if field in earlier:
                merged[field] = earlier[field]
    if evidence and earlier.get('venue') and not current.get('venue'):
        merged = {**merged, 'venue': earlier['venue']}
    return merged


def merge_event_rows(earlier, current, current_pages, reviewed_ids, earlier_pages=None):
    """Use complete imports per page while keeping independent performance records."""
    earlier_pages = earlier_pages or {}
    current_wins = {url for url, stamp in current_pages.items()
                    if url not in earlier_pages or stamp >= earlier_pages[url]}
    earlier_wins = earlier_pages.keys() - current_wins
    prior = {event['id']: event for event in earlier}
    events = {identifier: event for identifier, event in prior.items()
              if identifier in reviewed_ids or event.get('source_kind') != 'fan_database'
              or event['source_url'] not in current_wins}
    for event in current:
        identifier = event['id']
        if (identifier not in reviewed_ids and event.get('source_kind') == 'fan_database'
                and event['source_url'] in earlier_wins):
            if identifier in events:
                events[identifier] = _keep_live_evidence(event, events[identifier])
            continue
        events[identifier] = _keep_live_evidence(prior[identifier], event) if identifier in prior else event
    return events


def coalesce_reviewed_events(events, reviewed_links):
    """Keep the reviewed performance ID when a raw table has the same session."""
    for link in reviewed_links:
        official = events.get(link.get('official_id'))
        if not official or official['date'] != link.get('date'):
            continue
        candidates = [event for event in events.values()
                      if event.get('source_kind') == 'fan_database'
                      and event['id'] != official['id'] and event['date'] == official['date']
                      and event['source_url'] == link.get('database_url')
                      and (not link.get('database_section')
                           or event.get('source_table_label') == link['database_section'])]
        if len(candidates) != 1:
            continue
        database = candidates[0]
        sources = {item['url']: item for item in (*official.get('official_sources', []),
                                                    *database.get('official_sources', []))}
        if official.get('source_kind', 'official') == 'official':
            for field in ('source_url', 'date_source_url', 'setlist_source_url'):
                if url := official.get(field):
                    sources.setdefault(url, {'name': official.get('source_name') or '공식 자료', 'url': url})
        merged = {**database, 'id': official['id'], 'title': official['title'],
                  'official_sources': list(sources.values()), 'songs': deepcopy(database['songs'])}
        for row in merged['songs']:
            matches = [old for old in official['songs'] if title_key(row['title']) in
                       {title_key(old[field]) for field in ('title', 'song_title', 'performance_title') if old.get(field)}]
            if len(matches) == 1:
                for field in ('song_title', 'performance_title'):
                    if matches[0].get(field):
                        row.setdefault(field, matches[0][field])
        events.pop(database['id'])
        events[official['id']] = _keep_live_evidence(official, merged)


def _collection_report(snapshot):
    report = snapshot.get('collection_report')
    return report if isinstance(report, dict) else {}


def live_page_checks(snapshot):
    report = _collection_report(snapshot)
    stored = report.get('successful_url_checks')
    checks = {url: _snapshot_time({'verified_at': stamp}) for url, stamp in stored.items()
              if isinstance(url, str) and isinstance(stamp, str)} if isinstance(stored, dict) else {}
    urls = report.get('successful_urls')
    if isinstance(urls, list):
        stamp = _snapshot_time(snapshot)
        for url in urls:
            if isinstance(url, str):
                checks.setdefault(url, stamp)
    return checks


def merge_live_snapshots(base, update, reviewed_links=None):
    if base is None or update is None:
        return update or base
    if reviewed_links is None:
        reviewed_links = reviewed_event_links()
    reviewed_ids = {link.get('official_id') for link in reviewed_links}
    earlier, current = (base, update) if _snapshot_time(update) >= _snapshot_time(base) else (update, base)
    prior_report, report = _collection_report(earlier), _collection_report(current)
    earlier_pages, current_pages = live_page_checks(earlier), live_page_checks(current)
    by_id = merge_event_rows(earlier['events'], current['events'], current_pages, reviewed_ids, earlier_pages)
    coalesce_reviewed_events(by_id, reviewed_links)
    events = list(by_id.values())
    checks = {url: max(stamp, earlier_pages.get(url, stamp)) for url, stamp in current_pages.items()}
    checks.update((url, stamp) for url, stamp in earlier_pages.items() if url not in checks)
    return {**current, 'events': events, 'collection_report': {
        **prior_report, **report, 'events': len(events),
        'successful_url_checks': {url: stamp.isoformat() for url, stamp in checks.items()},
        'setlist_entries': sum(len(event['songs']) for event in events)}}


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
        self._recording_source = None
        self.reload()

    def reload(self):
        base = read_catalog(self.resource_path)
        update = (read_catalog(self.update_path)
                  if self.update_path and self.update_path != self.resource_path else None)
        if not prefer_catalog_update(base, update):
            update = None
        value = merge_live_snapshots(base, update)
        if value is None:
            return False
        events = sorted(value['events'], key=lambda event: (event['date'], event['id']), reverse=True)
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
        self._reviewed_live_works = _reviewed_live_works()
        self._recording_source = None
        self.verified_at = value['verified_at']
        self.generated_at = value.get('generated_at', self.verified_at)
        self.note = value.get('note') or self.note
        self.collection_report = deepcopy(value.get('collection_report', {}))
        self.status = 'ready'
        return True

    def index_recordings(self, songs):
        """Bind the full dictionary once so qualified fallback never depends on filters."""
        if songs is self._recording_source:
            return
        index = {}
        for song in songs:
            if song.get('kind') not in ('instrumental', 'spoken', 'unknown'):
                index.setdefault(_song_work_key(song, self._work_aliases), []).append(song)
        self._row_recordings, self._by_recording = {}, {}
        for event in self.events:
            for position, performance in enumerate(event['songs'], 1):
                matches = _live_candidates(performance, event, index, self._reviewed_live_works)
                self._row_recordings[event['id'], position] = matches
                for song in matches:
                    self._by_recording.setdefault(song['id'], {})[event['id']] = event
        self._recording_source = songs

    def _matching_events(self, song):
        if not isinstance(song, dict):
            return '', ()
        key = _song_work_key(song, self._work_aliases)
        if not key or song.get('kind') in ('instrumental', 'spoken', 'unknown'):
            return key, ()
        if self._recording_source is not None and song.get('id') in self._by_recording:
            return key, tuple(self._by_recording[song['id']].values())
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
        if self._recording_source is not None:
            shown = None if songs is self._recording_source else {song['id'] for song in songs}
            return [{'position':position, 'performance':deepcopy(performance),
                     'matches':deepcopy([song for song in self._row_recordings[event_id, position]
                                         if shown is None or song['id'] in shown]),
                     'match_basis':'work_title_and_series', 'recording_version_unverified':True}
                    for position, performance in enumerate(selected['songs'], 1)]
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
