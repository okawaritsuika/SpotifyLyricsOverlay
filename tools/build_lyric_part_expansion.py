"""Review public part-color facts against the complete local song catalog.

Public lyric text stays in memory. Output contains metadata, hashes and singer
offsets only. Candidate profiles are not installed unless --install is supplied.
An uncached profile uses the source's complete-text fingerprint, so it cannot
apply to a different text or a shortened/game/live/solo recording.
"""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import URLError

from bs4 import BeautifulSoup, NavigableString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lyrics import normalized
from lyric_parts import _recording
from lyric_match import match_lyrics
from song_catalog import SongCatalog

INDEXES = [
    'https://ameblo.jp/bingo69p/entry-12583693889.html',
    'https://ameblo.jp/ummg315/entry-12539341148.html',
    'https://ameblo.jp/c18222/',
]
LABEL = re.compile(r'[（(]([^（）()\n]{1,65})[）)]')
SKIP_ALBUM = re.compile(r'solo|instrumental|karaoke|off.?vocal|live blu|live dvd|1stlive|2ndlive|3rdlive|4thlive|5thlive|6thlive|7thlive|8thlive|9thlive|10thlive|hotchpotch|non.stop mix', re.I)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def color_of(node, body):
    for parent in node.parents:
        if parent is body:
            break
        value = parent.get('color', '')
        match = re.search(r'(?:^|;)\s*color\s*:\s*([^;]+)', parent.get('style', ''))
        if match:
            value = match[1].strip()
        if value:
            rgb = re.fullmatch(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', value)
            return ('#' + ''.join(f'{int(v):02x}' for v in rgb.groups())) if rgb else value.casefold()
    return ''


def person_maps(metadata):
    aliases, identities = defaultdict(set), {}
    for person in metadata['members']:
        name = person['name'].replace(' ', '').replace('　', '')
        identities[person['id']] = name
        for alias in (person['name'], *person.get('aliases', [])):
            key = normalized(alias)
            if len(key) >= 2:
                aliases[key].add(name)
    lookup = {key: next(iter(names)) for key, names in aliases.items() if len(names) == 1}
    groups = defaultdict(set)
    for group in metadata['groups']:
        members = frozenset(identities[p] for p in group.get('members', []) if p in identities)
        if not members:
            continue
        for alias in (group['name'], *group.get('aliases', [])):
            groups[normalized(alias)].add(members)
    return lookup, groups


def parse_page(html, lookup):
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one('.skin-entryBody')
    if body is None:
        raise ValueError('missing article body')
    for node in body.select('script, style, rt, rp'):
        node.decompose()
    def text_runs(node):
        if isinstance(node, NavigableString):
            value = re.sub(r'[\r\n\t]+', ' ', str(node))
            if value.strip():yield value, color_of(node, body)
            return
        if node.name == 'br':
            yield '\n', ''
            return
        block = node.name in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6')
        if block:yield '\n', ''
        for child in node.children:yield from text_runs(child)
        if block:yield '\n', ''

    nodes = list(text_runs(body))
    colors, people, last_legend, seen_chars = defaultdict(set), [], -1, 0
    # A full-name, individually colored prefix is the required legend. Stop
    # as soon as actual prose/lyrics begin after the first legend name.
    examined = 0
    for index, (value, color) in enumerate(nodes):
        text = normalized(value)
        if not text:continue
        examined += 1
        if examined > 40:break
        person = lookup.get(text)
        if person and color:
            colors[color].add(person)
            if person not in people:
                people.append(person)
            last_legend = index
        elif people and text:
            break
        seen_chars += len(text)
        if seen_chars > 220:
            break
    if len(people) < 2:
        raise ValueError('no explicit multi-person color legend')
    cmap = {color: tuple(names) for color, names in colors.items() if len(names) == 1}
    text, char_colors = '', []
    for value, color in nodes[last_legend + 1:]:
        text += value
        char_colors.extend([color] * len(value))

    def resolve_label(value):
        if value.strip() in ('全員', '全体', '全'):
            return tuple(people)
        names = []
        for token in re.split(r'[、・,，/／&＆＋+\s]+', value):
            key = normalized(token)
            matches = [name for name in people if key and
                       (normalized(name) == key or normalized(name).endswith(key))]
            if len(matches) != 1:
                return ()
            names.append(matches[0])
        return tuple(dict.fromkeys(names))

    labels = {}
    for match in LABEL.finditer(text):
        # Only a label at the start of a rendered HTML line may introduce a
        # singer. A name-like word inside ordinary lyrics stays in the text.
        line_start = text.rfind('\n', 0, match.start()) + 1
        if text[line_start:match.start()].strip():continue
        names = resolve_label(match.group(1))
        if names:
            labels[match.start()] = (match.end(), names)
    output, attribution, current, index = [], [], (), 0
    while index < len(text):
        if index in labels:
            index, current = labels[index]
            continue
        value = normalized(text[index])
        colored = cmap.get(char_colors[index], ())
        if char_colors[index]:
            # A separately colored passage ends the preceding explicit label;
            # an unknown color also cannot inherit the preceding singer.
            current = ()
        # Color-only pages do not grant an uncolored passage to the preceding
        # singer. Only an explicit parenthetical label grants that scope.
        singers = colored or (current if labels else ())
        output.append(value)
        attribution.extend([singers] * len(value))
        index += 1
    joined = ''.join(output)
    if len(joined) < 120 or len(joined) > 8000:
        raise ValueError('unsupported source text size')
    return joined, attribution, people


def compatible(artists, people, lookup, groups):
    expected = frozenset(people)
    explicit = {lookup[normalized(artist)] for artist in artists if normalized(artist) in lookup}
    if explicit == expected:
        return True
    return any(expected in groups.get(normalized(artist), ()) for artist in artists)


def parts_of(values):
    parts, start = [], 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[end] == values[start]:
            end += 1
        if values[start]:
            parts.append({'start': start, 'end': end, 'singers': list(values[start])})
        start = end
    return parts


def aligned_values(source, target, values, target_rows=None):
    matcher = SequenceMatcher(None, source, target, autojunk=False)
    matches = matcher.get_matching_blocks()
    matched = sum(block.size for block in matches)
    # Equal words in a substantially different/game text cannot establish the
    # recording. Require the shared whole-document, bounded-edit validator.
    alignment = match_lyrics([source], target_rows or [target])
    if alignment is None:
        return None, matched
    result = [()] * len(target)
    for index, value in enumerate(values):
        start, end = alignment.source_to_target[index:index + 2]
        if end == start + 1 and source[index] == target[start]:
            result[start] = value
    return result, matched


class Client:
    def __init__(self):
        self.last = 0

    def get(self, url):
        time.sleep(max(0, 1 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        request = Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay personal factual-credit review'})
        with urlopen(request, timeout=20) as response:
            content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError('public document size limit')
            return content.decode('utf-8')


def key_of(profile):
    return (normalized(profile['title']), normalized(profile.get('album', '')),
            profile['text_sha256'], tuple(sorted(normalized(a) for a in profile['artists'])))


def installation_candidates(profiles, report):
    """Keep source-only fingerprints out of the runtime bundle until reviewed."""
    approved = {(normalized(item['title']), normalized(item['album']), item['text_sha256'])
                for page in report['pages'] for item in page['profiles']
                if item['mode'] in ('local_display', 'local_direct')}
    return [profile for profile in profiles
            if (normalized(profile['title']), normalized(profile['album']), profile['text_sha256']) in approved]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=0, help='0 reviews every indexed catalog match')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--review-local', action='store_true',
                        help='Recheck only catalog-indexed pages with a local lyric cache, without recrawling indexes')
    args = parser.parse_args()
    songs = SongCatalog(update_path=ROOT / 'data/song_catalog.json').songs
    metadata = json.loads((ROOT / 'resources/group_credits.json').read_text(encoding='utf-8'))
    lookup, groups = person_maps(metadata)
    by_title, caches = defaultdict(list), defaultdict(list)
    for song in songs:
        by_title[normalized(song['title'])].append(song)
    for path in sorted((ROOT / 'data/lyrics_cache').glob('*.json')):
        cache = json.loads(path.read_text(encoding='utf-8'))
        key = cache.get('key', '').split('\0')
        if len(key) == 3:
            caches[normalized(key[0])].append((key, cache))
    client, entries, indexes = Client(), {}, []
    if args.review_local:
        previous = json.loads((ROOT / 'data/diagnostics/lyric-parts-expansion-report.json').read_text(encoding='utf-8'))
        entries = {item['url']: {key: item[key] for key in ('title', 'url', 'index')}
                   for item in previous['pages'] if normalized(item['title']) in caches}
        indexes = previous['indexes']
    for url in (() if args.review_local else INDEXES):
        body = BeautifulSoup(client.get(url), 'html.parser').select_one('.skin-entryBody')
        links = []
        for link in body.select('a[href]') if body else []:
            title = link.get_text(' ', strip=True)
            target = urlsplit(link.get('href', ''))
            if target.hostname != 'ameblo.jp' or not re.fullmatch(r'/[^/]+/entry-\d+\.html', target.path):
                continue
            target_url = urlunsplit(('https', target.netloc, target.path, '', ''))
            if normalized(title) not in by_title:
                continue
            links.append({'title': title, 'url': target_url})
            entries.setdefault(target_url, {'title': title, 'url': target_url, 'index': url})
        indexes.append({'url': url, 'catalog_title_matches': len(links)})
    entries = sorted(entries.values(), key=lambda item: (not bool(caches.get(normalized(item['title']))), item['index'], item['title']))
    if args.limit:
        entries = entries[:args.limit]
    profiles, reports = [], []
    for number, entry in enumerate(entries, 1):
        title, url = entry['title'], entry['url']
        report = {**entry, 'catalog_recordings': len(by_title[normalized(title)]), 'profiles': []}
        try:
            source, values, people = parse_page(client.get(url), lookup)
            matched_songs = [song for song in by_title[normalized(title)]
                             if song.get('kind') == 'song' and song.get('album')
                             and not SKIP_ALBUM.search(song['album'])
                             and compatible(song.get('artists', []), people, lookup, groups)]
            report['singers'] = people
            report['source_sha256'] = digest(source)
            report['source_length'] = len(source)
            report['compatible_recordings'] = len(matched_songs)
            source_note = ('팬 파트표의 개별 인물 색상 범례와 명시 가창자 표기를 확인함. '
                           '색상·이름이 없는 부분은 추정하지 않음. 전체 가사 해시와 앨범·가창자·원제 일치 시에만 적용.')
            def append_profile(profile_title, artists, album, text, assigned, mode, matched=None):
                parts = parts_of(assigned)
                if not parts:
                    return
                profile = {'title': profile_title, 'artists': list(dict.fromkeys(artists)), 'album': album,
                           'text_sha256': digest(text), 'text_length': len(text),
                           'sources': [{'url': url, 'kind': 'fan_part_transcription', 'note': source_note}],
                           'parts': parts}
                if _recording(profile) is None:
                    raise ValueError('invalid generated profile')
                profiles.append(profile)
                report['profiles'].append({'title': profile_title, 'album': album, 'text_sha256': digest(text),
                    'text_length': len(text), 'mode': mode, 'matched_chars': matched,
                    'covered_chars': sum(bool(value) for value in assigned), 'parts': len(parts)})
            for song in matched_songs:
                append_profile(song['title'], song['artists'], song['album'], source, values, 'source_fingerprint_pending_playback')
            for (actual_title, artist, album), cache in caches.get(normalized(title), []):
                if SKIP_ALBUM.search(album):
                    continue
                if not compatible([artist], people, lookup, groups) and not any(
                        normalized(song['album']) == normalized(album) and
                        normalized(artist) in {normalized(a) for a in song['artists']} for song in matched_songs):
                    continue
                seen_hashes = set()
                for field, lines in (('display', cache.get('lines', [])), ('direct', cache.get('direct', {}).get('lines', []))):
                    text = ''.join(normalized(line['original']) for line in lines)
                    if not text or digest(text) in seen_hashes:
                        continue
                    seen_hashes.add(digest(text))
                    assigned, matched = aligned_values(source, text, values,
                        [line['original'] for line in lines if normalized(line['original'])])
                    if assigned is not None:
                        append_profile(actual_title, [artist], album, text, assigned, 'local_' + field, matched)
                    else:
                        report.setdefault('rejected_local_texts', []).append({'sha256': digest(text), 'length': len(text), 'matched': matched})
            if not report['profiles']:
                report['status'] = 'no_verified_recording_identity'
            else:
                report['status'] = 'profiles_created'
        except (URLError, OSError, ValueError, UnicodeError) as error:
            report['status'] = 'held'
            report['reason'] = str(error)
        reports.append(report)
        print(f"{number}/{len(entries)} {title}: {report['status']} {len(report['profiles'])}", flush=True)
    unique = {}
    for profile in profiles:
        key = key_of(profile)
        if key not in unique:
            unique[key] = profile
        elif unique[key]['parts'] == profile['parts']:
            unique[key]['sources'].extend(source for source in profile['sources'] if source not in unique[key]['sources'])
        else:
            # Different source claims remain separate so the runtime rejects
            # conflicts instead of selecting one source silently.
            unique[(key, profile['sources'][0]['url'])] = profile
    candidate = {'schema_version': 1, 'recordings': list(unique.values())}
    suffix = 'reviewed' if args.review_local else 'candidate'
    atomic_json(ROOT / f'data/diagnostics/lyric_parts_expansion_{suffix}.json', candidate)
    series = Counter(series for song in songs for series in song.get('series', []))
    report = {'schema_version': 1, 'catalog_recordings': len(songs),
              'catalog_unique_normalized_titles_including_versions': len(by_title),
              'catalog_series_recordings': dict(series), 'indexes': indexes,
              'reviewed_pages': len(reports), 'profiles': len(unique),
              'unique_titles': len({normalized(p['title']) for p in unique.values()}),
              'pages': reports, 'notes': [
                  'Title counts retain version suffixes; they are not a complete count of musical compositions.',
                  'Source-fingerprint profiles without a local-cache match remain playback-unverified.',
                  'No lyrics, timings, translations, or external HTML are saved.',
                  'A fan transcription can be wrong; source identity and recording checks do not turn it into an official credit.']}
    report_path = ('tests/lyric-parts-expansion-review.json' if args.review_local
                   else 'data/diagnostics/lyric-parts-expansion-report.json')
    atomic_json(ROOT / report_path, report)
    if args.install:
        path = ROOT / 'resources/lyric_parts.json'
        existing = json.loads(path.read_text(encoding='utf-8'))
        known = {key_of(profile) for profile in existing['recordings']}
        existing['recordings'].extend(profile for profile in installation_candidates(unique.values(), report)
                                     if key_of(profile) not in known)
        atomic_json(path, existing)
    print(json.dumps({key: report[key] for key in ('catalog_recordings', 'catalog_unique_normalized_titles_including_versions', 'reviewed_pages', 'profiles', 'unique_titles')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
