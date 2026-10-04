"""Audit every indexed SideM fan part sheet without playback or saving lyrics.

Only metadata, source URLs, fingerprints and singer offsets leave memory. A
source match is not an official endorsement of a fan's listening transcription.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lyrics import Line, normalized
from lyric_parts import LyricParts, _recording
from song_catalog import SongCatalog
from tools.build_lyric_part_expansion import (
    aligned_values, atomic_json, digest, parts_of, person_maps,
)

OFFICIAL_INDEX = 'https://www.lantis.jp/sidem/releaseinfo/index.html'
SKIP = re.compile(r'solo|instrumental|karaoke|off.?vocal|\blive\b|\d+(?:st|nd|rd|th)live|non.stop mix', re.I)
LABEL = re.compile(r'[（(]([^（）()\n]{1,65})[）)]\s*[:：]?')
OFFICIAL_ACTORS = defaultdict(set)


def clean_url(url):
    parsed = urlsplit(url)
    return urlunsplit(('https', parsed.netloc, parsed.path, '', ''))


def resolve_label(label, people):
    value = label.strip().rstrip(':：').strip()
    if value in ('全員', '全体', '全', 'ALL'):
        return tuple(people)
    names = []
    for token in re.split(r'[、・･,，/／&＆＋+\s]+', value):
        key = normalized(token)
        matches = [person for person in people if key and
                   key in normalized(person)]
        if len(matches) != 1:
            return ()
        names.append(matches[0])
    return tuple(dict.fromkeys(names))


def parse_source(html, expected_title, lookup):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.select_one('.skin-entryTitle')
    if title is None or normalized(title.get_text(' ', strip=True)) != normalized(expected_title):
        raise ValueError('article title does not match indexed song')
    body = soup.select_one('.skin-entryBody')
    if body is None:
        raise ValueError('missing article body')
    for node in body.select('script, style, rt, rp'):
        node.decompose()
    # Mark block/BR breaks before flattening: labels may span nested font tags,
    # while real lyric parentheses must remain in the source fingerprint.
    for node in body.select('br'):
        node.replace_with('\n')
    for node in body.select('p,div'):
        node.insert_after('\n')
    text = body.get_text('', strip=False)
    labels = list(LABEL.finditer(text))
    first = None
    people = []
    header = ''
    for match in labels:
        if not (match.group(0).rstrip().endswith((':', '：')) or match.group(1).endswith((':', '：'))):
            continue
        prefix = text[:match.start()]
        if len(prefix) > 1200:
            break
        possible = []
        # Full names in the explicit header establish its roster. A unit name
        # alone never expands to all members, including for an ALL label.
        for key, person in lookup.items():
            if (key == normalized(person) or len(key) >= 4) and key in normalized(prefix):
                possible.append(person)
        possible = list(dict.fromkeys(possible))
        if len(possible) >= 2 and resolve_label(match.group(1), possible):
            people, first, header = possible, match.start(), prefix
            break
    if first is None:
        raise ValueError('no full-name header followed by an explicit singer label')
    people.sort(key=lambda person: min(normalized(header).find(key) for key, found in lookup.items()
                                      if found == person and key in normalized(header)))
    text = text[first:]
    output, assigned, unknown_labels = [], [], []
    cursor, current = 0, ()
    label_count = 0
    for match in LABEL.finditer(text):
        # A label-looking unknown name ends the preceding assignment. Do not
        # let an unknown voice inherit the singer from the previous section.
        explicit = match.group(0).rstrip().endswith((':', '：')) or match.group(1).endswith((':', '：'))
        if not explicit:
            continue
        names = resolve_label(match.group(1), people)
        piece = normalized(text[cursor:match.start()])
        output.append(piece)
        assigned.extend([current] * len(piece))
        if names:
            current = names
            label_count += 1
        else:
            current = ()
            unknown_labels.append(match.group(1))
        cursor = match.end()
    piece = normalized(text[cursor:])
    output.append(piece)
    assigned.extend([current] * len(piece))
    joined = ''.join(output)
    if not 120 <= len(joined) <= 8000 or label_count < 4:
        raise ValueError('unsupported lyric length or insufficient explicit singer labels')
    return joined, assigned, people, {
        'explicit_labels': label_count,
        'unknown_labels': sorted(set(unknown_labels)),
        'header_sha256': digest(normalized(header)),
        'legend_kind': 'explicit_full_name_roster_and_parenthetical_singer_labels',
    }


class Client:
    def __init__(self):
        self.cache = {}
        self.last = defaultdict(float)
        self.lock = threading.Lock()
        self.host_locks = defaultdict(threading.Lock)

    def get(self, url):
        with self.lock:
            if url in self.cache:
                return self.cache[url]
        host = urlsplit(url).hostname
        with self.host_locks[host]:
            interval = 3.0 if 'uta-net' in host else 0.9
            time.sleep(max(0, interval - (time.monotonic() - self.last[host])))
            self.last[host] = time.monotonic()
        request = Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay personal factual-credit review'})
        with urlopen(request, timeout=20) as response:
            data = response.read(3 * 1024 * 1024 + 1)
        if len(data) > 3 * 1024 * 1024:
            raise ValueError('public document size limit')
        result = data.decode('utf-8')
        with self.lock:
            self.cache[url] = result
        return result


def album_key(value):
    return normalized(value).split(normalized('THE IDOLM@STER SideM'), 1)[-1].replace('disc', '')


def album_identity(left, right, credited_artists=()):
    """Release numbers must agree; fuzzy discovery never proves an album."""
    first, second = album_key(left), album_key(right)
    if not first or not second or re.findall(r'\d+', first) != re.findall(r'\d+', second):
        return False
    if first == second:
        return True
    suffix = ''.join(normalized(artist) for artist in credited_artists)
    return bool(suffix) and (first == second + suffix or second == first + suffix)


def compatible_roster(artists, people, lookup, groups):
    """Compare a credited collaboration to names already explicit in the sheet."""
    named = set()
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    allowed_actors = {normalized(actor['name']) for member in metadata['members']
                      if normalized(member['name']) in {normalized(person) for person in people}
                      for actor in member.get('voice_actors', [])}
    for artist in artists:
        key = normalized(artist)
        if key in lookup:
            named.add(lookup[key])
            continue
        possibilities = groups.get(key, ())
        if len(possibilities) == 1:
            named.update(next(iter(possibilities)))
        elif key not in allowed_actors:
            return False
    return named == set(people)


def album_release_anchor(song, album, date):
    return bool(date) and any(normalized(release['title']) == normalized(album) and release.get('date') == date
                              for release in song.get('releases', []))


def official_candidates(client, albums, album_artists=None):
    soup = BeautifulSoup(client.get(OFFICIAL_INDEX), 'html.parser')
    candidates = {}
    for link in soup.select('a[href*="release_"]'):
        image = link.find('img')
        name = image.get('alt', '') if image else ''
        box = link.find_parent(class_='dsc_box')
        group = box.find('h4').get_text(' ', strip=True).lstrip('■') if box and box.find('h4') else ''
        if SKIP.search(name + ' ' + group):
            continue
        if group and normalized(group) not in normalized(name):
            name = group + ' ' + name
        key = album_key(name)
        if len(key) < 6:
            continue
        matching = [album for album in albums if
                    key.startswith(album_key(album)) or album_key(album).startswith(key) or
                    SequenceMatcher(None, key, album_key(album)).ratio() >= 0.94]
        if matching:
            url = clean_url(urljoin(OFFICIAL_INDEX, link['href']))
            candidates[url] = {'indexed_album': name, 'catalog_albums': matching,
                               'album_artists': {album: (album_artists or {}).get(album, []) for album in matching}}
    return candidates


def official_page(client, url, info):
    try:
        soup = BeautifulSoup(client.get(url), 'html.parser')
        section = soup.find('section')
        heading = section.find('h2') if section else None
        if heading is None:
            raise ValueError('missing official release heading')
        album = heading.get_text(' ', strip=True)
        key = album_key(album)
        matching = [value for value in info['catalog_albums'] if album_identity(album, value, info['album_artists'].get(value, []))]
        track_heading = next((h for h in section.find_all('h3') if 'Index' in h.get_text()), None)
        chunks = []
        if track_heading:
            for sibling in track_heading.next_siblings:
                if getattr(sibling, 'name', None) == 'h3':
                    break
                if hasattr(sibling, 'get_text'):
                    chunks.append(sibling.get_text('\n', strip=True))
        track_text = '\n'.join(chunks)
        tracks = []
        for part in re.split(r'(?:^|\n)\s*\d{1,2}\s*[．.、:：]?\s*', track_text)[1:]:
            rows = [row.strip() for row in part.splitlines() if row.strip()]
            if not rows:
                continue
            title_row = re.split(r'\s*(?:作詞|作曲|編曲|歌[：:])', rows[0])[0]
            credit = re.search(r'歌[：:]\s*([^\n]+)', part)
            tracks.append({'title': title_row, 'credit': credit.group(1).strip() if credit else ''})
        # Unit releases occasionally omit per-track vocals; permit only a
        # single explicitly credited header unit, verified below per song.
        header_p = section.select_one('.release_contents > p')
        return {**info, 'catalog_albums': matching, 'url': url, 'album': album,
                'tracks': tracks,
                'header_credit': re.split(r'品\s*番|価\s*格|発売|LAC', header_p.get_text(' ', strip=True))[0] if header_p else ''}
    except Exception as error:
        return {**info, 'url': url, 'error': type(error).__name__ + ': ' + str(error)}


def credit_matches(credit, artists, people):
    if 'solo' in normalized(credit) or 'ソロ' in credit:
        return False
    lookup, groups = credit_maps()
    def strip_known_cast(match):
        prefix = normalized(credit[:match.start()])
        matches = {person for alias, person in lookup.items() if prefix.endswith(alias)}
        actor = re.sub(r'^\s*CV[.：:\s]*', '', match.group(1), flags=re.I)
        if len(matches) == 1 and normalized(actor) in OFFICIAL_ACTORS[next(iter(matches))]:
            return ''
        return match.group(0)
    credit = re.sub(r'[（(]([^（）()]+)[）)]', strip_known_cast, credit)
    cleaned = re.sub(r'^\s*(?:歌|アーティスト)\s*[：:]\s*', '', credit)
    names = set()
    for token in re.split(r'[、,，&＆/／・\[\]［］()（）]+', cleaned):
        key = normalized(token)
        if not key:
            continue
        if key in lookup:
            names.add(lookup[key])
        elif len(groups.get(key, ())) == 1:
            names.update(next(iter(groups[key])))
        else:
            return False
    return names == set(people)


@lru_cache(maxsize=1)
def credit_maps():
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    for member in metadata['members']:
        person = member['name'].replace(' ', '').replace('　', '')
        OFFICIAL_ACTORS[person].update(normalized(actor['name']) for actor in member.get('voice_actors', []))
    return person_maps(metadata)


def album_credit_index(catalog):
    result = defaultdict(list)
    for song in catalog:
        for release in song.get('releases', [{'title': song.get('album', '')}]):
            for artist in song.get('artists', []):
                if artist not in result[release['title']]:
                    result[release['title']].append(artist)
    return result


def official_profile_pages(profile, people, pages, album_artists):
    return [page for page in pages if 'error' not in page and
            album_identity(page['album'], profile['album'], album_artists.get(profile['album'], profile['artists']))
            and official_track(page, profile['title'], profile['artists'], people)]


def recover_official_profile_metadata():
    """Read only previously recorded official URLs missing from the checkpoint."""
    report = json.loads((ROOT / 'tests/sidem-candidates-validation.json').read_text(encoding='utf-8'))
    pack = json.loads((ROOT / 'data/diagnostics/validated-sidem-parts.json').read_text(encoding='utf-8'))
    path = ROOT / 'tests/sidem-official-validation-metadata.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    pages = {page['url']: page for page in saved['pages']}
    outcomes = {normalized(item['title']): item for item in report['outcomes']}
    catalog = SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs
    album_artists = album_credit_index(catalog)
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, _ = credit_maps()
    register_official_cast(pages.values(), metadata, lookup)
    requests = defaultdict(set)
    for profile in pack['recordings']:
        outcome = outcomes[normalized(profile['title'])]
        if not official_profile_pages(profile, outcome['evidence']['singers'], pages.values(), album_artists):
            for url in outcome['evidence'].get('official_urls', []):
                requests[clean_url(url)].add(profile['album'])
    client = Client()
    for url, albums in requests.items():
        parsed = urlsplit(url)
        if parsed.hostname != 'www.lantis.jp' or not parsed.path.startswith('/sidem/releaseinfo/release_'):
            raise ValueError('unrecognized official evidence URL')
        page = official_page(client, url, {'catalog_albums': sorted(albums),
                              'album_artists': {album: album_artists[album] for album in albums}})
        if 'error' in page:
            raise ValueError(page['error'])
        pages[url] = page
    register_official_cast(pages.values(), metadata, lookup)
    target_albums = {profile['album'] for profile in pack['recordings']}
    for page in pages.values():
        matching = [album for album in target_albums if album_identity(page['album'], album, album_artists[album])]
        page['catalog_albums'] = sorted(set(page.get('catalog_albums', [])) | set(matching))
        page.setdefault('album_artists', {}).update({album: album_artists[album] for album in matching})
    saved['pages'] = list(pages.values())
    atomic_json(path, saved)
    missing = [profile['title'] for profile in pack['recordings'] if not official_profile_pages(
        profile, outcomes[normalized(profile['title'])]['evidence']['singers'], pages.values(), album_artists)]
    print(json.dumps({'additional_official_requests': len(requests), 'still_missing': missing}, ensure_ascii=False))


def register_official_cast(releases, metadata, lookup):
    for member in metadata['members']:
        person = member['name'].replace(' ', '').replace('　', '')
        OFFICIAL_ACTORS[person].update(normalized(actor['name']) for actor in member.get('voice_actors', []))
    for page in releases:
        for credit in [page.get('header_credit', ''), *(track.get('credit', '') for track in page.get('tracks', []))]:
            for match in re.finditer(r'[（(]\s*CV[.：:\s]*([^（）()]+)[）)]', credit, re.I):
                prefix = normalized(credit[:match.start()])
                people = {person for alias, person in lookup.items() if prefix.endswith(alias)}
                if len(people) == 1:
                    OFFICIAL_ACTORS[next(iter(people))].add(normalized(match.group(1)))


def official_track(page, title, artists, people):
    matching = [track for track in page.get('tracks', []) if normalized(track['title']) == normalized(title)]
    for track in matching:
        if track['credit'] and credit_matches(track['credit'], artists, people):
            return True
        if not track['credit']:
            header = page.get('header_credit', '')
            # Do not borrow one unit's credit from a collaboration album.
            if all(normalized(person) in normalized(header) for person in people) and credit_matches(header, artists, people):
                return True
    return False


def independent_texts(client, title, artists, people, known_urls=()):
    urls = list(known_urls)
    if not urls:
        url = 'https://www.uta-net.com/search/?' + urlencode({'target': 'title', 'type': 'in', 'Keyword': title})
        soup = BeautifulSoup(client.get(url), 'html.parser')
        for row in soup.select('.songlist-table tr'):
            heading = row.select_one('.songlist-title')
            cells = row.find_all('td')
            link = heading.find_parent('a') if heading else None
            if not heading or normalized(heading.get_text(' ', strip=True)) != normalized(title) or not link or len(cells) < 2:
                continue
            if credit_matches(cells[1].get_text(' ', strip=True), artists, people):
                urls.append(clean_url(urljoin(url, link['href'])))
    result = []
    for song_url in dict.fromkeys(urls):
        page = BeautifulSoup(client.get(song_url), 'html.parser')
        area, heading = page.select_one('#kashi_area'), page.select_one('h2.kashi-title')
        credit = page.select_one('h3[itemprop="recordedAs"]')
        if area is None or heading is None or normalized(heading.get_text(' ', strip=True)) != normalized(title):
            continue
        artist = credit.get_text(' ', strip=True) if credit else ''
        if not credit_matches(artist, artists, people):
            continue
        for node in area.select('rt,rp,script,style'):
            node.decompose()
        for node in area.select('br'):
            node.replace_with('\n')
        text = area.get_text('', strip=False)
        date = re.search(r'発売日[：:]\s*(\d{4})/(\d{2})/(\d{2})', page.get_text(' ', strip=True))
        result.append((song_url, text, artist, '-'.join(date.groups()) if date else None))
    return result


def verify_runtime(profile, text, values):
    resolver = LyricParts(resource_path=ROOT / 'tests/no-such-part-source.json')
    resolver.recordings = [_recording(profile)]
    rows, expected, cursor = [], [], 0
    while cursor < len(text):
        end = cursor + 1
        while end < len(text) and values[end] == values[cursor]:
            end += 1
        rows.append(Line(text[cursor:end], '', ''))
        expected.append(tuple(values[cursor]))
        cursor = end
    playback = SimpleNamespace(title=profile['title'], artist=profile['artists'][0], album=profile['album'])
    if resolver.match(playback, rows) != tuple(expected):
        raise ValueError('runtime part projection mismatch')
    for changed in ('title', 'artist', 'album'):
        invalid = SimpleNamespace(**vars(playback))
        setattr(invalid, changed, getattr(invalid, changed) + ' DIFFERENT RECORDING')
        if any(resolver.match(invalid, rows)):
            raise ValueError('runtime recording identity guard failed')
    if any(resolver.match(playback, rows[:-1])):
        raise ValueError('runtime shortened text guard failed')
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--resume', action='store_true', help='Reuse prior independent fingerprints and known public document URLs')
    parser.add_argument('--resume-held', action='store_true', help='Keep approved rows and retry only unresolved evidence')
    args = parser.parse_args()
    entries = json.loads((ROOT / 'data/diagnostics/lyric-parts-sidem-source-index.json').read_text(encoding='utf-8'))['entries']
    if args.limit:
        entries = entries[:args.limit]
    original_entries = list(entries)
    previous, previous_profiles, first_pass, retained_keys = {}, [], {}, []
    retained_outcomes, retained_profiles = [], []
    if args.resume or args.resume_held:
        old = json.loads((ROOT / 'tests/sidem-candidates-validation.json').read_text(encoding='utf-8'))
        previous = {normalized(item['title']): item for item in old['outcomes']}
        previous_profiles = json.loads((ROOT / 'data/diagnostics/validated-sidem-parts.json').read_text(encoding='utf-8'))['recordings']
        baseline_report = ROOT / 'tests/sidem-first-pass-validation.json'
        baseline_pack = ROOT / 'tests/sidem-first-pass-candidates.json'
        if baseline_report.exists() and baseline_pack.exists():
            first_pass = {normalized(item['title']): item for item in json.loads(baseline_report.read_text(encoding='utf-8'))['outcomes']}
            previous_profiles += json.loads(baseline_pack.read_text(encoding='utf-8'))['recordings']
        if args.resume_held:
            done = {normalized(item['title']) for item in old['outcomes'] if item['status'] == 'independent_text_and_recording_verified'}
            retained_outcomes = [item for item in old['outcomes'] if normalized(item['title']) in done]
            retained_keys = [item for item in old.get('approved_profile_keys', []) if normalized(item['title']) in done]
            retained_profiles = [item for item in previous_profiles if normalized(item['title']) in done and any(
                item['album'] == key['album'] and item['text_sha256'] == key['text_sha256'] and item['artists'] == key['artists'] for key in retained_keys)]
            entries = [entry for entry in entries if normalized(entry['title']) not in done]
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, groups = person_maps(metadata)
    catalog = SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs
    by_title = defaultdict(list)
    titles = {normalized(entry['title']) for entry in entries}
    for song in catalog:
        if normalized(song['title']) in titles and song.get('kind') == 'song' and not SKIP.search(song.get('album', '')):
            by_title[normalized(song['title'])].append(song)
    albums = {release['title'] for songs in by_title.values() for song in songs for release in
              song.get('releases', [{'title': song.get('album', '')}]) if release.get('title') and not SKIP.search(release['title'])}
    album_artists = album_credit_index(catalog)
    client = Client()
    official = official_candidates(client, albums, album_artists)
    print(f'Official candidate releases: {len(official)}', flush=True)
    metadata_path = ROOT / 'tests/sidem-official-validation-metadata.json'
    cached_metadata = {}
    if metadata_path.exists():
        value = json.loads(metadata_path.read_text(encoding='utf-8'))
        if value.get('parser_schema') == 2:
            cached_metadata = {page['url']: page for page in value['pages']}
    def get_release(item):
        url, info = item
        if url in cached_metadata:
            page = cached_metadata[url]
            return {**page, **info, 'catalog_albums': [album for album in info['catalog_albums']
                    if album_identity(page['album'], album, info['album_artists'].get(album, []))]}
        return official_page(client, url, info)
    with ThreadPoolExecutor(max_workers=3) as pool:
        releases = list(pool.map(get_release, official.items()))
    cached_metadata.update({page['url']: page for page in releases if 'error' not in page})
    atomic_json(metadata_path, {'parser_schema': 2, 'pages': list(cached_metadata.values()),
                               'note': 'Official release facts and track credits only; no lyrics or HTML.'})
    register_official_cast(releases, metadata, lookup)
    print(f'Official pages read: {sum("error" not in page for page in releases)}', flush=True)
    profiles, outcomes, approved_profile_keys = list(retained_profiles), list(retained_outcomes), list(retained_keys)
    for index, entry in enumerate(entries, 1):
        title, url = entry['title'], clean_url(entry['url'])
        outcome = {'title': title, 'source_urls': [url], 'status': 'held', 'reason': '',
                   'evidence': {}, 'verified_profile_count': 0}
        try:
            prior = previous.get(normalized(title), {})
            source_prior = first_pass.get(normalized(title), prior)
            known_urls = list(dict.fromkeys(item['url'] for item in prior.get('evidence', {}).get('independent_comparisons', [])))
            if not known_urls:
                known_urls = list(dict.fromkeys(item['url'] for item in source_prior.get('evidence', {}).get('independent_comparisons', [])))
            uta_texts = None
            reusable = []
            if source_prior.get('status') == 'independent_text_and_recording_verified':
                hashes = {item['text_sha256'] for item in source_prior.get('profiles', []) if item['mode'] == 'independent_full_text'}
                reusable = [p for p in previous_profiles if normalized(p['title']) == normalized(title) and p['text_sha256'] in hashes]
            if reusable:
                people = source_prior['evidence']['singers']
                uta_texts = independent_texts(client, title, reusable[0]['artists'], people, known_urls)
                found = next(((profile, plain) for _, plain, _, _ in uta_texts for profile in reusable
                              if digest(normalized(plain)) == profile['text_sha256']), None)
                if found:
                    profile, plain = found
                    source = normalized(plain)
                    values = [()] * len(source)
                    for part in profile['parts']:
                        values[part['start']:part['end']] = [tuple(part['singers'])] * (part['end'] - part['start'])
                    details = {key: source_prior['evidence'][key] for key in ('explicit_labels', 'unknown_labels', 'header_sha256', 'legend_kind')}
                    details['reused_prior_independent_fingerprint'] = digest(source)
                else:
                    source, values, people, details = parse_source(client.get(url), title, lookup)
            else:
                source, values, people, details = parse_source(client.get(url), title, lookup)
            outcome['evidence'] = {**details, 'singers': people, 'source_sha256': digest(source),
                                   'source_length': len(source)}
            songs = [song for song in by_title[normalized(title)] if compatible_roster(song.get('artists', []), people, lookup, groups)]
            verified = []
            for song in songs:
                song_albums = [release['title'] for release in song.get('releases', [])] + [song.get('album', '')]
                pages = [page for page in releases if 'tracks' in page and official_track(page, title, song['artists'], people) and
                         any(album in page['catalog_albums'] for album in song_albums)]
                if pages:
                    verified.append((song, pages))
            outcome['evidence']['catalog_recordings'] = len(songs)
            outcome['evidence']['official_recordings'] = len(verified)
            outcome['evidence']['official_urls'] = sorted({page['url'] for _, pages in verified for page in pages})
            outcome['source_urls'].extend(outcome['evidence']['official_urls'])
            if not songs:
                outcome['reason'] = 'No matching full studio recording with the explicit source roster in the catalog.'
            elif not verified:
                outcome['reason'] = 'Source title and explicit singers match the catalog; an exact official album/track page was not confirmed.'
            else:
                outcome['status'] = 'source_and_recording_verified'
                outcome['reason'] = 'Fan part labels and explicit roster checked against official track/album identity; exact full-text guard required at runtime.'
                def add_profile(song, album, text, assigned, mode, evidence_url=None, release_anchor=None):
                    parts = parts_of(assigned)
                    if not parts:
                        return
                    sources = [{'url': url, 'kind': 'fan_part_transcription',
                                'note': 'Full-name roster and explicit singer labels. Unmarked or unresolved passages are not inferred. Fan listening transcription; not an official part credit.'}]
                    pages = next(pages for verified_song, pages in verified if verified_song is song)
                    sources.extend({'url': page['url'], 'kind': 'official', 'note': 'Album and song/artist identity only; not a lyric part transcription.'} for page in pages[:5])
                    if evidence_url:
                        sources.append({'url': evidence_url, 'kind': 'independent_lyrics',
                                        'note': 'Independent whole lyric text and recording-credit verification; not a singer-part transcription.'})
                    profile = {'title': title, 'artists': song['artists'], 'album': album,
                               'text_sha256': digest(text), 'text_length': len(text), 'sources': sources, 'parts': parts}
                    if _recording(profile) is None:
                        raise ValueError('invalid profile')
                    runtime_rows = verify_runtime(profile, text, assigned)
                    profiles.append(profile)
                    outcome.setdefault('profiles', []).append({'album': album, 'text_sha256': digest(text),
                        'text_length': len(text), 'parts': len(parts), 'covered_chars': sum(bool(value) for value in assigned),
                        'mode': mode, 'verification_url': evidence_url, 'runtime_rows_checked': runtime_rows,
                        'runtime_identity_and_shortened_text_rejected': True})
                    if mode == 'independent_full_text' and release_anchor:
                        approved_profile_keys.append({'title': title, 'album': album, 'artists': song['artists'],
                            'text_sha256': digest(text), 'release_anchor_verified': True,
                            'exact_official_track_credit_verified': True, 'recording_anchor': release_anchor})
                try:
                    if uta_texts is None:
                        uta_texts = independent_texts(client, title, list(dict.fromkeys(a for song, _ in verified for a in song['artists'])), people, known_urls)
                    outcome['evidence']['uta_net_candidates'] = len(uta_texts)
                except Exception as error:
                    uta_texts = []
                    outcome['evidence']['uta_net_error'] = type(error).__name__ + ': ' + str(error)
                for song, pages in verified:
                    # The fan sheet proves this text, not every re-release or
                    # similarly titled alternate recording in the catalog.
                    official_albums = {album for page in pages for album in page['catalog_albums']}
                    for album in sorted(official_albums & ({song.get('album', '')} | {r['title'] for r in song.get('releases', [])})):
                        if not SKIP.search(album):
                            add_profile(song, album, source, values, 'official_identity_source_fingerprint')
                    for evidence_url, plain, credit, release_date in uta_texts:
                        target = normalized(plain)
                        assigned, matched = aligned_values(source, target, values, [row for row in plain.splitlines() if normalized(row)])
                        matching_albums = {release['title'] for release in song.get('releases', [])
                                           if release_date and release.get('date') == release_date}
                        release_verified = bool(matching_albums)
                        outcome['evidence'].setdefault('independent_comparisons', []).append({
                            'url': evidence_url, 'artist': credit, 'sha256': digest(target),
                            'length': len(target), 'matched_chars': matched, 'accepted': assigned is not None,
                            'release_date': release_date, 'release_anchor_verified': release_verified,
                            'release_anchor_albums': sorted(matching_albums),
                            'catalog_recording_id': song['id']})
                        if assigned is not None and release_verified:
                            outcome['source_urls'].append(evidence_url)
                            outcome['evidence']['uta_net_verified'] = outcome['evidence'].get('uta_net_verified', 0) + 1
                            for album in sorted(official_albums & ({song.get('album', '')} | {r['title'] for r in song.get('releases', [])})):
                                if not SKIP.search(album) and album in matching_albums:
                                    add_profile(song, album, target, assigned, 'independent_full_text', evidence_url,
                                                {'kind': 'published_release_date', 'date': release_date, 'catalog_recording_id': song['id']})
                    try:
                        if any(item['catalog_recording_id'] == song['id'] and item['accepted'] and item['release_anchor_verified']
                               for item in outcome['evidence'].get('independent_comparisons', [])):
                            continue
                        result = json.loads(client.get('https://lrclib.net/api/search?' + urlencode({'track_name': title, 'artist_name': song['artists'][0]})))
                        checked, accepted = 0, 0
                        for item in result if isinstance(result, list) else []:
                            if not isinstance(item, dict) or item.get('instrumental') or normalized(item.get('trackName', '')) != normalized(title):
                                continue
                            if not credit_matches(item.get('artistName', ''), song['artists'], people):
                                continue
                            if item.get('albumName', '') not in official_albums or abs(float(item.get('duration', 0)) - float(song.get('duration', 0))) > 2:
                                continue
                            plain = item.get('plainLyrics') or ''
                            target = normalized(plain)
                            if not target:
                                continue
                            checked += 1
                            assigned, matched = aligned_values(source, target, values, [row for row in plain.splitlines() if normalized(row)])
                            if assigned is not None:
                                outcome['evidence'].setdefault('independent_duration_comparisons', []).append({
                                    'artist': item['artistName'], 'album': item['albumName'],
                                    'duration': float(item['duration']), 'sha256': digest(target),
                                    'catalog_recording_id': song['id'], 'accepted': True,
                                    'url': f'https://lrclib.net/api/get/{item["id"]}'})
                                add_profile(song, item['albumName'], target, assigned, 'independent_full_text', f'https://lrclib.net/api/get/{item["id"]}',
                                            {'kind': 'exact_album_duration', 'duration': float(item['duration']), 'catalog_recording_id': song['id']})
                                accepted += 1
                        outcome['evidence']['independent_text_candidates'] = outcome['evidence'].get('independent_text_candidates', 0) + checked
                        outcome['evidence']['independent_text_verified'] = outcome['evidence'].get('independent_text_verified', 0) + accepted
                    except Exception as error:
                        outcome['evidence']['independent_text_error'] = type(error).__name__
                outcome['source_profile_count'] = len(outcome.get('profiles', []))
                outcome['verified_profile_count'] = sum(item['title'] == title for item in approved_profile_keys)
                if outcome['evidence'].get('independent_text_verified', 0) or outcome['evidence'].get('uta_net_verified', 0):
                    outcome['status'] = 'independent_text_and_recording_verified'
        except Exception as error:
            outcome['reason'] = type(error).__name__ + ': ' + str(error)
        outcomes.append(outcome)
        print(f'{index}/{len(entries)} {title}: {outcome["status"]} ({outcome["verified_profile_count"]})', flush=True)
        # Incremental factual-only output also leaves an explicit outcome for
        # every page processed if a public service fails later in the run.
        atomic_json(ROOT / 'tests/sidem-candidates-validation.json', {'schema_version': 1, 'outcomes': outcomes})
    unique = {}
    for profile in profiles:
        key = (normalized(profile['title']), normalized(profile['album']), profile['text_sha256'], tuple(profile['artists']))
        if key not in unique:
            unique[key] = profile
        elif unique[key]['parts'] != profile['parts']:
            raise ValueError('conflicting same-text part claims')
        else:
            unique[key]['sources'].extend(source for source in profile['sources'] if source not in unique[key]['sources'])
    key_of = lambda p: (normalized(p['title']), normalized(p['album']), p['text_sha256'], tuple(p['artists']))
    approved = {key_of(p): p for p in approved_profile_keys}
    installed = [profile for profile in unique.values() if key_of(profile) in approved]
    for outcome in outcomes:
        outcome['verified_profile_count'] = sum(normalized(item['title']) == normalized(outcome['title']) for item in approved.values())
    order = {normalized(entry['title']): index for index, entry in enumerate(original_entries)}
    outcomes.sort(key=lambda outcome: order[normalized(outcome['title'])])
    report = {'schema_version': 1, 'reviewed': len(outcomes), 'counts': dict(Counter(o['status'] for o in outcomes)),
              'profile_count': len(installed), 'source_only_profile_count': len(unique) - len(installed), 'official_pages': len(releases),
              'approved_profile_keys': list(approved.values()),
              'official_page_errors': [{k: v for k, v in page.items() if k != 'text'} for page in releases if 'error' in page],
              'outcomes': outcomes,
              'notes': ['No listening or Spotify playback was used.', 'Fan part transcriptions are not official voice/part credits.',
                        'Original lyrics, HTML, timings and translations are never written by this audit.',
                        'A source fingerprint activates only on exact full text plus title, artist and album.']}
    atomic_json(ROOT / 'tests/sidem-source-fingerprint-candidates.json', {'schema_version': 1, 'recordings': list(unique.values())})
    atomic_json(ROOT / 'data/diagnostics/validated-sidem-parts.json', {'schema_version': 1, 'recordings': installed})
    atomic_json(ROOT / 'tests/sidem-candidates-validation.json', report)
    print(json.dumps({key: report[key] for key in ('reviewed', 'counts', 'profile_count', 'official_pages')}, ensure_ascii=False))


def finalize_local_report():
    """Apply final per-album gates to factual checkpoints, without more HTTP."""
    report_path = ROOT / 'tests/sidem-candidates-validation.json'
    pack_path = ROOT / 'data/diagnostics/validated-sidem-parts.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    pack = json.loads(pack_path.read_text(encoding='utf-8'))
    catalog = SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs
    songs = {song['id']: song for song in catalog}
    album_artists = album_credit_index(catalog)
    outcomes = {normalized(item['title']): item for item in report['outcomes']}
    lookup, groups = credit_maps()
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    official_path = ROOT / 'tests/sidem-official-validation-metadata.json'
    official_pages = json.loads(official_path.read_text(encoding='utf-8'))['pages'] if official_path.exists() else []
    register_official_cast(official_pages, metadata, lookup)
    key_of = lambda item: (normalized(item['title']), normalized(item['album']), item['text_sha256'],
                           tuple(sorted(normalized(a) for a in item['artists'])))
    keys, official_matches = {}, {}
    for item in report.get('approved_profile_keys', []):
        outcome = outcomes[normalized(item['title'])]
        song = songs.get(item['recording_anchor']['catalog_recording_id'])
        reason = None
        if not song or not compatible_roster(item['artists'], outcome['evidence']['singers'], lookup, groups):
            reason = 'catalog_artist_roster_mismatch'
        elif item['recording_anchor']['kind'] == 'published_release_date':
            date = item['recording_anchor']['date']
            if not album_release_anchor(song, item['album'], date):
                reason = 'independent_release_date_does_not_anchor_this_album'
            elif not any(entry.get('accepted') and entry.get('sha256') == item['text_sha256']
                         and entry.get('release_date') == date and entry.get('catalog_recording_id') == song['id']
                         and credit_matches(entry.get('artist', ''), item['artists'], outcome['evidence']['singers'])
                         for entry in outcome['evidence'].get('independent_comparisons', [])):
                reason = 'independent_credit_roster_or_cast_not_verified'
        elif item['recording_anchor']['kind'] != 'exact_album_duration':
            reason = 'missing_exact_recording_anchor'
        elif not any(entry.get('accepted') and entry.get('sha256') == item['text_sha256']
                     and normalized(entry.get('album', '')) == normalized(item['album'])
                     and entry.get('catalog_recording_id') == song['id']
                     and abs(entry.get('duration', 0) - song.get('duration', 0)) <= 2
                     and credit_matches(entry.get('artist', ''), item['artists'], outcome['evidence']['singers'])
                     for entry in outcome['evidence'].get('independent_duration_comparisons', [])):
            reason = 'independent_exact_album_duration_credit_not_verified'
        matching_pages = official_profile_pages(item, outcome['evidence']['singers'], official_pages, album_artists)
        if not reason and not matching_pages:
            reason = 'target_album_official_track_credit_not_verified'
        if reason:
            outcome['evidence'].setdefault('rejected_album_anchors', []).append({'album': item['album'], 'reason': reason})
        else:
            keys[key_of(item)] = item
            official_matches[key_of(item)] = matching_pages
    verified = []
    for profile in pack['recordings']:
        key = key_of(profile)
        if key not in keys:
            continue
        outcome = outcomes[normalized(profile['title'])]
        official_urls = sorted({page['url'] for page in official_matches[key]})
        profile['sources'] = [source for source in profile['sources'] if source['kind'] != 'official']
        profile['sources'].extend({'url': url, 'kind': 'official',
                                  'note': 'This exact album, track row and complete performer credit; not a lyric part transcription.'}
                                 for url in official_urls)
        keys[key]['official_urls'] = official_urls
        outcome['evidence'].setdefault('final_official_album_sources', [])
        evidence = {'album': profile['album'], 'urls': official_urls}
        if evidence not in outcome['evidence']['final_official_album_sources']:
            outcome['evidence']['final_official_album_sources'].append(evidence)
        urls = {entry['verification_url'] for entry in outcome.get('profiles', [])
                if entry.get('mode') == 'independent_full_text' and entry.get('verification_url')
                and normalized(entry['album']) == normalized(profile['album'])
                and entry['text_sha256'] == profile['text_sha256']}
        for url in sorted(urls):
            if not any(source.get('url') == url and source.get('kind') == 'independent_lyrics' for source in profile['sources']):
                profile['sources'].append({'url': url, 'kind': 'independent_lyrics',
                                          'note': 'Independent whole lyric text and recording-credit verification; not a singer-part transcription.'})
        if not any(source.get('kind') == 'independent_lyrics' for source in profile['sources']):
            raise ValueError('approved fingerprint lacks its independent evidence URL')
        if _recording(profile) is None:
            raise ValueError('final profile invalid')
        verified.append(profile)
    valid_keys = {key_of(profile) for profile in verified}
    report['approved_profile_keys'] = [item for key, item in keys.items() if key in valid_keys]
    for outcome in report['outcomes']:
        count = sum(normalized(profile['title']) == normalized(outcome['title']) for profile in verified)
        outcome['verified_profile_count'] = count
        if count:
            outcome['status'] = 'independent_text_and_recording_verified'
            outcome['reason'] = 'Exact official track/album and complete singer roster; independent whole text; this album release date or exact album/duration; runtime match and negative guards passed.'
        elif outcome.get('evidence', {}).get('official_recordings'):
            outcome['status'] = 'source_and_recording_verified'
            comparisons = outcome['evidence'].get('independent_comparisons', [])
            if outcome['evidence'].get('rejected_album_anchors'):
                failures = {item['reason'] for item in outcome['evidence']['rejected_album_anchors']}
                outcome['reason'] = ('Held: independent lyric release date does not anchor the specific candidate album.'
                                     if failures == {'independent_release_date_does_not_anchor_this_album'} else
                                     'Held: the independent credit, cast, or exact recording evidence did not pass the final gate.')
            elif comparisons and all(not entry.get('accepted') for entry in comparisons):
                outcome['reason'] = 'Held: independent complete lyric text differs beyond the bounded alignment rules; no lyric or singer data was guessed.'
            elif not outcome['evidence'].get('uta_net_candidates'):
                outcome['reason'] = 'Held: no independent lyric document with the same complete singer roster was verified.'
            else:
                outcome['reason'] = 'Held: independent text/recording evidence did not satisfy every per-album approval gate.'
        outcome['source_urls'] = sorted(set(clean_url(url) for url in outcome['source_urls']))
    report['profile_count'] = len(verified)
    report['source_only_profile_count'] += len(pack['recordings']) - len(verified)
    report['counts'] = dict(Counter(item['status'] for item in report['outcomes']))
    report['per_album_anchor_finalized'] = True
    report['official_profile_sources_finalized'] = True
    note = 'The final gate reuses factual checkpoints only. An original album date never approves a different reissue album.'
    if note not in report['notes']:
        report['notes'].append(note)
    atomic_json(pack_path, {'schema_version': 1, 'recordings': verified})
    atomic_json(report_path, report)
    print(json.dumps({'reviewed': len(report['outcomes']), 'counts': report['counts'], 'profile_count': len(verified)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
    finalize_local_report()
