"""Read untimed lyrics from Idolmaster fan catalogs."""

import json
import re
import threading
import time
import unicodedata
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString

from catalog_sources import series_from_release
from lyrics import Line, normalized


SIDEM_INDEX = 'https://disgaearobkt.hatenadiary.jp/entry/2021/07/22/213831'
GAKUMAS_INDEX = 'https://semython.github.io/GAKUMASU_lyrics/sidebar.html'
SHINY_INDEX = ('https://shinycolors.wiki/w/api.php?action=query&list=categorymembers'
               '&cmtitle=Category%3ASongs&cmnamespace=0&cmlimit=max&format=json')
_SHINY_SEARCH = {'action': 'query', 'list': 'search', 'srnamespace': '0', 'srlimit': '10', 'format': 'json'}
_INDEXES = {'sidem': SIDEM_INDEX, 'gakuen': GAKUMAS_INDEX, 'shiny': SHINY_INDEX}
_SOURCES = {'sidem': 'imas_sidem', 'gakuen': 'imas_gakumas', 'shiny': 'imas_shiny'}
_MAX_HTML = 2_000_000
_INDEX_TTL = 900
_index_cache = {}
_index_lock = threading.Lock()
_SIDEM_PATH = re.compile(r'/entry/\d{4}/\d{2}/\d{2}/\d{6}')
_GAKUMAS_PATH = re.compile(r'/GAKUMASU_lyrics/[A-Za-z0-9_!\-]+\.html')
_SHINY_PATH = re.compile(r'/wiki/(?:[A-Za-z0-9_.~!()@\-/]|%[0-9A-Fa-f]{2})+')
_SIDEM_SUFFIX = re.compile(r'\s*パート分け\s*$')
_SIDEM_SINGER = re.compile(r'^(?:唄|歌|歌唱)\s*[:：]\s*')
_SIDEM_CREDIT = re.compile(r'^(?:唄|歌|歌唱)\s*[:：]|\(CV[.:：]', re.I)
_SIDEM_FOOTNOTE = re.compile(
    r'^(?:\d{1,2}/\d{1,2}\s|※\s*|追記\s*[:：]|関連記事\s*[:：]?)'
    r'|(?:修正しました|追加しました|指摘してください|いかがでしたか|良かったですね)[。！!？?]*$'
)
_NO_VOCAL = re.compile(
    r'\b(?:instrumental|off[\s_-]*vocal|karaoke)\b|カラオケ|インスト(?:ゥルメンタル)?',
    re.I,
)
# The FLASH LIGHT game excerpt matches the short Saisuta lyric article.
# GO FOR IT!! has 339 normalized characters against 683 in the full fan text.
_SIDEM_PARTIAL = frozenset((
    'https://disgaearobkt.hatenadiary.jp/entry/2023/02/05/162254',
    'https://disgaearobkt.hatenadiary.jp/entry/2023/01/03/152921',
))


def source_for(series):
    return _SOURCES.get(series, '')


def series_for(album, artist=''):
    """Use explicit brand evidence from the track credits."""
    series = set(series_from_release(album or ''))
    artist_key = normalized(artist or '')
    if artist_key in ('初星学園', '学園アイドルマスター'):
        series.add('gakuen')
    elif artist_key in ('sidem', 'アイドルマスターsidem', '315allstars', '315stars'):
        series.add('sidem')
    elif artist_key in ('shinycolors', 'シャイニーカラーズ'):
        series.add('shiny')
    if len(series) == 1:
        key = next(iter(series))
        if key in _INDEXES:
            return key
    return ''


def _url_series(address):
    if address == SHINY_INDEX:
        return 'shiny'
    value = urlsplit(address)
    if (value.scheme != 'https' or value.username or value.password
            or value.port not in (None, 443) or value.fragment):
        raise ValueError('Invalid Idolmaster lyrics URL')
    if value.hostname == 'shinycolors.wiki' and value.path == '/w/api.php':
        params = parse_qs(value.query, strict_parsing=True)
        title = params.pop('srsearch', ())
        if (params == {key: [item] for key, item in _SHINY_SEARCH.items()}
                and len(title) == 1 and 2 < len(title[0]) <= 302):
            return 'shiny'
    if value.query:
        raise ValueError('Invalid Idolmaster lyrics URL')
    if value.hostname == 'disgaearobkt.hatenadiary.jp' and _SIDEM_PATH.fullmatch(value.path):
        return 'sidem'
    if value.hostname == 'semython.github.io' and _GAKUMAS_PATH.fullmatch(value.path):
        return 'gakuen'
    if (value.hostname == 'shinycolors.wiki' and _SHINY_PATH.fullmatch(value.path)
            and not any(part in ('', '.', '..') for part in value.path.split('/')[2:])):
        return 'shiny'
    raise ValueError('Invalid Idolmaster lyrics URL')


def validate_url(address):
    """Accept only the supported track-page hosts and paths."""
    series = _url_series(address)
    path = urlsplit(address).path
    if (path in (urlsplit(_INDEXES[series]).path, '/w/api.php')
            or series == 'gakuen' and path.rsplit('/', 1)[-1] in ('index.html', 'SearchRight.html')):
        raise ValueError('Expected an Idolmaster song page')
    return address


def fetch_html(address):
    series = _url_series(address)
    request = Request(address, headers={'User-Agent': 'SpotifyLyricsOverlay/1.0'})
    with urlopen(request, timeout=8) as response:
        if _url_series(response.geturl()) != series:
            raise ValueError('Unexpected Idolmaster lyrics redirect')
        content = response.read(_MAX_HTML + 1)
    if len(content) > _MAX_HTML:
        raise ValueError('Idolmaster lyrics page exceeds the size limit')
    return content.decode('utf-8-sig')


def _soup(page):
    if isinstance(page, bytes):
        page = page.decode('utf-8-sig')
    return BeautifulSoup(page, 'html.parser')


def _index_links(series, page):
    if series == 'shiny':
        entries = json.loads(page).get('query', {}).get('categorymembers', ())
        for item in entries:
            title = item.get('title')
            if isinstance(title, str) and item.get('ns') == 0:
                yield title, '/wiki/' + quote(title.replace(' ', '_'), safe='/!()@')
        return
    soup = _soup(page)
    if series == 'sidem':
        body = soup.select_one('article .entry-content')
        links = body.select('a[href]') if body else ()
    else:
        links = soup.select('a[href]')
    for link in links:
        yield link.get_text(strip=True), link['href']


def _index(series, fetch):
    address = _INDEXES[series]
    if fetch is None:
        with _index_lock:
            saved = _index_cache.get(series)
            if saved and time.monotonic() < saved[0]:
                return saved[1]
    entries = {}
    for title, link in _index_links(series, (fetch or fetch_html)(address)):
        key = normalized(title)
        if not key:
            continue
        url = urljoin(address, link)
        try:
            validate_url(url)
            if _url_series(url) != series:
                continue
        except ValueError:
            continue
        entries.setdefault(key, set()).add(url)
    if entries and fetch is None:
        with _index_lock:
            _index_cache[series] = (time.monotonic() + _INDEX_TTL, entries)
    return entries


def _gakumas_rows(soup):
    spans = soup.select('span.line[data-part]')
    if not spans:
        return ()
    body = spans[0].parent
    if any(span.parent is not body for span in spans):
        # These pages combine different recordings in separate HTML branches.
        return ()
    chunks = []
    for item in body.children:
        if item.name == 'br':
            chunks.append('\n')
        elif item.name == 'span' and 'line' in item.get('class', ()):
            chunks.append(item.get_text())
        elif type(item) is NavigableString and not str(item).strip():
            if '\u3000' in item:
                chunks.append('\u3000')
            elif '\n' not in item and '\r' not in item:
                chunks.append(' ')
    return ''.join(chunks).splitlines()


def _sidem_rows(soup, key):
    body = soup.select_one('article .entry-content')
    if body is None:
        return ()
    for element in body.select('script, style, iframe, rt, rp'):
        element.decompose()
    for element in body.select('br'):
        element.replace_with('\n')
    paragraphs = [tuple(row.strip() for row in node.get_text().splitlines() if row.strip())
                  for node in body.find_all(('p', 'div'), recursive=False)]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    start = next((i for i, rows in enumerate(paragraphs[:8])
                  if normalized(rows[0]) == key), None)
    if start is None:
        return ()
    credit = paragraphs[start][1:] or (paragraphs[start + 1] if start + 1 < len(paragraphs) else ())
    if not credit or not any(_SIDEM_CREDIT.search(unicodedata.normalize('NFKC', row)) for row in credit):
        return ()
    first_lyric = start + (1 if len(paragraphs[start]) > 1 else 2)
    credit_keys = normalized(' '.join(credit))
    groups = set()
    for row in credit:
        label = re.split(r'[\[［]', _SIDEM_SINGER.sub('', row), maxsplit=1)[0]
        if not _SIDEM_CREDIT.search(label):
            groups.add(normalized(label))

    def remove_credit(match):
        value = match.group(1).strip()
        people = [normalized(person) for person in re.split(r'[・&＆＋+、/]', value)]
        if all(person and (person == '全員' or person in groups
                           or re.search(r'[ぁ-んァ-ヶ一-龯]', person) and person in credit_keys)
               for person in people):
            return ''
        return match.group()

    result = []
    for paragraph in paragraphs[first_lyric:]:
        if any(_SIDEM_CREDIT.search(unicodedata.normalize('NFKC', row)) for row in paragraph):
            return ()
        if _SIDEM_FOOTNOTE.search(paragraph[0]):
            break
        result.extend(re.sub(r'[\[［]([^\[\]［］]{1,65})[\]］]', remove_credit, row)
                      for row in paragraph)
    return result


def _shiny_rows(soup):
    table = soup.select_one('table.lyrics')
    if table is None:
        return ()
    rows = table.select('tr')
    if not rows:
        return ()
    headings = rows[0].find_all('th', recursive=False)
    column = next((i for i, node in enumerate(headings)
                   if normalized(node.get_text()) == 'japanesekanjilyrics'), None)
    if column is None:
        return ()
    lyrics = []
    for row in rows[1:]:
        cells = row.find_all('td', recursive=False)
        if len(cells) <= column:
            continue
        cell = cells[column]
        for node in cell.select('rt, rp, sup.reference'):
            node.decompose()
        for node in cell.select('br'):
            node.replace_with('\n')
        lyrics.extend(cell.get_text().splitlines())
    return lyrics


def _shiny_original_title(soup):
    for cell in soup.select('table.infobox td'):
        if normalized(cell.get_text()) == 'originaltitle':
            value = cell.find_next_sibling('td')
            if value is not None:
                return value.get_text(strip=True)
    return ''


def _shiny_search(title, index, fetch):
    # Wiki article titles are often romanized. Resolve them through MediaWiki
    # search and limit candidates to the song category.
    url = 'https://shinycolors.wiki/w/api.php?' + urlencode(
        {**_SHINY_SEARCH, 'srsearch': '"' + title.replace('"', '') + '"'}
    )
    matches = json.loads(fetch(url)).get('query', {}).get('search', ())
    addresses = set()
    for hit in matches:
        addresses.update(index.get(normalized(hit.get('title', '')), ()))
    return sorted(addresses)


def find_candidate(title, series, *, fetch=None):
    """Return exact-title original lines from a single supported brand."""
    if series not in _INDEXES or _NO_VOCAL.search(unicodedata.normalize('NFKC', title)):
        return None
    key = normalized(title)
    if not key:
        return None
    index = _index(series, fetch)
    transport = fetch or fetch_html
    addresses = index.get(key, ())
    if not addresses and series == 'shiny' and re.search(r'[ぁ-んァ-ヶ一-龯]', title):
        candidates = []
        for address in _shiny_search(title, index, transport):
            soup = _soup(transport(address))
            if normalized(_shiny_original_title(soup)) == key:
                candidates.append((address, _shiny_rows(soup)))
        if len(candidates) != 1:
            return None
        address, rows = candidates[0]
    else:
        if len(addresses) != 1:
            return None
        address = next(iter(addresses))
        rows = _exact_rows(address, key, series, transport)
    lines = tuple(Line(row.strip(), '', '') for row in rows if normalized(row))
    if not lines:
        return None
    return {'source': source_for(series), 'url': address, 'lines': lines}


def _exact_rows(address, key, series, fetch):
    if address in _SIDEM_PARTIAL:
        return ()
    soup = _soup(fetch(address))
    heading = soup.select_one('article h1.entry-title' if series == 'sidem' else 'h1')
    if heading is None:
        return ()
    heading_text = heading.get_text(strip=True)
    if series == 'sidem':
        heading_text = _SIDEM_SUFFIX.sub('', heading_text)
    if normalized(heading_text) != key:
        return ()
    if series == 'sidem':
        return _sidem_rows(soup, key)
    if series == 'shiny':
        return _shiny_rows(soup)
    return _gakumas_rows(soup)
