"""Review every previously indexed fan part candidate against independent lyrics.

Only public title/performer metadata is sent to search. External text and HTML
stay in memory. Checkpoints contain hashes, character spans and factual credits.
No playback, account API, source-cache or application-resource changes occur.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lyrics import Line, normalized
from lyric_parts import LyricParts, _recording
from media import Playback
from song_catalog import SongCatalog
from tools.build_lyric_part_expansion import (
    aligned_values, atomic_json, compatible, digest, key_of, parse_page,
    parts_of, person_maps, SKIP_ALBUM,
)

INPUT = ROOT / 'data/diagnostics/lyric_parts_expansion_candidate.json'
OUTPUT = ROOT / 'data/diagnostics/validated-catalog-parts.json'
MASTER = re.compile(r'\s*[（(]\s*M@STER\s+VERSION\s*[)）]\s*$', re.I)
VERSION = re.compile(r'game\s*(?:size|ver)|live\s*(?:size|ver)|202\d\s*(?:ver|version)|solo|remix|re.mix|リミックス', re.I)


def clean_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


class PublicClient:
    def __init__(self):
        self.memory = {}
        self.last_request = 0.0
        self.requests = 0
        self.artist_searches = set()
        self.rate_limits = []
        self.blocked_until = {}

    def get(self, url):
        if url in self.memory:
            return self.memory[url]
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('ameblo.jp', 'www.uta-net.com'):
            raise ValueError('unsupported public host')
        remaining = self.blocked_until.get(parsed.hostname, 0) - time.monotonic()
        if remaining > 60:
            raise ValueError(f'transport_limited: {parsed.hostname}; retry_after={int(remaining) + 1}s')
        if remaining > 0:
            time.sleep(remaining)
        time.sleep(max(0, 2.0 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay factual-credit research'})
        for attempt in range(2):
            try:
                with urlopen(request, timeout=18) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                break
            except HTTPError as error:
                if error.code != 429:
                    raise
                wait = retry_after_seconds(error.headers.get('Retry-After', ''))
                self.blocked_until[parsed.hostname] = time.monotonic() + wait
                self.rate_limits.append({'host': parsed.hostname, 'url': clean_url(url),
                                         'retry_after_seconds': wait})
                if attempt or wait > 60:
                    raise ValueError(f'transport_limited: {parsed.hostname}; retry_after={wait}s') from error
                time.sleep(wait)
                self.last_request = time.monotonic()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('public document size limit')
        result = raw.decode('utf-8')
        self.requests += 1
        self.memory[url] = result
        return result


def retry_after_seconds(value, now=None):
    if value.isdigit():
        return max(2, int(value))
    try:
        deadline = parsedate_to_datetime(value)
        return max(2, int((deadline - (now or datetime.now(timezone.utc))).total_seconds()) + 1)
    except (TypeError, ValueError):
        return 15


def release_dates(identities, album):
    # Recording-level first-release-date and dates of other reissues are not
    # evidence for this profile's album. Only the named release may anchor it.
    return sorted({release['date'] for song in identities for release in song.get('releases', [])
                   if normalized(release.get('title', '')) == normalized(album)
                   and isinstance(release.get('date'), str)
                   and re.fullmatch(r'\d{4}-\d{2}-\d{2}', release['date'])})


def parse_search(html):
    soup = BeautifulSoup(html, 'html.parser')
    entries = []
    for row in soup.select('table.songlist-table tr'):
        title = row.select_one('.songlist-title')
        if title is None:
            continue
        link = title.find_parent('a')
        cells = row.find_all('td', recursive=False)
        if link is None or not re.fullmatch(r'/song/\d+/', link.get('href', '')):
            continue
        artist = cells[1].get_text(' ', strip=True) if len(cells) > 1 else ''
        if not artist:
            mobile = row.select_one('.utaidashi')
            artist = mobile.get_text(' ', strip=True) if mobile else ''
        entries.append({'title': title.get_text(' ', strip=True), 'artist': artist,
                        'url': urljoin('https://www.uta-net.com', link['href'])})
    return entries


def parse_independent(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.select_one('h2.kashi-title')
    artist = soup.select_one('h3[itemprop="recordedAs"] [itemprop="byArtist"]')
    text = soup.select_one('#kashi_area')
    if title is None or artist is None or text is None:
        raise ValueError('independent song metadata/text unavailable')
    for reading in text.select('rt, rp, script, style'):
        reading.decompose()
    lines = [Line(row.strip(), '', '') for row in text.get_text('\n').splitlines() if normalized(row)]
    release = re.search(r'発売日\s*[：:]\s*(\d{4})/(\d\d)/(\d\d)', soup.get_text(' ', strip=True))
    return {'title': title.get_text(' ', strip=True), 'artist': artist.get_text(' ', strip=True),
            'artist_url': urljoin('https://www.uta-net.com', artist.get('href', '')),
            'release_date': '-'.join(release.groups()) if release else '',
            'lines': lines, 'text': ''.join(normalized(line.original) for line in lines)}


@lru_cache(maxsize=1)
def character_voice_aliases():
    """Read only explicit character/CV relations and documented actor aliases."""
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    actors = []
    for path in sorted((ROOT / 'resources/artist_names').glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        for entry in data.get('entries', []):
            if entry.get('kind') == 'voice_actor':
                actors.append({normalized(value) for value in
                    [entry.get('name', ''), entry.get('ko', ''), *entry.get('aliases', [])] if value})
    result = {}
    for person in metadata['members']:
        aliases = set()
        for voice in person.get('voice_actors', []):
            original = {normalized(value) for value in
                [voice.get('name', ''), voice.get('display', ''), *voice.get('aliases', [])] if value}
            aliases.update(original)
            # Resolve the canonical CV first; never borrow aliases merely from
            # another actor credited on the same song or a broad brand group.
            for actor in actors:
                if original.intersection(actor):
                    aliases.update(actor)
        result[person['name'].replace(' ', '').replace('　', '')] = aliases
    return result


def resolved_credit(credit, people, lookup, groups):
    """Resolve every token, including character(CV), without ignoring extras."""
    expected, voices = frozenset(people), character_voice_aliases()

    def atomic(value, allow_voice=True):
        key = normalized(value)
        if key in lookup:
            return frozenset((lookup[key],)) if lookup[key] in expected else None
        rosters = groups.get(key, ())
        if rosters:
            return expected if set(rosters) == {expected} else None
        owners = frozenset(person for person in expected if key in voices.get(person, ()))
        return owners if allow_voice and owners else None

    direct = atomic(credit)
    if direct:
        return direct
    document = unicodedata.normalize('NFKC', credit).strip()
    tokens = re.split(r'[,、・×＆&]', document)
    seen = set()
    for token in tokens:
        token = token.strip()
        if not token:
            return None
        named = re.fullmatch(r'([^()]+)\((?:CV[.:]?\s*)?([^()]+)\)', token, re.I)
        if named:
            character = atomic(named[1], allow_voice=False)
            actor_key = normalized(named[2])
            if not character or len(character) != 1 or not all(
                    actor_key in voices.get(person, ()) for person in character):
                return None
            seen.update(character)
        else:
            resolved = atomic(token)
            if not resolved:
                return None
            seen.update(resolved)
    return frozenset(seen) if seen else None


def credit_matches(credit, people, lookup, groups):
    return resolved_credit(credit, people, lookup, groups) == frozenset(people)


def catalog_credits_match(artists, people, lookup, groups):
    resolved = [resolved_credit(artist, people, lookup, groups) for artist in artists]
    return bool(resolved) and all(resolved) and frozenset().union(*resolved) == frozenset(people)


def title_matches(candidate_title, independent_title, fan_heading):
    if normalized(candidate_title) == normalized(independent_title):
        return True
    # Some lyric services omit the M@STER VERSION suffix. Permit that only
    # when the part source explicitly names that suffix and fulltext matches.
    base = MASTER.sub('', candidate_title)
    return (base != candidate_title and normalized(base) == normalized(independent_title)
            and normalized(candidate_title) in normalized(fan_heading))


def fan_heading_matches(title, heading):
    document = unicodedata.normalize('NFKC', heading.split('|', 1)[0]).strip()
    # These are the actual, reviewed article decorations. Unknown extra text
    # (Acoustic, Another Story, named unit versions, etc.) is not discarded.
    document = re.sub(r'\s+(?:歌割り\s*\(パート分け\)|歌割り|歌詞|パート分け(?:\s+歌詞)?)\s*$', '', document)
    return bool(normalized(title)) and normalized(document) == normalized(title)


def deduplicate_profiles(profiles):
    grouped = defaultdict(list)
    for profile in profiles:
        grouped[key_of(profile)].append(profile)
    good, conflicts = [], []
    for key, choices in grouped.items():
        if any(profile['parts'] != choices[0]['parts'] for profile in choices[1:]):
            conflicts.append(key)
        else:
            good.append(choices[0])
    return good, conflicts


def replay(profile, lines):
    parsed = _recording(profile)
    if parsed is None:
        raise ValueError('invalid part profile')
    catalog = LyricParts(resource_path=ROOT / 'data/diagnostics/__absent_lyric_parts__.json')
    catalog.recordings = [parsed]
    playback = Playback(profile['title'], profile['artists'][0], profile['album'])
    complete = catalog.match(playback, lines)
    partial = catalog.match(playback, lines, allow_partial=True)
    return {'rows': len(lines), 'complete_singer_rows': sum(bool(row) for row in complete),
            'partial_singer_rows': sum(bool(b) and not bool(a) for a, b in zip(complete, partial)),
            'known_singer_rows': sum(bool(row) for row in partial), 'parts': len(profile['parts'])}


def make_profile(candidate, text, values, url, independent_url):
    return {'title': candidate['title'], 'artists': candidate['artists'], 'album': candidate['album'],
            'text_sha256': digest(text), 'text_length': len(text),
            'sources': [{'url': clean_url(url), 'kind': 'fan_part_transcription',
                'note': '팬 표의 명시 인물 색상·구간을 독립 공개 가사 원문과 전체 대조함. '
                        '표시 없는 구간·서로 다른 녹음은 추정하지 않음.'},
                {'url': clean_url(independent_url), 'kind': 'independent_lyrics',
                 'note': '공개 가사 서비스의 전체 원문·가창자·발매일 대조. 개별 구절 가창의 공식 증명은 아님.'}],
            'parts': parts_of(values)}


def source_metadata(html):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('.skin-entryTitle') or soup.select_one('h1') or soup.title
    return heading.get_text(' ', strip=True) if heading else ''


def catalog_index(songs):
    indexed = defaultdict(list)
    for song in songs:
        indexed[normalized(song['title'])].append(song)
    return indexed


def catalog_identity(candidate, songs, people, lookup, groups):
    if (SKIP_ALBUM.search(candidate.get('album', '')) or VERSION.search(candidate['title'])
            or re.search(r'\bremix\b|リミックス|re-record', candidate.get('album', ''), re.I)):
        return [], 'different_recording_version'
    pool = songs.get(normalized(candidate['title']), ()) if isinstance(songs, dict) else songs
    candidates = [song for song in pool if normalized(song['title']) == normalized(candidate['title'])
                  and normalized(candidate.get('album', '')) in {
                      normalized(song.get('album', '')),
                      *(normalized(release.get('title', '')) for release in song.get('releases', []))}
                  and song.get('kind') == 'song'
                  and catalog_credits_match(song.get('artists', []), people, lookup, groups)]
    candidate_artists = {normalized(artist) for artist in candidate.get('artists', [])}
    candidates = [song for song in candidates if candidate_artists and candidate_artists.issubset(
        {normalized(artist) for artist in song.get('artists', [])})]
    if not candidates:
        return [], 'catalog_recording_or_lineup_unconfirmed'
    return candidates, ''


def independent_candidates(client, title, people, lookup, groups, known, fan_heading, artists=()):
    key = normalized(title)
    result = [entry for entry in known.get(key, []) if credit_matches(entry['artist'], people, lookup, groups)]
    if result:
        return result
    for artist in artists:
        if not compatible([artist], people, lookup, groups) or normalized(artist) in client.artist_searches:
            continue
        client.artist_searches.add(normalized(artist))
        artist_search = 'https://www.uta-net.com/search/?' + urlencode({'target': 'art', 'type': 'in', 'Keyword': artist})
        soup = BeautifulSoup(client.get(artist_search), 'html.parser')
        for link in soup.select('a[href^="/artist/"]'):
            label = re.sub(r'\s*歌詞\s*[：:]\s*\d+\s*$', '', link.get_text(' ', strip=True))
            if not credit_matches(label, people, lookup, groups):
                continue
            for entry in parse_search(client.get(urljoin('https://www.uta-net.com', link['href']))):
                bucket = known.setdefault(normalized(entry['title']), [])
                if entry not in bucket:
                    bucket.append(entry)
        result = [entry for entry in known.get(key, []) if credit_matches(entry['artist'], people, lookup, groups)]
        if result:
            return result
    query = MASTER.sub('', title)
    url = 'https://www.uta-net.com/search/?' + urlencode({'target': 'title', 'type': 'in', 'Keyword': query})
    entries = parse_search(client.get(url))
    for entry in entries:
        bucket = known.setdefault(normalized(entry['title']), [])
        if entry not in bucket:
            bucket.append(entry)
    return [entry for entry in entries if title_matches(title, entry['title'], fan_heading)
            and credit_matches(entry['artist'], people, lookup, groups)]


def save_result(outcomes, profiles, input_titles, client, path=OUTPUT):
    unique = {}
    for profile in profiles:
        key = key_of(profile)
        if key not in unique:
            unique[key] = profile
        elif unique[key]['parts'] != profile['parts']:
            # Keep conflicting sources separate; the runtime resolves no winner.
            unique[(key, tuple(s['url'] for s in profile['sources']))] = profile
    complete = len(outcomes) == len(input_titles)
    value = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
             'status': 'complete' if complete else 'in_progress', 'input_title_count': len(input_titles),
             'completed_title_count': len(outcomes), 'input_titles': input_titles,
             'network_requests_this_run': client.requests,
             'rate_limit_events_this_run': client.rate_limits,
             'profiles': {'schema_version': 1, 'recordings': list(unique.values())},
             'outcomes': outcomes,
             'scope': 'Only the previous 211-title candidate set; the separate 111-entry SideM source index is excluded.',
             'limitations': ['Singer allocation is sourced from fan transcriptions, not official isolated-vocal credits.',
                 'Independent lyric services verify whole text and credited performers, not individual line allocation.',
                 'A verified profile is replayed against its actual in-memory text; Spotify/display success requires its own metadata and text match.',
                 'No public lyric text, HTML, audio, user lyric input, or account information is saved or uploaded.']}
    atomic_json(path, value)
    return value


def finalize_result(path=OUTPUT):
    """Reapply final metadata/version/conflict gates without refetching text."""
    value = json.loads(path.read_text(encoding='utf-8'))
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, groups = person_maps(metadata)
    songs = catalog_index(SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs)
    outcomes = {normalized(row['title']): row for row in value['outcomes']}
    # Older checkpoints predate the stricter album/credit gates. Recompute
    # every recorded diagnostic so even held entries cannot retain an old True.
    for outcome in value['outcomes']:
        for evidence in outcome['evidence']:
            people = evidence['legend_people']
            for check in evidence['independent_checks']:
                check['performer_roster_matches'] = credit_matches(check['artist'], people, lookup, groups)
                for record in check['recordings']:
                    candidate = {'title': outcome['title'], **record}
                    identities, _ = catalog_identity(candidate, songs, people, lookup, groups)
                    dates = release_dates(identities, record['album'])
                    record.update({'catalog_performer_roster_matches': bool(identities),
                        'catalog_release_dates': dates, 'release_anchor_album': record['album'],
                        'part_source_title_matches_profile': fan_heading_matches(outcome['title'], evidence['fan_heading']),
                        'lyric_service_release_matches_catalog': bool(check['release_date']) and check['release_date'] in dates})
    profiles, conflicts = deduplicate_profiles(value['profiles']['recordings'])
    kept, rejected = [], list(value.get('final_gate_rejections', []))
    for profile in profiles:
        outcome = outcomes[normalized(profile['title'])]
        accepted, reason, independent_url = False, 'final_evidence_missing', ''
        fan_urls = {clean_url(source['url']) for source in profile['sources']
                    if source['kind'] == 'fan_part_transcription'}
        for evidence in outcome['evidence']:
            if clean_url(evidence['part_source_url']) not in fan_urls:
                continue
            if not fan_heading_matches(profile['title'], evidence['fan_heading']):
                reason = 'part_source_title_or_version_mismatch'
                continue
            identities, reason = catalog_identity(profile, songs, evidence['legend_people'], lookup, groups)
            if reason:
                continue
            dates = release_dates(identities, profile['album'])
            for check in evidence['independent_checks']:
                if check['text_sha256'] != profile['text_sha256'] or not check['whole_text_alignment']:
                    continue
                if check['release_date'] not in dates:
                    reason = 'release_identity_unconfirmed'
                    continue
                if not title_matches(profile['title'], check['title'], evidence['fan_heading']) or not credit_matches(
                        check['artist'], evidence['legend_people'], lookup, groups):
                    reason = 'independent_recording_metadata_mismatch'
                    continue
                records = [record for record in check['recordings'] if
                    key_of({'title': profile['title'], **record}) == key_of(profile)
                    and record['runtime_replay']['known_singer_rows'] > 0]
                if records:
                    for record in records:
                        record['catalog_release_dates'] = dates
                        record['lyric_service_release_matches_catalog'] = True
                    accepted, independent_url = True, check['url']
                    break
            if accepted:
                break
        if accepted:
            source = {'url': clean_url(independent_url), 'kind': 'independent_lyrics',
                      'note': '공개 가사 서비스의 전체 원문·가창자·발매일 대조. 개별 구절 가창의 공식 증명은 아님.'}
            if not any(row['url'] == source['url'] and row['kind'] == source['kind'] for row in profile['sources']):
                profile['sources'].append(source)
            if _recording(profile) is None:
                raise ValueError('final profile invalid')
            kept.append(profile)
        else:
            rejected.append({'title': profile['title'], 'album': profile['album'],
                             'text_sha256': profile['text_sha256'], 'reason': reason})
    value['profiles']['recordings'] = kept
    for outcome in value['outcomes']:
        matched = [profile for profile in kept if normalized(profile['title']) == normalized(outcome['title'])]
        outcome['verified_profiles'] = [{key: profile[key] for key in
            ('title', 'album', 'artists', 'text_sha256', 'text_length')} for profile in matched]
        reasons = [row['reason'] for row in rejected if normalized(row['title']) == normalized(outcome['title'])]
        if any(key[0] == normalized(outcome['title']) for key in conflicts):
            reasons.append('conflicting_part_assignments')
        if matched:
            outcome['status'] = 'verified'
            outcome['reason'] = 'Explicit fan parts, independent whole text and credited roster, exact release album/date, and runtime replay verified.'
        else:
            outcome['status'] = 'held'
            if reasons:
                outcome['reason'] = '; '.join(dict.fromkeys(reasons))
        outcome['classification'] = ('verified' if matched else 'transport_limited' if any(
            token in outcome['reason'] for token in ('429', 'transport_limited', 'TimeoutError', 'timed out'))
            else 'text_mismatch' if 'whole_text_mismatch' in outcome['reason'] else 'metadata_or_source_unconfirmed')
    value['final_gate_rejections'] = rejected
    value['final_gate_complete'] = True
    if INPUT.is_file() and path == OUTPUT:
        original = json.loads(INPUT.read_text(encoding='utf-8'))['recordings']
        original_titles = {normalized(profile['title']) for profile in original}
        if original_titles != set(outcomes):
            raise ValueError('candidate title coverage is incomplete')
        for outcome in value['outcomes']:
            reviews = []
            for candidate in original:
                if normalized(candidate['title']) != normalized(outcome['title']):
                    continue
                matches = [profile for profile in kept if
                    normalized(profile['title']) == normalized(candidate['title'])
                    and normalized(profile['album']) == normalized(candidate['album'])
                    and {normalized(a) for a in profile['artists']} == {normalized(a) for a in candidate['artists']}]
                reasons = [record['reason'] for record in rejected if
                    normalized(record['title']) == normalized(candidate['title'])
                    and normalized(record['album']) == normalized(candidate['album'])]
                reviews.append({'title': candidate['title'], 'album': candidate['album'],
                    'artists': candidate['artists'], 'candidate_text_sha256': candidate['text_sha256'],
                    'source_urls': list(dict.fromkeys(clean_url(source['url']) for source in candidate['sources'])),
                    'status': 'verified' if matches else 'held',
                    'reason': '' if matches else '; '.join(dict.fromkeys(reasons)) or (
                        outcome['reason'] if outcome['status'] == 'held' else 'original_candidate_metadata_or_text_unconfirmed'),
                    'verified_text_sha256': [profile['text_sha256'] for profile in matches]})
            outcome['candidate_reviews'] = reviews
        value['input_profile_count'] = len(original)
        value['completed_profile_count'] = sum(len(row['candidate_reviews']) for row in value['outcomes'])
    atomic_json(path, value)
    return value


def recover_original_albums(path=OUTPUT):
    """Narrowly revisit held reissues whose recording names its original release.

    No search/index request: revisit only previously matched source documents,
    reconstruct strict offsets in memory and preserve the rejected reissue.
    """
    value = json.loads(path.read_text(encoding='utf-8'))
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, groups = person_maps(metadata)
    songs = catalog_index(SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs)
    client, added, checks = PublicClient(), [], []
    for rejected in value.get('final_gate_rejections', []):
        if rejected['reason'] != 'release_identity_unconfirmed':
            continue
        outcome = next(row for row in value['outcomes'] if row['title'] == rejected['title'])
        for evidence in outcome['evidence']:
            if not fan_heading_matches(outcome['title'], evidence['fan_heading']):
                continue
            for independent in evidence['independent_checks']:
                if independent['text_sha256'] != rejected['text_sha256'] or not independent['whole_text_alignment']:
                    continue
                original_records = list(independent['recordings'])
                for record in original_records:
                    if normalized(record['album']) != normalized(rejected['album']):
                        continue
                    base = {'title': outcome['title'], 'artists': record['artists'], 'album': record['album']}
                    identities, reason = catalog_identity(base, songs, evidence['legend_people'], lookup, groups)
                    albums = sorted({release['title'] for song in identities for release in song.get('releases', [])
                        if release.get('date') == independent['release_date'] and release.get('date')
                        and normalized(release['title']) != normalized(record['album'])})
                    # Several differently named releases on one date need a
                    # separate manual decision; do not emit aliases as albums.
                    if reason or len(albums) != 1:
                        continue
                    candidate = {**base, 'album': albums[0]}
                    marker = (candidate['title'], candidate['album'], independent['text_sha256'])
                    if any((p['title'], p['album'], p['text_sha256']) == marker
                           for p in [*value['profiles']['recordings'], *added]):
                        continue
                    review = {'title': base['title'], 'held_album': base['album'], 'original_album': albums[0],
                        'part_source_url': evidence['part_source_url'], 'independent_url': independent['url'],
                        'status': 'held'}
                    checks.append(review)
                    try:
                        html = client.get(evidence['part_source_url'])
                        source, attribution, people = parse_page(html, lookup)
                        heading = source_metadata(html)
                        text = parse_independent(client.get(independent['url']))
                        if (digest(source) != evidence['source_text_sha256'] or set(people) != set(evidence['legend_people'])
                                or not fan_heading_matches(base['title'], heading)
                                or digest(text['text']) != independent['text_sha256']
                                or text['release_date'] != independent['release_date']
                                or not title_matches(base['title'], text['title'], heading)
                                or not credit_matches(text['artist'], people, lookup, groups)):
                            raise ValueError('source_changed_or_metadata_unconfirmed')
                        aligned, _ = aligned_values(source, text['text'], attribution,
                            [line.original for line in text['lines']])
                        identities, reason = catalog_identity(candidate, songs, people, lookup, groups)
                        if aligned is None or reason or text['release_date'] not in release_dates(identities, albums[0]):
                            raise ValueError('original_album_or_text_unconfirmed')
                        profile = make_profile(candidate, text['text'], aligned, evidence['part_source_url'], independent['url'])
                        runtime = replay(profile, text['lines'])
                        if not runtime['known_singer_rows']:
                            raise ValueError('runtime_no_parts')
                        independent['recordings'].append({**record, 'album': albums[0],
                            'catalog_ids': [song['id'] for song in identities],
                            'catalog_source_urls': [song.get('source_url', '') for song in identities],
                            'catalog_release_dates': release_dates(identities, albums[0]),
                            'release_anchor_album': albums[0], 'lyric_service_release_matches_catalog': True,
                            'runtime_replay': runtime, 'original_album_from_same_recording_release': True})
                        added.append(profile)
                        review['status'] = 'verified'
                        review['text_sha256'] = profile['text_sha256']
                    except Exception as error:
                        review['reason'] = type(error).__name__ + ': ' + str(error)
                    print(json.dumps(review, ensure_ascii=False), flush=True)
    value['profiles']['recordings'].extend(added)
    value['original_album_reviews'] = checks
    value['original_album_network_requests'] = client.requests
    atomic_json(path, value)
    return finalize_result(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--retry-transient', action='store_true')
    parser.add_argument('--recover-original-albums', action='store_true')
    parser.add_argument('--finalize-only', action='store_true')
    args = parser.parse_args()
    if args.recover_original_albums or args.finalize_only:
        value = recover_original_albums() if args.recover_original_albums else finalize_result()
        print(json.dumps({'completed': len(value['outcomes']), 'verified_titles': sum(
            row['status'] == 'verified' for row in value['outcomes']), 'profiles': len(value['profiles']['recordings']),
            'status': value['status']}), flush=True)
        return
    candidates = json.loads(INPUT.read_text(encoding='utf-8'))['recordings']
    grouped = defaultdict(list)
    for profile in candidates:
        grouped[normalized(profile['title'])].append(profile)
    input_titles = [items[0]['title'] for items in grouped.values()]
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, groups = person_maps(metadata)
    songs = catalog_index(SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs)
    client, outcomes, profiles, known = PublicClient(), [], [], {}
    if args.resume and OUTPUT.is_file():
        previous = json.loads(OUTPUT.read_text(encoding='utf-8'))
        outcomes = previous['outcomes']
        profiles = previous['profiles']['recordings']
        if args.retry_transient:
            outcomes = [row for row in outcomes if not (
                row['status'] != 'verified' and any(token in row['reason'] for token in
                    ('429', 'transport_limited', 'timed out', 'TimeoutError', 'URLError')))]
    finished = {normalized(row['title']) for row in outcomes}
    inspected = 0
    for title_key, choices in grouped.items():
        if title_key in finished:
            continue
        if args.limit and inspected >= args.limit:
            break
        inspected += 1
        title = choices[0]['title']
        source_urls = list(dict.fromkeys(clean_url(source['url']) for profile in choices for source in profile['sources']))
        outcome = {'title': title, 'source_urls': source_urls, 'status': 'held', 'reason': '',
                   'candidate_profiles': len(choices), 'evidence': [], 'verified_profiles': []}
        errors, created = [], []
        for source_url in source_urls:
            try:
                html = client.get(source_url)
                source, attribution, people = parse_page(html, lookup)
                heading = source_metadata(html)
                evidence = {'part_source_url': source_url, 'fan_heading': heading,
                    'legend_people': people, 'source_text_sha256': digest(source),
                    'source_text_length': len(source), 'source_part_count': len(parts_of(attribution)),
                    'uncredited_source_characters': sum(not bool(value) for value in attribution),
                    'independent_checks': []}
                outcome['evidence'].append(evidence)
                if not fan_heading_matches(title, heading):
                    errors.append('part_source_title_or_version_mismatch')
                    continue
                matches = independent_candidates(client, title, people, lookup, groups, known, heading,
                                                 choices[0]['artists'])
                if not matches:
                    errors.append('independent_title_or_performer_not_found')
                    continue
                for entry in matches[:4]:
                    independent = parse_independent(client.get(entry['url']))
                    if not title_matches(title, independent['title'], heading) or not credit_matches(independent['artist'], people, lookup, groups):
                        errors.append('independent_recording_metadata_mismatch')
                        continue
                    # Reuse the artist index for other candidate titles instead
                    # of repeating title searches for the same unit.
                    artist_url = independent['artist_url']
                    if re.fullmatch(r'https://www\.uta-net\.com/artist/\d+/', artist_url):
                        try:
                            for item in parse_search(client.get(artist_url)):
                                known.setdefault(normalized(item['title']), []).append(item)
                        except (OSError, ValueError):
                            pass
                    mapped, matched = aligned_values(source, independent['text'], attribution,
                        [line.original for line in independent['lines']])
                    check = {'url': clean_url(entry['url']), 'kind': 'independent_licensed_lyric_service',
                        'title': independent['title'], 'artist': independent['artist'],
                        'release_date': independent['release_date'], 'text_sha256': digest(independent['text']),
                        'text_length': len(independent['text']), 'matched_characters_diagnostic': matched,
                        'whole_text_alignment': mapped is not None, 'recordings': []}
                    evidence['independent_checks'].append(check)
                    if mapped is None:
                        errors.append('independent_whole_text_mismatch')
                        continue
                    for candidate in choices:
                        identities, reason = catalog_identity(candidate, songs, people, lookup, groups)
                        if reason:
                            errors.append(reason)
                            continue
                        dates = release_dates(identities, candidate['album'])
                        if not independent['release_date'] or independent['release_date'] not in dates:
                            errors.append('release_identity_unconfirmed: ' + candidate['album'])
                            continue
                        profile = make_profile(candidate, independent['text'], mapped, source_url, entry['url'])
                        if not profile['parts']:
                            errors.append('no_explicit_part_spans')
                            continue
                        result = replay(profile, independent['lines'])
                        if result['known_singer_rows'] == 0:
                            errors.append('runtime_replay_returned_no_singers')
                            continue
                        record = {'album': candidate['album'], 'artists': candidate['artists'],
                            'catalog_ids': [song['id'] for song in identities],
                            'catalog_source_urls': list(dict.fromkeys(song.get('source_url', '') for song in identities)),
                            'catalog_release_dates': dates, 'lyric_service_release_matches_catalog': independent['release_date'] in dates,
                            'runtime_replay': result, 'text_sha256': profile['text_sha256']}
                        check['recordings'].append(record)
                        created.append(profile)
            except (OSError, ValueError, UnicodeError, KeyError) as error:
                errors.append(type(error).__name__ + ': ' + str(error)[:180])
            # Additional copies of the same fan transcription do not establish
            # a second independent source. Stop after one verified source.
            if created:
                break
        if created:
            outcome['status'] = 'verified'
            outcome['reason'] = 'Explicit fan singer/color facts and full ordered independent lyrics agree; metadata and runtime replay passed.'
            deduplicated, conflicts = deduplicate_profiles(created)
            if conflicts:
                errors.append('conflicting_part_assignments')
            if not deduplicated:
                outcome['status'] = 'held'
                outcome['reason'] = 'conflicting_part_assignments'
            for profile in deduplicated:
                profiles.append(profile)
                outcome['verified_profiles'].append({'title': profile['title'], 'album': profile['album'],
                    'artists': profile['artists'], 'text_sha256': profile['text_sha256'], 'text_length': profile['text_length']})
        else:
            outcome['reason'] = '; '.join(dict.fromkeys(errors)) or 'no_supported_evidence'
        outcome['review_notes'] = list(dict.fromkeys(errors))
        outcomes.append(outcome)
        save_result(outcomes, profiles, input_titles, client)
        print(f"{len(outcomes)}/{len(input_titles)} {title}: {outcome['status']} ({len(outcome['verified_profiles'])})", flush=True)
    result = save_result(outcomes, profiles, input_titles, client)
    if result['status'] == 'complete':
        result = finalize_result()
    print(json.dumps({'completed': result['completed_title_count'], 'verified_titles': sum(row['status'] == 'verified' for row in result['outcomes']),
                      'profiles': len(result['profiles']['recordings']), 'status': result['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
