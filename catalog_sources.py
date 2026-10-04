"""Bounded, cancellable imports of public music metadata (never audio or lyrics).

MusicBrainz core metadata is CC0. im@sparql's repository is MIT licensed;
only factual fields are imported, not its descriptive prose or pictures.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlencode, unquote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zipfile


USER_AGENT = 'SpotifyLyricsOverlay/1.0 (personal non-commercial music catalog)'
IMAS_URL = 'https://codeload.github.com/imas/imasparql/zip/refs/heads/master'
MB_URL = 'https://musicbrainz.org/ws/2/recording'
# Reviewed MusicBrainz ensemble entities (not individual voice actors). Their
# recording credits explicitly name the franchise; IDs survive credit aliases.
FRANCHISE_ENSEMBLES = {
    '3e221c36-bc34-41ee-a59d-80546bfe6c09': 'THE IDOLM@STER FIVE STARS!!!!!',
    '9b1ae2d2-e9b8-4517-912b-23932bcfd2d1': 'シャイニーカラーズ',
    'e45ab497-e63c-4bf3-8815-1cff2427d818': 'THE IDOLM@STER CINDERELLA GIRLS',
    '8930a114-2b1f-4636-abe1-8e79fa1c7414': '315 STARS',
    '4c6d0d72-66d0-43e0-9f4c-beb41e747cc1': '765 MILLIONSTARS',
    '88cb8a6a-8b99-4e2e-964f-3353f90f71eb': '765PRO ALLSTARS',
}
MB_QUERIES = ('release:"THE IDOLM@STER"', 'artist:"初星学園"', 'release:"283 Production"',
              'release:"ラムネ色 青春" OR release:"虹色ミラクル"',
              '(artist:"THE IDOLM@STER" OR artist:"アイドルマスター") AND NOT release:"THE IDOLM@STER"',
              '(' + ' OR '.join('arid:' + identifier for identifier in FRANCHISE_ENSEMBLES)
              + ') AND NOT release:"THE IDOLM@STER"')
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_PAGES = 480
MAX_SECONDS = 1200
SERIES = ('765', 'million', 'cinderella', 'sidem', 'shiny', 'gakuen', 'other')
# The abbreviated title alone is not evidence. These physical release IDs were
# checked against Columbia's product numbers, dates, barcodes and four tracks.
REVIEWED_CHANGE_RELEASES = {
    'f0851cf5-b32d-4a2b-a6ea-c3025588aa03': 'https://columbia.jp/prod-info/COCC-16516/',
    'c8144507-62ab-4799-942a-4b4f101e604e': 'https://columbia.jp/prod-info/COZC-597-8/',
}
RECORDING_KIND_CORRECTIONS_PATH = Path(__file__).resolve().parent / 'resources' / 'recording_kind_corrections.json'
REVIEWED_RECORDING_RELEASES_PATH = Path(__file__).resolve().parent / 'resources' / 'reviewed_recording_releases.json'
RDF = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}'
SCHEMA = '{http://schema.org/}'
IMAS = '{https://sparql.crssnky.xyz/imasrdf/URIs/imas-schema.ttl#}'
RDFS = '{http://www.w3.org/2000/01/rdf-schema#}'


class CatalogCancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None:
        stopped = cancel.is_set() if hasattr(cancel, 'is_set') else bool(cancel())
        if stopped:
            raise CatalogCancelled('카탈로그 업데이트를 취소했습니다.')


def notify(progress, message):
    if progress is not None:
        progress(message)


def fetch_bytes(url, *, cancel=None, timeout=15, opener=urlopen):
    """HTTPS is fixed by the caller. Check cancellation between bounded reads."""
    check_cancel(cancel)
    deadline = time.monotonic() + timeout * 2
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with opener(request, timeout=timeout) as response:
        data = bytearray()
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise TimeoutError('데이터 응답 제한 시간을 초과했습니다.')
            chunk = response.read(64 * 1024)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError('데이터 응답이 허용 크기를 초과했습니다.')


def _pause(seconds, cancel):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_cancel(cancel)
        time.sleep(min(.1, max(0, end - time.monotonic())))


def key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))).casefold()


def unique(values):
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v.strip()))


def series_from_release(title):
    """Classify explicit album branding, not the performer's overlapping roster."""
    text = unicodedata.normalize('NFKC', title).upper()
    series = []
    markers = {
        'cinderella': ('CINDERELLA', 'シンデレラ'),
        'million': ('MILLION', 'ミリオン', 'LIVE THE@TER', 'M@STER SPARKLE',
                    'THE@TER ACTIVITIES', 'THE@TER BOOST', 'THE@TER CHALLENGE'),
        'sidem': ('SIDEM',),
        'shiny': ('SHINY COLORS', 'シャイニーカラーズ'),
        'gakuen': ('学園アイドルマスター', '初星学園'),
    }
    for name, words in markers.items():
        if any(word in text for word in words):
            series.append(name)
    if not series and ('IDOLM@STER' in text or 'アイドルマスター' in text):
        if any(word in text for word in ('MASTER ARTIST', 'MASTERPIECE', 'MASTER LIVE',
                                        'MASTER SPECIAL', 'MASTER BOX', 'MASTER PRIMAL',
                                        'ANIM@TION MASTER', 'NAMASSUKA', 'NAM@SUMMER',
                                        'GRE@TEST BEST', 'PLATINUM MASTER',
                                        'STELLA MASTER', 'MASTER OF MASTER')):
            series.append('765')
        elif ('765PRO ALLSTARS' in text
              and not any(word in text for word in ('961', '876', 'KOHAKU'))):
            # Explicit AS branding is evidence; mixed guest-brand editions are
            # left unclassified instead of assigning every track to 765.
            series.append('765')
    return series


def _reviewed_recording_releases():
    """Index exact recordings on independently checked official releases."""
    def text(value):
        return isinstance(value, str) and 0 < len(value) <= 2000 and bool(value.strip())

    def identifier(value):
        return isinstance(value, str) and bool(re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value))

    def url(value):
        if not text(value):
            return False
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password

    try:
        with REVIEWED_RECORDING_RELEASES_PATH.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return {}
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('entries'), list)):
            return {}
        reviewed, release_ids = {}, set()
        for entry in value['entries']:
            if (not isinstance(entry, dict) or not identifier(entry.get('release_id'))
                    or entry['release_id'] in release_ids or not text(entry.get('release_title'))
                    or entry.get('series') not in SERIES[:-1]
                    or not isinstance(entry.get('source_urls'), list) or not entry['source_urls']
                    or not all(url(item) for item in entry['source_urls'])
                    or not isinstance(entry.get('recordings'), list) or not entry['recordings']):
                return {}
            if 'date' in entry and (not isinstance(entry['date'], str)
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['date'])
                    or not url(entry.get('date_source_url'))
                    or entry.get('date_source_kind') not in ('official_label', 'musicbrainz')):
                return {}
            if 'date' in entry:
                time.strptime(entry['date'], '%Y-%m-%d')
            if any(field in entry for field in ('date_source_url', 'date_source_kind')) and 'date' not in entry:
                return {}
            if 'incorrect_date' in entry:
                if (not isinstance(entry['incorrect_date'], str)
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['incorrect_date'])
                        or not entry.get('date') or entry['incorrect_date'] == entry['date']
                        or entry.get('date_source_kind') != 'official_label'):
                    return {}
                time.strptime(entry['incorrect_date'], '%Y-%m-%d')
            if 'formats' in entry or 'format_source_url' in entry:
                formats = entry.get('formats')
                if (not isinstance(formats, list) or not formats or len(formats) > 10
                        or any(item not in ('CD', 'Digital Media', 'Blu-ray') for item in formats)
                        or len(set(formats)) != len(formats)
                        or entry.get('format_source_url') != 'https://musicbrainz.org/release/' + entry['release_id']):
                    return {}
            release_ids.add(entry['release_id'])
            seen = set()
            for recording in entry['recordings']:
                if (not isinstance(recording, dict) or not identifier(recording.get('id'))
                        or not text(recording.get('title')) or recording['id'] in seen):
                    return {}
                if 'release_when_missing_artists' in recording:
                    artists = recording['release_when_missing_artists']
                    if not isinstance(artists, list) or not artists or not all(text(item) for item in artists):
                        return {}
                if 'artist_correction' in recording:
                    correction = recording['artist_correction']
                    if (not isinstance(correction, dict)
                            or any(not isinstance(correction.get(field), list) or not correction[field]
                                   or len(correction[field]) > 50 or not all(text(item) for item in correction[field])
                                   for field in ('original_artists', 'artists'))
                            or any(not text(correction.get(field))
                                   for field in ('original_artist_credit', 'artist_credit'))
                            or correction['original_artists'] == correction['artists']
                            or correction['original_artist_credit'] == correction['artist_credit']
                            or not isinstance(correction.get('source_urls'), list)
                            or not 0 < len(correction['source_urls']) <= 10
                            or not all(url(item) and item in entry['source_urls'] for item in correction['source_urls'])):
                        return {}
                seen.add(recording['id'])
                prior = reviewed.setdefault(recording['id'], {'title': recording['title'], 'releases': {}})
                if prior['title'] != recording['title']:
                    return {}
                if 'artist_correction' in recording:
                    if 'artist_correction' in prior and prior['artist_correction'] != recording['artist_correction']:
                        return {}
                    prior['artist_correction'] = recording['artist_correction']
                prior['releases'][entry['release_id']] = entry
        return reviewed
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _reviewed_release_matches(identifier, title, releases, reviewed):
    recording = reviewed.get(identifier)
    if not recording or recording['title'] != title:
        return {}
    return {release['id']: recording['releases'][release['id']]
            for release in releases if isinstance(release, dict)
            and release.get('id') in recording['releases']
            and release.get('title') == recording['releases'][release['id']]['release_title']}


def _correct_reviewed_artists(song, correction):
    """Repair only the reviewed original credit, including its nested recording."""
    if (song.get('artists') != correction['original_artists']
            or song.get('artist_credit') != correction['original_artist_credit']):
        return False
    recordings = song.get('recordings', [])
    if (not isinstance(recordings, list) or not recordings
            or any(not isinstance(row, dict) or row.get('id') != song['id']
                   or row.get('mbid') != song['id'].removeprefix('mb:')
                   or row.get('title') != song['title']
                   or row.get('artists') != correction['original_artists'] for row in recordings)):
        return False
    song.update(artists_original=list(song['artists']), artist_credit_original=song['artist_credit'],
                artists=list(correction['artists']), artist_credit=correction['artist_credit'],
                artist_source_urls=list(correction['source_urls']), artist_source_kind='official_label')
    for row in recordings:
        row.update(artists_original=list(row['artists']), artists=list(correction['artists']),
                   artist_source_urls=list(correction['source_urls']), artist_source_kind='official_label')
    return True


def enrich_reviewed_releases(songs):
    """Apply exact published release facts without rewriting the raw snapshot.

    A known wrong date has an explicit old-value guard. First-release dates are
    repaired only for recordings whose sole listed release was independently
    audited; additional or different upstream evidence is preserved. Return the
    songs whose artist credits changed so callers can refresh their derived links.
    """
    reviewed = _reviewed_recording_releases()
    corrected_artists = []
    for song in songs:
        if song.get('source_id') != 'musicbrainz':
            continue
        releases = song.get('releases', [])
        matched = _reviewed_release_matches(song.get('id', '').removeprefix('mb:'),
                                           song.get('title'), releases, reviewed)
        # Artist repairs require an existing exact edition; adding a reviewed
        # edition later in this helper must never grant a new credit correction.
        for known in matched.values():
            row = next((row for row in known['recordings'] if 'mb:' + row['id'] == song.get('id')), None)
            correction = row.get('artist_correction') if row else None
            if (correction and song.get('series') == [known['series']]
                    and _correct_reviewed_artists(song, correction)):
                corrected_artists.append(song)
        # Search responses can omit another edition of the same exact recording.
        # Require an existing verified release, or an explicitly reviewed
        # missing membership with exact artist credits. Conflicting upstream
        # release IDs/titles remain untouched.
        known_recording = reviewed.get(song.get('id', '').removeprefix('mb:'))
        if known_recording and known_recording['title'] == song.get('title'):
            existing_ids = {release.get('id') for release in releases}
            for release_id, entry in known_recording['releases'].items():
                missing_artists = next((row.get('release_when_missing_artists') for row in entry['recordings']
                                        if row['id'] == song['id'].removeprefix('mb:')), None)
                if release_id not in existing_ids and (matched or missing_artists == song.get('artists')
                                                       and missing_artists is not None):
                    releases.append({'id': release_id, 'title': entry['release_title'],
                                     'date': entry.get('date', ''),
                                     'url': 'https://musicbrainz.org/release/' + release_id})
                    matched[release_id] = entry
        for release in releases:
            known = matched.get(release.get('id'))
            if not known:
                continue
            if known.get('formats'):
                release.update(formats=list(known['formats']), format_source_url=known['format_source_url'])
            if known.get('date'):
                old_date = release.get('date', '')
                incorrect = known.get('incorrect_date')
                correction_supported = bool(incorrect and (old_date == incorrect
                    or old_date == known['date'] and release.get('date_original') == incorrect))
                if incorrect and old_date == incorrect:
                    release['date_original'] = old_date
                    release['date'] = known['date']
                elif not old_date:
                    release['date'] = known['date']
                if release.get('date') == known['date']:
                    release.update(date_source_url=known['date_source_url'],
                                   date_source_kind=known['date_source_kind'])
                if (correction_supported and len(releases) == 1
                        and song.get('first_release_date') == incorrect):
                    song.update(first_release_date_original=incorrect,
                                first_release_date=known['date'],
                                first_release_date_source_url=known['date_source_url'],
                                first_release_date_source_kind=known['date_source_kind'])
    return corrected_artists


def recording_series_evidence(title, releases, *, recording_id='', reviewed=None):
    """Classify releases and reviewed track exceptions, never singers.

    STARLIT is a crossover album series, so it cannot be classified wholesale.
    The label's exact track credit confirms the 13-member AS M@STERPIECE version.
    Unknown tracks on that release remain unknown, including crossover covers.
    """
    brands, evidence, urls = [], [], []
    if not isinstance(releases, list):
        return brands, evidence, urls
    reviewed = _reviewed_recording_releases() if reviewed is None and recording_id else reviewed or {}
    matched = _reviewed_release_matches(recording_id, title, releases, reviewed)
    for release in releases:
        if not isinstance(release, dict):
            continue
        name = release.get('title', '')
        found = series_from_release(name)
        text = unicodedata.normalize('NFKC', name).upper()
        if 'STELLA MASTER' in text and found == ['765']:
            urls.append('https://columbia.jp/idolmaster/idolworld2023cd/science_cdlist2023.pdf')
        if 'MASTER OF MASTER' in text and found == ['765']:
            urls.append('https://columbia.jp/prod-info/COCX-35247-8/')
        if (key(name) == key('THE IDOLM@STER STARLIT SEASON 03')
                and key(title) == key('M@STERPIECE')):
            found = ['765']
            urls.append('https://catalog.bandainamcomusiclive.co.jp/release/70023/')
        if (release.get('id') in REVIEWED_CHANGE_RELEASES and key(name) == 'change!!!!'
                and key(title) in {'wejuststarted', '今スタート!', 'change!!!!(m@sterversion)',
                                   'change!!!!(m@sterversion)instrumental'}):
            found = ['765']
            urls.append(REVIEWED_CHANGE_RELEASES[release['id']])
        if release.get('id') in matched:
            reviewed_release = matched[release['id']]
            found = [reviewed_release['series']]
            urls.extend(reviewed_release['source_urls'])
        if found:
            brands.extend(found)
            evidence.append(name)
    return unique(brands), unique(evidence), unique(urls)


def reclassify_unknown_series(songs):
    """Repair cached normalized public records without changing their identity.

    Normalized MusicBrainz releases were already filtered to official releases
    during import. Apply the same reviewed rules after loading old update files.
    """
    changed = 0
    reviewed = _reviewed_recording_releases()
    for song in songs:
        if song.get('source_id') != 'musicbrainz' or song.get('series') != ['other']:
            continue
        brands, evidence, urls = recording_series_evidence(song['title'], song.get('releases', []),
            recording_id=song.get('id', '').removeprefix('mb:'), reviewed=reviewed)
        if not brands:
            continue
        song['series'] = brands
        song['series_evidence'] = evidence
        if urls:
            song['series_source_urls'] = urls
        if 'missing' in song:
            song['missing'] = [field for field in song['missing'] if field != 'series']
        changed += 1
    return changed


def _recording_kind_reviews():
    """Read only complete, unambiguous reviewed recording/release evidence."""
    def text(value):
        return isinstance(value, str) and 0 < len(value) <= 2000 and bool(value.strip())

    def identifier(value):
        return isinstance(value, str) and bool(re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value))

    try:
        with RECORDING_KIND_CORRECTIONS_PATH.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return {}
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('entries'), list)):
            return {}
        reviewed, release_ids = {}, set()
        for entry in value['entries']:
            if (not isinstance(entry, dict) or not identifier(entry.get('release_id'))
                    or entry['release_id'] in release_ids or not text(entry.get('release_title'))
                    or entry.get('kind') not in ('instrumental', 'spoken')
                    or not isinstance(entry.get('source_urls'), list) or not entry['source_urls']
                    or not isinstance(entry.get('recordings'), list) or not entry['recordings']):
                return {}
            for url in entry['source_urls']:
                if not text(url):
                    return {}
                parsed = urlsplit(url)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    return {}
            release_ids.add(entry['release_id'])
            alternate = entry.get('alternate_releases', [])
            if not isinstance(alternate, list) or len(alternate) > 20:
                return {}
            for release in alternate:
                if (not isinstance(release, dict) or not identifier(release.get('id'))
                        or release['id'] in release_ids or not text(release.get('title'))):
                    return {}
                release_ids.add(release['id'])
            for recording in entry['recordings']:
                if (not isinstance(recording, dict) or not identifier(recording.get('id'))
                        or not text(recording.get('title')) or 'mb:' + recording['id'] in reviewed):
                    return {}
                reviewed['mb:' + recording['id']] = (recording['title'], entry)
        return reviewed
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def reclassify_reviewed_kinds(songs):
    """Keep exact recording identities while correcting reviewed non-vocal kinds."""
    reviewed = _recording_kind_reviews()
    changed = 0
    for song in songs:
        match = reviewed.get(song.get('id'))
        if not match or song.get('source_id') != 'musicbrainz' or song.get('title') != match[0]:
            continue
        evidence = match[1]
        verified_releases = [{'id': evidence['release_id'], 'title': evidence['release_title']}]
        verified_releases.extend(evidence.get('alternate_releases', []))
        if not any(isinstance(release, dict) and any(release.get('id') == known['id']
                   and release.get('title') == known['title'] for known in verified_releases)
                   for release in song.get('releases', [])):
            continue
        kind, urls = evidence['kind'], evidence['source_urls']
        if song.get('kind') != kind or song.get('type') != kind or song.get('kind_source_urls') != urls:
            song.update(kind=kind, type=kind, kind_source_urls=list(urls))
            changed += 1
    return changed


def recording_kind(title, artists=(), releases=()):
    if any(name.strip().casefold() == '[dialogue]' for name in artists):
        return 'spoken'
    text = unicodedata.normalize('NFKC', title).casefold()
    if any(word in text for word in ('カラオケ', 'off vocal', 'off-vocal', 'instrumental',
                                     'karaoke', 'インスト', 'offvocal')):
        return 'instrumental'
    if (re.match(r'^(?:(?:ボーナス|ミニ|オーディオ)\s*)?ドラマ(?:[\s「(（:：～-]|$)', text)
            or re.match(r'^(?:ボーナス\s*)?トーク(?:[\s\d「(（:：～-]|$)', text)
            or re.match(r'^(?:talk|drama|mc)(?:\s+\d|\s*[-:(])', text)
            or re.match(r'^(?:ボイスメッセージ|mcパート|朗読劇|なんちゃってコメンタリー)', text)
            or '【ドラマ】' in text):
        return 'spoken'
    spoken_album = any(re.search(r'ドラマ\s*cd|cd\s*ドラマ|\bdjcd\b',
                                  unicodedata.normalize('NFKC', item.get('title', '')).casefold())
                       for item in releases if isinstance(item, dict))
    if spoken_album:
        if re.search(r'\b(?:chapter|scene)[\s.:]*\d', text):return 'spoken'
        # Drama/radio CDs may contain theme songs. Ambiguous bonus tracks stay
        # unknown instead of silently becoming vocal-song random candidates.
        return 'unknown'
    return 'song'


def is_franchise_release(title):
    text = unicodedata.normalize('NFKC', title).strip().casefold()
    return bool(re.match(r'^(?:the\s+)?(?:idolm@ster|アイドルマスター)(?:\b|[\s!:：])', text))


def catalog_queries(performers=()):
    """Include character credits when the album title omits franchise branding.

    Search names discover candidates only; normalization still requires explicit
    franchise identity in the returned artist entity and an Official release.
    """
    other = sorted({p['name'] for p in performers
                    if p.get('kind') == 'idol' and 'gakuen' not in p.get('series', [])})
    additions = []
    for start in range(0, len(other), 24):
        names_query = ' OR '.join('artistname:"' + name.replace('"', '\\"') + '"'
                                  for name in other[start:start + 24])
        additions.append('(' + names_query + ') AND NOT release:"THE IDOLM@STER"')
    names = sorted({p['name'] for p in performers
                    if p.get('kind') == 'idol' and 'gakuen' in p.get('series', [])})
    if not names:
        return MB_QUERIES + tuple(additions)
    query = ' OR '.join('artistname:"' + name.replace('"', '\\"') + '"' for name in names)
    return MB_QUERIES + tuple(additions) + (query,)


def franchise_artist_evidence(credits):
    """Accept explicit fictional/ensemble identities, never an actor's career.

    A character's brand identifies franchise membership only. It does not assign
    a recording to every overlapping series in which that character appears.
    """
    evidence = []
    for credit in credits:
        artist = credit.get('artist', {})
        name = artist.get('name', credit.get('name', ''))
        comment = unicodedata.normalize('NFKC', artist.get('disambiguation', '')).strip()
        text = (name + ' ' + comment).casefold()
        if (artist.get('type') == 'Person' or any(word in text for word in
                ('voice actor', 'voice actress', 'seiyuu', 'composer', 'arranger', 'cover artist',
                 'cover band', 'fan unit', 'unofficial', 'tribute', '声優', '作曲家',
                 'アレンジャー', '非公式', '同人'))):
            continue
        explicit = (artist.get('id') in FRANCHISE_ENSEMBLES
                    or is_franchise_release(name) or is_franchise_release(comment)
                    or name == '初星学園' or comment.startswith('学園アイドルマスター'))
        if explicit:
            evidence.append({'id': artist.get('id', ''), 'name': name,
                             'disambiguation': comment,
                             'source_url': 'https://musicbrainz.org/artist/' + artist['id']
                             if artist.get('id') else ''})
    return evidence


def compact_mb_response(value):
    """Keep only core facts, avoiding ratings/tags/annotations and bulky aliases."""
    if not isinstance(value, dict) or not isinstance(value.get('recordings'), list):
        raise ValueError('MusicBrainz 응답 형식이 올바르지 않습니다.')
    if any(type(value.get(field)) is not int or value[field] < 0 for field in ('count', 'offset')):
        raise ValueError('MusicBrainz 검색 개수·위치가 올바르지 않습니다.')
    recordings = []
    for item in value['recordings']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not item['id'].strip() or not isinstance(item.get('title'), str)
                or not item['title'].strip()):
            raise ValueError('MusicBrainz 녹음 식별자·제목 형식이 올바르지 않습니다.')
        raw_credits = item.get('artist-credit', [])
        raw_releases = item.get('releases', [])
        if not isinstance(raw_credits, list) or not isinstance(raw_releases, list):
            raise ValueError('MusicBrainz 가창자·발매 목록 형식이 올바르지 않습니다.')
        if (item.get('first-release-date') is not None and not isinstance(item['first-release-date'], str)
                or not isinstance(item.get('isrcs', []), list)
                or any(not isinstance(code, str) for code in item.get('isrcs', []))):
            raise ValueError('MusicBrainz 발매일·녹음 코드 형식이 올바르지 않습니다.')
        credits = []
        for credit in raw_credits:
            if (not isinstance(credit, dict) or not isinstance(credit.get('artist', {}), dict)
                    or any(credit.get(field) is not None and not isinstance(credit[field], str)
                           for field in ('name', 'joinphrase'))):
                raise ValueError('MusicBrainz 가창자 크레딧 형식이 올바르지 않습니다.')
            artist = credit.get('artist', {})
            if any(artist.get(field) is not None and not isinstance(artist[field], str)
                   for field in ('id', 'name', 'disambiguation', 'type')):
                raise ValueError('MusicBrainz 가창자 정보 형식이 올바르지 않습니다.')
            credits.append({'name': credit.get('name') or artist.get('name') or '',
                            'joinphrase': credit.get('joinphrase') or '',
                            'artist': {k: artist[k] for k in ('id', 'name', 'disambiguation', 'type')
                                       if artist.get(k) is not None}})
        releases = []
        for release in raw_releases:
            if (not isinstance(release, dict) or any(release.get(field) is not None and not isinstance(release[field], str)
                                                   for field in ('id', 'title', 'date', 'status'))):
                raise ValueError('MusicBrainz 발매 정보 형식이 올바르지 않습니다.')
            releases.append({k: release[k] for k in ('id', 'title', 'date', 'status') if release.get(k) is not None})
        recordings.append({'id': item['id'], 'title': item['title'],
                           'length': item.get('length'), 'artist-credit': credits,
                           'first-release-date': item.get('first-release-date') or '',
                           'releases': releases, 'isrcs': item.get('isrcs', []),
                           'video': item.get('video', False)})
    return {'count': value['count'], 'offset': value['offset'],
            'recordings': recordings}


def fetch_musicbrainz(*, cache_directory=None, resume=False, progress=None, cancel=None,
                     fetcher=fetch_bytes, queries=MB_QUERIES, interval=1.1):
    """Import two bounded searches; cached pages are used only for build resumption.

    On an incomplete query raise, so the caller retains the previous complete
    MusicBrainz snapshot. No deletion is inferred from a search response.
    """
    start = time.monotonic()
    last_request = 0.0
    cache = Path(cache_directory) if cache_directory else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    rows = {}
    query_stats = []
    request_count = 0

    def get_page(query, offset):
        nonlocal last_request, request_count
        request_count += 1
        if request_count > MAX_PAGES:
            raise ValueError('MusicBrainz 검색 페이지 제한을 초과했습니다.')
        digest = hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]
        page_path = cache / f'musicbrainz-{digest}-{offset:05}.json' if cache else None
        value = None
        if resume and page_path and page_path.exists():
            try:
                value = compact_mb_response(json.loads(page_path.read_text(encoding='utf-8')))
            except (OSError, ValueError, AttributeError):
                value = None
        if value is None:
            _pause(max(0, interval - (time.monotonic() - last_request)), cancel)
            url = MB_URL + '?' + urlencode({'query': query, 'fmt': 'json',
                                            'limit': 100, 'offset': offset})
            last_request = time.monotonic()
            for attempt in range(2):
                try:
                    value = compact_mb_response(json.loads(fetcher(url, cancel=cancel)))
                    break
                except CatalogCancelled:
                    raise
                except (OSError, ValueError):
                    if attempt:
                        raise
                    _pause(3, cancel)
                    last_request = time.monotonic()
            if page_path:
                page_path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        if value['offset'] != offset:
            raise ValueError('MusicBrainz 검색 응답의 시작 위치가 요청과 다릅니다.')
        return value

    def uuid_text(number):
        value = f'{number:032x}'
        return f'{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}'

    for query_index, base_query in enumerate(queries):
        first = get_page(base_query, 0)
        expected = first['count']
        ids = set()
        # MusicBrainz limits indexed searches to 500 results. Restrict each
        # range to one page as score-tied pages can overlap across search replicas.
        # Disjoint MBID ranges avoid silently losing recordings to unstable paging.
        ranges = [(0, (1 << 128) - 1, first)]
        if expected > 100:
            parts = 128 if expected > 8000 else 8
            width = (1 << 128) // parts
            ranges = [(i * width, (i + 1) * width - 1, None) for i in range(parts)]
        while ranges:
            check_cancel(cancel)
            if time.monotonic() - start > MAX_SECONDS:
                raise TimeoutError('MusicBrainz 업데이트 제한 시간을 초과했습니다.')
            low, high, value = ranges.pop(0)
            query = (base_query if low == 0 and high == (1 << 128) - 1 else
                     f'({base_query}) AND rid:[{uuid_text(low)} TO {uuid_text(high)}]')
            value = value if value is not None else get_page(query, 0)
            if value['count'] > 100:
                midpoint = (low + high) // 2
                if midpoint == low:
                    raise ValueError('MusicBrainz 식별자 범위를 더 좁힐 수 없습니다.')
                ranges[:0] = [(low, midpoint, None), (midpoint + 1, high, None)]
                continue
            offset = 0
            count = value['count']
            range_ids = set()
            while True:
                check_cancel(cancel)
                if value['count'] != count:
                    raise ValueError('MusicBrainz 검색 결과 개수가 수집 중 변경되었습니다.')
                page = value['recordings']
                if not page and offset < count:
                    raise ValueError('MusicBrainz 검색 결과가 중간에 비었습니다.')
                for row in page:
                    if row['id'] in range_ids or row['id'] in ids:
                        raise ValueError('MusicBrainz 검색 결과가 중복되어 완전성을 확인할 수 없습니다.')
                    range_ids.add(row['id'])
                    ids.add(row['id'])
                    rows[row['id']] = row
                offset += len(page)
                notify(progress, f'MusicBrainz {query_index + 1}/{len(queries)}: {len(ids):,}/{expected:,} 녹음 확인')
                if offset >= count:
                    break
                if offset + 100 > 500:
                    raise ValueError('MusicBrainz 검색 결과가 수집 중 변경되었습니다. 다시 시도해 주세요.')
                value = get_page(query, offset)
            if len(range_ids) != count:
                raise ValueError('MusicBrainz 검색 개수와 확인한 고유 녹음 수가 다릅니다.')
        if len(ids) != expected:
            raise ValueError('MusicBrainz 전체 검색 개수와 확인한 고유 녹음 수가 다릅니다.')
        query_stats.append({'query': base_query, 'count': expected, 'downloaded': len(ids)})
    return list(rows.values()), query_stats


def normalize_musicbrainz(recordings, *, fetched_at=''):
    songs = []
    reviewed = _reviewed_recording_releases()
    for row in recordings:
        identifier = row.get('id', '')
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', identifier) or not row.get('title'):
            continue
        artists = unique(c.get('name', c.get('artist', {}).get('name', ''))
                         for c in row.get('artist-credit', []))
        releases = row.get('releases', [])
        # A search hit is not proof that every associated release is official.
        official = [r for r in releases if r.get('status') == 'Official']
        reviewed_matches = _reviewed_release_matches(identifier, row['title'], official, reviewed)
        matching = [r for r in official if is_franchise_release(r.get('title', ''))
                    or r.get('id') in reviewed_matches]
        artist_evidence = franchise_artist_evidence(row.get('artist-credit', []))
        gakuen = any('学園アイドルマスター' in c.get('artist', {}).get('disambiguation', '')
                     or c.get('name') == '初星学園' for c in row.get('artist-credit', []))
        if not official or not matching and not artist_evidence:
            continue  # Unofficial fan releases and ambiguous hits are not an official catalog.
        applicable = matching or official
        brand, series_evidence, series_urls = recording_series_evidence(row['title'], applicable,
            recording_id=identifier, reviewed=reviewed)
        if gakuen and 'gakuen' not in brand:
            brand.append('gakuen')
        kind = recording_kind(row['title'], artists, applicable)
        source_url = 'https://musicbrainz.org/recording/' + identifier
        song = {'id': 'mb:' + identifier, 'title': row['title'],
                'series': brand or ['other'], 'artists': artists,
                'album': applicable[0].get('title', '') if applicable else '',
                'source_url': source_url, 'url': source_url, 'aliases': [],
                'kind': kind, 'type': kind,
                'source_id': 'musicbrainz',
                'first_release_date': row.get('first-release-date', ''),
                'provenance': [{'source_id': 'musicbrainz', 'url': source_url,
                                'fetched_at': fetched_at, 'license': 'CC0-1.0'}],
                'series_evidence': series_evidence,
                'missing': ['spotify', 'genre', 'work_link']}
        if artist_evidence:
            song['franchise_artist_evidence'] = artist_evidence
        if series_urls:
            song['series_source_urls'] = series_urls
        length = row.get('length')
        if isinstance(length, (int, float)) and not isinstance(length, bool) and 0 < length <= 86400000:
            song['duration'] = round(length / 1000, 3)
        else:
            song['missing'].append('duration')
        if not artists:
            song['missing'].append('artists')
        if not applicable:
            song['missing'].append('album')
        if not brand:
            song['missing'].append('series')
        song['recordings'] = [{'id': 'mb:' + identifier, 'mbid': identifier, 'title': row['title'],
                               'artists': artists, 'duration': song.get('duration'),
                               'isrcs': unique(row.get('isrcs', [])), 'source_url': source_url}]
        song['releases'] = []
        for release in applicable:
            if not release.get('id'):
                continue
            known = reviewed_matches.get(release['id'], {})
            date = release.get('date') or known.get('date', '')
            normalized_release = {'id': release['id'], 'title': release.get('title', ''),
                                  'date': date, 'url': 'https://musicbrainz.org/release/' + release['id']}
            if known.get('date') and date == known['date']:
                normalized_release.update(date_source_url=known['date_source_url'],
                                          date_source_kind=known['date_source_kind'])
            song['releases'].append(normalized_release)
        # Keep the credited string (including CV parentheses) for display and matching.
        song['artist_credit'] = ''.join(c.get('name', '') + c.get('joinphrase', '')
                                        for c in row.get('artist-credit', []))
        songs.append(song)
    reclassify_reviewed_kinds(songs)
    enrich_reviewed_releases(songs)
    return songs


def parse_imasparql(data, *, fetched_at=''):
    """Read selected RDF files in memory. Never extract or execute archive paths."""
    file_series = {'765AS.rdf': '765', '765MillionStars.rdf': 'million',
                   'CinderellaGirls.rdf': 'cinderella', 'SideM.rdf': 'sidem',
                   '283.rdf': 'shiny', 'ShinyColors.rdf': 'shiny',
                   'MillionLive.rdf': 'million', 'Gakuen.rdf': 'gakuen',
                   '876.rdf': 'other', '961.rdf': 'other'}
    entities = {}
    recordings = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 5000:
            raise ValueError('RDF 압축 파일 항목 수를 초과했습니다.')
        for info in archive.infolist():
            path = info.filename
            basename = path.rsplit('/', 1)[-1]
            is_unit = '/RDFs/Units/' in path
            if not ('/RDFs/' in path and (basename in file_series or basename == 'Song.rdf')):
                continue
            if info.file_size > 12 * 1024 * 1024:
                raise ValueError('RDF 파일 크기를 초과했습니다.')
            root = ET.fromstring(archive.read(info))
            url = 'https://github.com/imas/imasparql/blob/master/' + path.split('/', 1)[1]
            for node in root:
                resource_id = node.get(RDF + 'about')
                if not resource_id:
                    continue
                name = node.findtext(SCHEMA + 'name') or node.findtext(RDFS + 'label')
                if not name:
                    continue
                if basename == 'Song.rdf':
                    recordings.append((node, url))
                    continue
                types = [n.get(RDF + 'resource', '') for n in node.findall(RDF + 'type')]
                if not any(t.endswith(('Idol', 'Unit')) for t in types):
                    continue
                identifier = 'imas:' + unquote(resource_id)
                entities[resource_id] = {
                    'id': identifier, 'name': name, 'kind': 'group' if is_unit else 'idol',
                    'series': [file_series[basename]],
                    'aliases': unique([node.findtext(IMAS + 'nameKana') or '',
                                       node.findtext(IMAS + 'nameEnglish') or '']),
                    'members_ids': [n.get(RDF + 'resource', '') for n in node.findall(SCHEMA + 'member')],
                    'cv': unique(n.text for n in node.findall(IMAS + 'cv') if n.text),
                    'color': node.findtext(IMAS + 'Color') or '',
                    'source_url': url, 'provenance': [{'source_id': 'imasparql', 'url': url,
                                                     'fetched_at': fetched_at, 'license': 'MIT'}]}
        for entity in entities.values():
            entity['members'] = [entities[r]['name'] for r in entity['members_ids'] if r in entities]
            entity['members_ids'] = ['imas:' + unquote(r) for r in entity['members_ids']]
        songs = []
        for node, url in recordings:
            resource_id = node.get(RDF + 'about')
            name = node.findtext(SCHEMA + 'name')
            refs = [n.get(RDF + 'resource', '') for n in node.findall(SCHEMA + 'byArtist')]
            artists = [entities[r]['name'] for r in refs if r in entities]
            identifier = 'imas:' + unquote(resource_id)
            duration_match = re.fullmatch(r'PT(?:(\d+)M)?(?:(\d+)S)?', node.findtext(SCHEMA + 'duration') or '')
            duration = (int(duration_match[1] or 0) * 60 + int(duration_match[2] or 0)) if duration_match else None
            # This tiny RDF source explicitly represents 765's 2005 MASTERPIECE 01.
            # Keep unmatched metadata unclassified; do not infer membership from singers.
            songs.append({'id': identifier, 'title': name, 'series': ['other'],
                          'artists': artists, 'duration': duration, 'source_url': url, 'url': url,
                          'source_id': 'imasparql', 'kind': recording_kind(name),
                          'aliases': [], 'missing': ['album', 'series', 'spotify', 'genre'],
                          'recordings': [{'id': identifier, 'title': name, 'artists': artists,
                                          'duration': duration,
                                          'isrcs': unique([node.findtext(SCHEMA + 'isrcCode') or '']),
                                          'source_url': url}],
                          'provenance': [{'source_id': 'imasparql', 'url': url,
                                          'fetched_at': fetched_at, 'license': 'MIT'}]})
    return songs, list(entities.values())


def enrich_performers(songs, performers, *, names_directory=None):
    """Join exact names/verified aliases only; no fuzzy identity or song grouping."""
    aliases = {}
    if names_directory is not None:
        for path in sorted(Path(names_directory).glob('*.json')):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                for item in value.get('entries', []):
                    values = unique([item.get('name', ''), item.get('ko', '')] + item.get('aliases', []))
                    for name in values:
                        aliases.setdefault(key(name), set()).update(values)
            except (OSError, ValueError, TypeError):
                continue
    lookup = {}
    for performer in performers:
        names = [performer['name']] + performer.get('aliases', [])
        extra = unique(a for n in names for a in sorted(aliases.get(key(n), ())))
        performer['aliases'] = unique(performer.get('aliases', []) + extra)
        for name in names + extra:
            lookup.setdefault(key(name), []).append(performer)
    for song in songs:
        related = []
        groups = []
        search_aliases = []
        for name in song['artists']:
            exact = lookup.get(key(name), [])
            if len({p['id'] for p in exact}) == 1:
                performer = exact[0]
                related.append(performer['id'])
                search_aliases.extend(performer.get('aliases', []))
                if performer['kind'] == 'group':
                    groups.append(performer['name'])
                    search_aliases.extend(performer.get('members', []))
                    for member in performer.get('members', []):
                        search_aliases.extend(sorted(aliases.get(key(member), ())))
            search_aliases.extend(sorted(aliases.get(key(name), ())))
        song['performer_ids'] = unique(related)
        song['groups'] = unique(groups)
        song['performer_aliases'] = unique(search_aliases)
    return songs


def refresh_catalog(destination, progress=None, cancel=None, **options):
    """Public UI entry point; imported lazily to avoid an import cycle."""
    from song_catalog import refresh_catalog as refresh
    return refresh(destination, progress, cancel, **options)
