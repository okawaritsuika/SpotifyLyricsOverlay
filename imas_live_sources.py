"""Collect attributed performance facts from the public アイマスDB event index.

This is an unofficial fan database, not proof of a particular studio recording.
Only URLs actually linked from its event index are visited. HTML is cached for
resumable, slow maintenance; prose, images, and lyrics are not exported.
"""
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener

from bs4 import BeautifulSoup


INDEX_URL = 'https://imas-db.jp/song/event/'
POLICY_URL = 'https://info.imas-db.jp/about/'
SOURCE_NAME = 'アイマスDB (비공식 팬 데이터베이스)'
MAX_HTML_BYTES = 4 * 1024 * 1024
DATE_CORRECTIONS_PATH = Path(__file__).resolve().parent / 'resources' / 'live_source_corrections.json'
BRANDS = {
    '1': ('765', '765PRO ALLSTARS'), '3': ('other', 'ディアリースターズ'),
    '4': ('cinderella', 'シンデレラガールズ'), '5': ('million', 'ミリオンライブ！'),
    '6': ('sidem', 'SideM'), '8': ('shiny', 'シャイニーカラーズ'),
    '9': ('other', '961プロダクション'), '10': ('other', 'vα-liv'),
    '11': ('gakuen', '学園アイドルマスター'),
}
BADGE_BRANDS = {'765as': '1', 'dearlystars': '3', 'cinderella': '4',
                'million': '5', 'sidem': '6', 'shinycolors': '8',
                '961pro': '9', 'valiv': '10', 'gakuen': '11'}


class Cancelled(Exception):
    pass


class ParseError(ValueError):
    pass


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def _text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def _event_url(value, base=INDEX_URL):
    value = urlsplit(urljoin(base, value))
    if (value.scheme != 'https' or value.netloc != 'imas-db.jp' or value.query
            or not re.fullmatch(r'/song/event/[\w-]+\.html', value.path)):
        return None
    return urlunsplit((value.scheme, value.netloc, value.path, '', ''))


def _brand_ids(node, descendants=True):
    values = []
    for item in [node] + (list(node.select('[data-brand-id], [data-brand-ids], .badge'))
                          if descendants else []):
        for attr in ('data-brand-id', 'data-brand-ids'):
            values.extend(re.findall(r'\d+', item.get(attr, '')))
        for cls in item.get('class', []):
            if cls.startswith('bg-imas-brand-'):
                value = BADGE_BRANDS.get(cls.removeprefix('bg-imas-brand-'))
                if value:
                    values.append(value)
    return list(dict.fromkeys(values))


def _brand_fields(ids):
    return {'brand_ids': list(ids),
            'series': list(dict.fromkeys(BRANDS.get(key, ('other', key))[0] for key in ids)) or ['other'],
            'series_names': [BRANDS.get(key, ('other', 'brand ' + key))[1] for key in ids]}


def parse_index(html):
    """Return unique page entries, retaining duplicate DAY links as evidence."""
    soup = BeautifulSoup(html, 'html.parser')
    entries = {}
    for anchor in soup.select('.section li a[href]'):
        url = _event_url(anchor['href'])
        if not url:
            continue
        li = anchor.find_parent('li')
        ancestor = li
        ids, dates = [], []
        while ancestor:
            if not ids:
                ids = _brand_ids(ancestor, descendants=False)
            dates.extend(_text(item) for item in ancestor.select('small.date')
                         if item.find_parent('li') is ancestor)
            ancestor = ancestor.find_parent('li')
        entry = entries.setdefault(url, {'url': url, 'index_entries': [], 'brand_ids': []})
        entry['brand_ids'] = list(dict.fromkeys(entry['brand_ids'] + ids))
        entry['index_entries'].append({'title': anchor.get('title') or _text(anchor),
                                       'date_text': ' '.join(dates), 'brand_ids': ids})
    if not entries:
        raise ParseError('event_index_empty_or_changed')
    return list(entries.values())


class _SameSiteRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl != INDEX_URL and not _event_url(newurl):
            raise ValueError('unexpected_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CachedClient:
    """Bounded same-site GET; cache misses/refreshes are at least 1 second apart."""
    def __init__(self, cache_dir, cancel=None, delay=1.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cancel = cancel
        self.delay = max(1.0, float(delay))
        self.last_request = 0.0
        self.opener = build_opener(_SameSiteRedirect())
        self.network_count = 0
        self.cache_count = 0

    def get(self, url, refresh=False):
        if _cancelled(self.cancel):
            raise Cancelled()
        if url != INDEX_URL and not _event_url(url):
            raise ValueError('unexpected_source_url')
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        path = self.cache_dir / (key + '.html')
        if not refresh and path.exists() and path.stat().st_size <= MAX_HTML_BYTES:
            self.cache_count += 1
            return path.read_text(encoding='utf-8')
        while time.monotonic() - self.last_request < self.delay:
            if _cancelled(self.cancel):
                raise Cancelled()
            time.sleep(.1)
        self.last_request = time.monotonic()
        request = Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay-live-catalog/1.0 (personal facts catalog)',
                                       'Accept': 'text/html'})
        with self.opener.open(request, timeout=25) as response:
            if response.url != url:
                raise ValueError('source_redirected_inspect_before_import')
            raw = response.read(MAX_HTML_BYTES + 1)
        if len(raw) > MAX_HTML_BYTES:
            raise ValueError('source_too_large')
        html = raw.decode('utf-8')
        if '<html' not in html.lower():
            raise ValueError('not_html')
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(raw)
        tmp.replace(path)
        path.with_suffix('.json').write_text(json.dumps({
            'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
        }), encoding='utf-8')
        self.network_count += 1
        return html


def parse_dates(value):
    """Read explicitly printed full dates and adjacent Japanese date lists.

    A year heading, filename, weekday, or publication date never supplies a
    missing year. Ranges are expanded only within the printed calendar month.
    """
    value = unicodedata.normalize('NFKC', str(value))
    result = []
    for match in re.finditer(r'(?<!\d)(20\d{2})[/年-](\d{1,2})[/月-](\d{1,2})(?:日)?', value):
        y, m, d = map(int, match.groups())
        try:
            current = date(y, m, d)
        except ValueError:
            raise ParseError('invalid_printed_date')
        result.append(current.isoformat())
        tail = value[match.end():]
        while True:
            extra = re.match(r'^\s*(?:\([^)]*\))?\s*([・、,〜～~])\s*'
                             r'(?:(\d{1,2})[/月])?(\d{1,2})(?:日)?(?!\d|:)', tail)
            if not extra:
                break
            month = int(extra[2]) if extra[2] else m
            day = int(extra[3])
            try:
                following = date(y, month, day)
            except ValueError:
                raise ParseError('invalid_printed_date')
            if extra[1] in '〜～~':
                if month != m or day < d or day - d > 30:
                    raise ParseError('ambiguous_date_range')
                result.extend(date(y, m, n).isoformat() for n in range(d + 1, day + 1))
            else:
                result.append(following.isoformat())
            m, d = month, day
            tail = tail[extra.end():]
    return list(dict.fromkeys(result))


def _label(value):
    return re.sub(r'[\s\W_]+', '', unicodedata.normalize('NFKC', value)
                  .replace('セットリスト', '').casefold())


def _date_blocks(soup):
    blocks = []
    for node in soup.select('p, div, li, tr, h2, h3, h4'):
        if node.find_parent(['header', 'footer', 'nav', 'aside']):
            continue
        table = node.find_parent('table')
        if table and 'tracklist' in table.get('class', []):
            continue
        # Read the smallest block; schedule rows keep their label/date together.
        if node.name != 'tr' and node.select('p, div, li, tr, table'):
            continue
        value = _text(node)
        if len(value) > 500 or not re.search(r'20\d{2}[/年-]\d', value):
            continue
        if any(term in value for term in ('発売', '更新', '配信期間', 'アーカイブ', '©')):
            continue
        dates = parse_dates(value)
        if not dates:
            continue
        # Parentheses explicitly identify an alternative timezone, not a DAY2.
        if '日本時間' in value and re.search(r'(?:夏時間|現地時間|標準時)', value):
            dates = parse_dates(value.split('日本時間', 1)[0])[:1]
        heading = node.find_previous(['h2', 'h3', 'h4'])
        label = _text(heading)
        if node.name == 'tr':
            label = _text(node.find(['td', 'th']))
        blocks.append({'dates': dates, 'text': value, 'label': label, 'node': node})
    return blocks


def _table_heading(table):
    section = table.find_parent(class_='section')
    if section:
        headings = [h for h in section.select('h2,h3,h4')
                    if h.find_parent(class_='section') is section]
        if headings:
            return _text(headings[0])
    return _text(table.find_previous(['h2', 'h3', 'h4']))


def _dates_for_table(table, heading, blocks, available):
    """Associate a table only through an explicit date/session/DAY label."""
    explicit = parse_dates(heading)
    if explicit:
        return explicit, 'table_heading'
    partial = re.search(r'(?<!\d)(\d{1,2})[/月](\d{1,2})(?:日)?', heading)
    if partial:
        suffix = f'-{int(partial[1]):02}-{int(partial[2]):02}'
        matches = [d for d in available if d.endswith(suffix)]
        if len(matches) == 1:
            return matches, 'table_heading_and_printed_year'
    name = _label(heading)
    matching = [b for b in blocks if _label(b['label'])
                and (_label(b['label']) == name or _label(b['label']) in name
                     or any(_label(q) in name for q in re.findall(r'「([^」]+)」', b['label'])))
                and 'セットリスト' not in b['label']]
    if matching:
        dates = list(dict.fromkeys(d for b in matching for d in b['dates']))
        return dates, 'named_schedule'
    # A nearby date inside this table's section is scoped evidence.
    section = table.find_parent(class_='section')
    scoped = [b for b in blocks if section and b['node'].find_parent(class_='section') is section]
    if scoped:
        dates = list(dict.fromkeys(d for b in scoped for d in b['dates']))
        if dates:
            return dates, 'section_schedule'
    day = re.search(r'day\s*([1-9]\d*)', heading, re.I)
    if day and 1 <= int(day[1]) <= len(available):
        return [available[int(day[1]) - 1]], 'day_label_and_printed_dates'
    if len(available) == 1:
        return available, 'single_printed_date'
    if '全公演共通' in heading:
        return available, 'explicitly_shared_setlist'
    raise ParseError('ambiguous_table_date: ' + heading[:150])


def _clean_title(cell, remove_notes=False):
    clone = BeautifulSoup(str(cell), 'html.parser')
    selector = '.badge, .visually-hidden, rt, rp, script, style'
    if remove_notes:
        selector += ', .notes, .additional'
    for extra in clone.select(selector):
        extra.decompose()
    return re.sub(r'\s+', ' ', clone.get_text()).strip()


def _expanded_rows(table):
    """Expand table spans without carrying section headers into song cells."""
    active = {}
    for tr in table.select('tr'):
        if tr.find_parent('table') is not table or tr.find_parent('thead'):
            continue
        cells = tr.find_all(['td', 'th'], recursive=False)
        if not cells:
            continue
        if len(cells) == 1 and int(cells[0].get('colspan', 1)) >= 3:
            yield tr, [], _text(cells[0])
            continue
        row = {col: item[0] for col, item in active.items()}
        active = {col: (item[0], item[1] - 1) for col, item in active.items() if item[1] > 1}
        col = 0
        for cell in cells:
            while col in row:
                col += 1
            colspan, rowspan = int(cell.get('colspan', 1)), int(cell.get('rowspan', 1))
            if not 1 <= colspan <= 20 or not 1 <= rowspan <= 300:
                raise ParseError('unreasonable_table_span')
            for offset in range(colspan):
                row[col + offset] = cell
                if rowspan > 1:
                    active[col + offset] = (cell, rowspan - 1)
            col += colspan
        yield tr, [row.get(n) for n in range(max(row, default=-1) + 1)], None


def parse_songs(table, fallback_brands, performance_session=None):
    songs, sections, skipped = [], [], 0
    section, last_title_cell = '', None
    for tr, cells, new_section in _expanded_rows(table):
        if new_section is not None:
            section = new_section
            if section:
                sections.append(section)
            continue
        if len(cells) < 2 or not cells[0] or not cells[1]:
            skipped += 1
            continue
        number = _text(cells[0])
        conditional = re.fullmatch(r'公演\((\d+)\)\s*(\d+)曲目', cells[0].get('title', ''))
        if conditional:
            if performance_session is None:
                raise ParseError('conditional_row_requires_explicit_session')
            if performance_session != conditional[1]:
                continue
            number = conditional[2]
        title_cell = cells[1]
        # Musicals include real songs without numbers; remove stage directions
        # through their explicit .notes markup instead of dropping all blanks.
        if number and not re.fullmatch(r'(?:M|EN|EC|E)?\s*\d{1,3}(?:[.\-〜～]\d{1,3})?[.．]?', number, re.I):
            skipped += 1
            continue
        base = _clean_title(title_cell, remove_notes=True)
        performed = _clean_title(title_cell)
        if not base or (not number and (re.fullmatch(r'[（(].*[）)]', base)
                                       or base.upper() in {'MC', 'VTR', 'SE', 'BGM'})):
            skipped += 1
            continue
        credits = _text(cells[2]) if len(cells) > 2 and cells[2] is not title_cell else ''
        if title_cell is last_title_cell:
            if credits and credits not in songs[-1].get('artists', []):
                songs[-1].setdefault('artists', []).append(credits)
            continue
        last_title_cell = title_cell
        links = [a for a in title_cell.select('a[href]')
                 if re.fullmatch(r'/song/detail/\d+\.html', urlsplit(urljoin(INDEX_URL, a['href'])).path)]
        row_brands = _brand_ids(title_cell)
        ids = row_brands or _brand_ids(tr, descendants=False) or fallback_brands
        # An ordered medley list may mix linked and unlinked song names.
        # Using only anchors would silently discard the unlinked songs.
        items = title_cell.select('ol > li, ul > li')
        components = items or (links if len(links) > 1 else [None])
        number_range = re.fullmatch(r'(\d+)[〜～](\d+)', number)
        if items and number_range and len(items) != int(number_range[2]) - int(number_range[1]) + 1:
            raise ParseError('medley_number_range_does_not_match_components')
        medley_note = ' '.join(_text(n) for n in title_cell.find_all(['span', 'small'], recursive=False)) if items else ''
        for component_index, component in enumerate(components, 1):
            title = _clean_title(component, True) if component else base
            component_brands = _brand_ids(component) if component else []
            row = {'title': title, 'performance_number': number,
                   'recording_version_unverified': True,
                   'series_basis': 'explicit_song_brand' if component_brands or row_brands else 'event_section_brand',
                   **_brand_fields(component_brands or ids)}
            if performed != title:
                row['performance_title'] = performed
            link = (component if component and component.name == 'a'
                    else component.find('a', href=True) if component else links[0] if links else None)
            if link:
                row['song_title'] = _clean_title(link, True)
                row['song_source_url'] = urljoin(INDEX_URL, link['href'])
            if credits:
                # Keep qualifiers (dance/backing vocals/all cast) with the credit.
                row['artists'] = [credits]
                row['artist_basis'] = 'fan_database_performance_credit'
            if section:
                row['performance_section'] = section
            if len(components) > 1:
                row['medley_component'] = component_index
                if medley_note:
                    row['note'] = medley_note
            songs.append(row)
    if not songs:
        raise ParseError('no_numbered_song_rows')
    notes = []
    section_node = table.find_parent(class_='section')
    if section_node:
        for node in section_node.select('p.notes, div.notes'):
            value = _text(node)
            if not node.find_parent('table') and len(value) <= 500 and re.search(r'曲目|歌唱なし|メドレー', value):
                notes.append(value)
                muted = re.search(r'(\d+)\s*[-〜～]\s*(\d+)曲目.*歌唱なし', value)
                if muted:
                    for row in songs:
                        if row['performance_number'].isdigit() and int(muted[1]) <= int(row['performance_number']) <= int(muted[2]):
                            row['performance_kind'] = 'dance_without_vocals'
    return songs, {'section_labels': sections, 'excluded_non_song_rows': skipped,
                   'setlist_notes': notes}


def _date_correction(entry, title, index_dates, page_dates, tables):
    """Apply only individually reviewed official evidence, fail closed on drift."""
    try:
        corrections = json.loads(DATE_CORRECTIONS_PATH.read_text(encoding='utf-8'))['corrections']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    matches = [item for item in corrections if item.get('database_url') == entry['url']]
    if not matches:
        return None
    correction = matches[0]
    labels = [_table_heading(table) for table in tables]
    sessions = correction.get('sessions') or []
    official = urlsplit(correction.get('official_url', ''))
    if (len(matches) != 1 or correction.get('event_title') != title
            or correction.get('observed_index_dates') != index_dates
            or correction.get('observed_page_dates') != page_dates
            or len(set(labels)) != len(labels)
            or {s.get('source_table_label') for s in sessions} != set(labels)
            or official.scheme != 'https' or official.netloc not in {'idolmaster-official.jp', 'avex.jp'}):
        raise ParseError('reviewed_date_correction_evidence_changed')
    for label in labels:
        selected = [s for s in sessions if s['source_table_label'] == label]
        # Reviewed official programme statements can establish shared content
        # even when the fan table omits that label. Bind this exception to the
        # exact programme, occurrence count and unchanged source table.
        evidence = correction.get('shared_setlist_official_evidence')
        evidence = evidence if isinstance(evidence, dict) else {}
        statement = re.fullmatch(r'(.+)全(\d+)公演は同様の内容になります。',
                                 str(evidence.get('statement', '')))
        official_shared = (statement is not None and statement[1] in title
                           and int(statement[2]) == len(selected)
                           and evidence.get('url') == correction['official_url']
                           and evidence.get('source_table_sha256') == hashlib.sha256(
                               str(tables[labels.index(label)]).encode()).hexdigest())
        if len(selected) > 1 and not (correction.get('shared_setlist_explicit') is True
                                     and ('全公演共通' in label or official_shared)):
            raise ParseError('reviewed_date_correction_shared_setlist_not_explicit')
    try:
        for session in sessions:
            if date.fromisoformat(session['date']).isoformat() != session['date'] or not isinstance(session['session'], str):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ParseError('invalid_reviewed_date_correction')
    return correction


def parse_event(html, entry, retrieved_at=None):
    """Normalize one indexed page; any unresolved table rejects that page.

    All-or-nothing per URL lets callers retain the previous complete import
    instead of accidentally deleting an unparsed DAY/session during refresh.
    """
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.select('table.tracklist')
    if not tables:
        raise ParseError('setlist_not_published_or_layout_changed')
    title = _text(soup.select_one('#page_title') or soup.find('h1'))
    if not title:
        raise ParseError('event_title_missing')
    blocks = _date_blocks(soup)
    page_dates = sorted(set(d for block in blocks for d in block['dates']))
    index_dates = sorted(set(d for item in entry['index_entries'] for d in parse_dates(item['date_text'])))
    correction = _date_correction(entry, title, index_dates, page_dates, tables)
    # A tour's index can state just two endpoints; interior dates must still be
    # explicitly printed next to each table, never generated from that range.
    index_ranges = []
    for item in entry['index_entries']:
        if re.search(r'20\d\d/\d{1,2}/\d{1,2}(?:\([^)]*\))?\s*[〜～~]\s*20\d\d/', item['date_text']):
            endpoints = parse_dates(item['date_text'])
            if len(endpoints) == 2:
                index_ranges.append((min(endpoints), max(endpoints)))
    if not correction and page_dates and index_dates and any(d not in index_dates and not any(a <= d <= b for a, b in index_ranges) for d in page_dates):
        raise ParseError('index_page_date_conflict: ' + ','.join(index_dates) + ' / ' + ','.join(page_dates))
    available = sorted({s['date'] for s in correction['sessions']}) if correction else page_dates or index_dates
    if not available:
        raise ParseError('no_explicit_performance_date')
    events = []
    retrieved_at = retrieved_at or datetime.now(timezone.utc).isoformat()
    for table_index, table in enumerate(tables, 1):
        heading = _table_heading(table)
        corrected_sessions = [(s['date'], s['session']) for s in correction['sessions']
                              if s['source_table_label'] == heading] if correction else []
        indexed_sessions = []
        conditional_ids = {match[1] for cell in table.select('td[title]')
                           if (match := re.fullmatch(r'公演\((\d+)\)\s*\d+曲目', cell['title']))}
        numbered_sessions = []
        if conditional_ids:
            for block in blocks:
                label_match = re.match(r'^公演\((\d+)\)\s*:', block['text'])
                if label_match and len(block['dates']) == 1:
                    numbered_sessions.append((block['dates'][0], '公演(' + label_match[1] + ')'))
            if {label[3:-1] for _, label in numbered_sessions} != conditional_ids:
                raise ParseError('conditional_session_schedule_mismatch')
            if not re.search(r'曲目は公演ごとに異なる楽曲', _text(table.parent)):
                raise ParseError('conditional_shared_setlist_not_explicit')
        if len(tables) == 1:
            for item in entry['index_entries']:
                for day_number, period in re.findall(r'DAY\s*([1-9]\d*)\s*(昼|夜)', item['date_text'], re.I):
                    if int(day_number) > len(available):
                        raise ParseError('indexed_session_date_missing')
                    indexed_sessions.append((available[int(day_number) - 1], 'DAY' + day_number + period))
        if corrected_sessions:
            dates, basis = list(dict.fromkeys(d for d, _ in corrected_sessions)), 'reviewed_official_date_correction'
        elif numbered_sessions:
            dates, basis = list(dict.fromkeys(d for d, _ in numbered_sessions)), 'numbered_performance_schedule'
        elif indexed_sessions:
            dates, basis = list(dict.fromkeys(d for d, _ in indexed_sessions)), 'indexed_day_session'
        else:
            dates, basis = _dates_for_table(table, heading, blocks, available)
        if not set(dates).issubset(available):
            raise ParseError('table_date_conflict')
        parent = table.find_parent(attrs={'data-brand-id': True}) or table.find_parent(attrs={'data-brand-ids': True})
        ids = _brand_ids(parent, descendants=False) if parent else entry['brand_ids']
        if ids == ['0']:
            ids = entry['brand_ids'] or ids
        songs, metadata = parse_songs(table, ids) if not numbered_sessions else (None, None)
        session = re.sub(r'\s*セットリスト\s*', '', heading).strip()
        occurrences = corrected_sessions or numbered_sessions or indexed_sessions or [(d, session) for d in dates]
        if '全公演共通' in heading and not corrected_sessions:
            schedule = [(d, b['label']) for b in blocks if re.search(r'DAY\s*\d|(?:昼|夜)公演', b['label'], re.I)
                        for d in b['dates']]
            if schedule and set(d for d, _ in schedule) == set(dates):
                occurrences = list(dict.fromkeys(schedule))
        for stamp, occurrence in occurrences:
            if numbered_sessions:
                songs, metadata = parse_songs(table, ids, performance_session=occurrence[3:-1])
            key = f"{entry['url']}|{table_index}|{stamp}|{occurrence}"
            event = {'id': 'imasdb-' + hashlib.sha256(key.encode()).hexdigest()[:20],
                     'title': title + (' — ' + occurrence if occurrence else ''), 'date': stamp,
                     **_brand_fields(ids), 'source_url': entry['url'],
                     'source_name': SOURCE_NAME, 'source_kind': 'fan_database',
                     'source_policy_url': POLICY_URL, 'setlist_source_url': entry['url'],
                     'date_source_url': entry['url'] if page_dates else INDEX_URL,
                     'date_basis': basis, 'retrieved_at': retrieved_at,
                     'setlist_coverage': 'source_table', 'songs': songs,
                     'recording_version_unverified': True,
                     'source_table_index': table_index,
                     'source_table_label': heading, **metadata}
            event['source_date_text'] = [b['text'] for b in blocks if stamp in b['dates']]
            if correction:
                event['date_source_url'] = correction['official_url']
                event['official_date_source_url'] = correction['official_url']
                event['date_correction'] = {key: correction[key] for key in
                                            ('observed_index_dates', 'observed_page_dates',
                                             'official_source_name', 'reason')}
                if correction.get('shared_setlist_official_evidence'):
                    event['date_correction']['shared_setlist_official_evidence'] = dict(
                        correction['shared_setlist_official_evidence'])
                    event['note'] = correction['reason']
                event['original_page_date_text'] = [b['text'] for b in blocks]
            # Rows may explicitly identify a guest song from another brand.
            event['series'] = list(dict.fromkeys(event['series'] + [s for row in songs for s in row['series']]))
            events.append(event)
    return events


def collect(cache_dir, progress=None, cancel=None, refresh=False, retry_urls=None):
    """Return a schema-1 candidate plus explicit coverage/failure diagnostics.

    progress receives {phase, completed, total, url, events, failures}. Cancel
    can be a callable or threading.Event. No catalog is overwritten here.
    retry_urls restricts retries to URLs still present in the current index.
    Successful URLs are only pages whose every setlist table was parsed.
    """
    client = CachedClient(cache_dir, cancel)
    result = {'schema_version': 1, 'coverage': 'partial',
              'verified_at': date.today().isoformat(), 'events': [], 'failures': [],
              'index_urls': [], 'successful_urls': [], 'skipped_urls': [], 'cancelled': False, 'stats': {}}
    try:
        entries = parse_index(client.get(INDEX_URL, refresh=refresh))
        result['index_urls'] = [item['url'] for item in entries]
        selected = set(retry_urls) if retry_urls is not None else None
        entries = [item for item in entries if selected is None or item['url'] in selected]
        for number, entry in enumerate(entries, 1):
            if _cancelled(cancel):
                raise Cancelled()
            if all('楽曲まとめ' in item['title'] and not parse_dates(item['date_text']) for item in entry['index_entries']):
                result['skipped_urls'].append({'url': entry['url'], 'reason': 'song_summary_not_performance_page'})
                continue
            try:
                events = parse_event(client.get(entry['url'], refresh=refresh), entry)
                result['events'].extend(events)
                result['successful_urls'].append(entry['url'])
            except Cancelled:
                raise
            except (OSError, ValueError, UnicodeError) as error:
                result['failures'].append({'url': entry['url'], 'reason': str(error)[:500]})
                if isinstance(error, HTTPError) and error.code in (403, 429, 503):
                    # Stop rather than repeatedly requesting a source asking us
                    # to wait. Unattempted URLs stay absent from successful_urls.
                    result['aborted_reason'] = 'source_http_' + str(error.code)
                    break
            if progress:
                progress({'phase': 'collecting', 'completed': number, 'total': len(entries),
                          'url': entry['url'], 'events': len(result['events']),
                          'failures': len(result['failures'])})
    except Cancelled:
        result['cancelled'] = True
    except (OSError, ValueError, UnicodeError) as error:
        result['failures'].append({'url': INDEX_URL, 'reason': str(error)[:500]})
    result['events'].sort(key=lambda event: (event['date'], event['id']), reverse=True)
    result['stats'] = {'indexed_pages': len(result['index_urls']),
                       'successful_pages': len(result['successful_urls']),
                       'failed_pages': len(result['failures']), 'events': len(result['events']),
                       'non_event_pages': len(result['skipped_urls']),
                       'song_rows': sum(len(event['songs']) for event in result['events']),
                       'network_requests': client.network_count, 'cache_hits': client.cache_count}
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', required=True)
    parser.add_argument('--output', required=True, help='Candidate JSON, never implicitly the bundled catalog')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--retry-failures', help='Previous candidate JSON with a failures list')
    args = parser.parse_args()
    retry = None
    if args.retry_failures:
        retry = [item['url'] for item in json.loads(Path(args.retry_failures).read_text(encoding='utf-8'))['failures']]
    def report(value):
        if value['completed'] % 25 == 0 or value['completed'] == value['total']:
            print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        result = collect(args.cache_dir, progress=report, refresh=args.refresh, retry_urls=retry)
    except KeyboardInterrupt:
        print('Cancelled. Cached pages are retained; rerun to resume.')
        return 130
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(target)
    print(json.dumps(result['stats'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
