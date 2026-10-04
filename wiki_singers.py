"""Read explicitly labeled Namu lyric colors, anchored to the full original text.

Colors have meaning only inside a table's own performer legend. Neither global
image colors, translated rows nor a unit's roster establish a singer credit.
"""
from functools import lru_cache
import re

from artist_colors import catalog as color_catalog
from artist_names import catalog as name_catalog, name_key
from lyric_parts import text_fingerprint
from lyrics import normalized


_ALL = {'합창', '전원', '전체', 'all', '全員'}
_JA_LATIN = re.compile(r'[A-Za-z\u3040-\u30ff\u3400-\u9fff]')
_KO = re.compile(r'[가-힣]')
_BLOCKS = {'td', 'th', 'div', 'p', 'tr', 'li'}
_SKIP = {'script', 'style', 'sup', 'rt', 'rp', 'noscript', 'template'}


def _css_color(value):
    value = value.strip().lower()
    if re.fullmatch(r'#[0-9a-f]{3}', value):
        return '#' + ''.join(char * 2 for char in value[1:])
    if re.fullmatch(r'#[0-9a-f]{6}', value):return value
    rgb = re.fullmatch(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(1(?:\.0*)?))?\s*\)', value)
    if rgb and all(0 <= int(number) <= 255 for number in rgb.groups()[:3]):
        return '#%02x%02x%02x' % tuple(int(number) for number in rgb.groups()[:3])
    return {'white': '#ffffff', 'black': '#000000', 'red': '#ff0000',
            'blue': '#0000ff', 'green': '#008000', 'yellow': '#ffff00'}.get(value)


def _style(node):
    result = {}
    for item in node.get('style', '').split(';'):
        key, separator, value = item.partition(':')
        if separator:result[key.strip().lower()] = re.sub(r'\s*!important\s*$', '', value.strip(), flags=re.I)
    return result


def _prune_hidden(soup):
    # Match the browser transfer's static visibility rules before reading either
    # legends or lyrics. Removing a hidden ancestor also removes its tables.
    for node in soup.find_all():
        if node.attrs is None:continue  # A previously removed ancestor owned it.
        style = {key: value.lower() for key, value in _style(node).items()}
        try:transparent = float(style.get('opacity', '1')) == 0
        except ValueError:transparent = False
        if (node.name in _SKIP or node.get('hidden') is not None
                or node.get('role') == 'tooltip' or node.get('aria-hidden') == 'true'
                or style.get('display') == 'none'
                or style.get('visibility') in ('hidden', 'collapse')
                or style.get('content-visibility') == 'hidden' or transparent):
            node.decompose()


@lru_cache(maxsize=32)
def _performer_names(artists=()):
    # Recording credits can disambiguate a given name, never invent a color
    # assignment or add an unlabelled performer to the table's chorus.
    colors, names = color_catalog(), name_catalog()
    credited = {re.sub(r'\s*\(\s*CV\s*[.:：]?[^()]+\)\s*$', '',
                       names.translate(artist, idolmaster=True), flags=re.I).strip()
                for artist in artists}
    choices = {}
    for alias, owner in colors.names.items():
        if colors.kinds.get(owner) != 'idol':continue
        display = names.names.get(owner)
        if not display:continue
        choices.setdefault(alias, set()).add(display)
        choices.setdefault(name_key(display.rsplit(' ', 1)[-1]), set()).add(display)
    resolved = {}
    for key, values in choices.items():
        if len(values) > 1:values = values & credited
        if len(values) == 1:resolved[key] = next(iter(values))
    return resolved


def _singers(label, artists=()):
    label = re.sub(r'\[\d+\]', '', label).strip()
    if name_key(label) in _ALL:return 'all'
    names = _performer_names(artists)
    direct = names.get(name_key(label))
    if direct:return (direct,)
    parts = re.split(r'\s*[,，、/&＆+＋·ㆍ×]\s*', label)
    resolved = [names.get(name_key(part)) for part in parts]
    if len(parts) > 1 and all(resolved):return tuple(dict.fromkeys(resolved))
    return None


def _legend(table, artists=()):
    entries, ignored = [], set()
    for row in table.find_all('tr'):
        if row.find_parent('table') is not table:continue
        cells = row.find_all(['td', 'th'], recursive=False)
        # Swatch/name rows explicitly associate a foreground color with a name.
        if len(cells) == 2:
            for swatch, label in (cells, cells[::-1]):
                if swatch.get_text(strip=True):continue
                style = _style(swatch)
                color = _css_color(style.get('background-color', style.get('background', '')))
                if color:
                    singers = _singers(label.get_text(' ', strip=True), artists)
                    entries.append((color, singers))
                    ignored.add(id(row))
                    break
        # Some tables put the colored names together in a single legend cell.
        if len(cells) == 1 and not cells[0].find('br'):
            colored = [node for node in cells[0].find_all(style=True)
                       if 'color' in _style(node)]
            if colored and all(_singers(node.get_text(' ', strip=True), artists) for node in colored):
                for node in colored:
                    entries.append((_css_color(_style(node)['color']),
                                    _singers(node.get_text(' ', strip=True), artists)))
                ignored.add(id(row))
        label = name_key(row.get_text(' ', strip=True))
        if label in {'캐릭터이미지컬러', '캐릭터이미지색상', '파트별색상', '가사색상'}:
            ignored.add(id(row))
    members = tuple(dict.fromkeys(name for _, singers in entries
                                 if singers and singers != 'all' for name in singers))
    complete = bool(members) and all(singers for _, singers in entries)
    choices = {}
    for color, singers in entries:
        if not color:continue
        if singers == 'all':singers = members if complete else None
        choices.setdefault(color, set()).add(singers)
    legend = {color: next(iter(values)) for color, values in choices.items()
              if len(values) == 1 and None not in values}
    return legend, ignored


def _rows(table, ignored):
    from bs4 import Comment, NavigableString

    rows, current = [], []

    def finish():
        if current:
            rows.append(tuple(current))
            current.clear()

    def walk(node, color=None):
        if isinstance(node, Comment):return
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node))
            if text:current.append((text, color))
            return
        if node.name in _SKIP or id(node) in ignored:return
        if node.name == 'a' and re.fullmatch(r'\[\d+\]', node.get_text(strip=True)):return
        style = _style(node)
        if (node.get('hidden') is not None or style.get('display') == 'none'
                or style.get('visibility') == 'hidden'):return
        foreground = style.get('color', node.get('color') if node.name == 'font' else None)
        if foreground is not None and foreground.lower() not in ('inherit', 'unset'):
            color = _css_color(foreground)
        if node.name == 'br':
            finish()
            return
        if node.name in _BLOCKS:finish()
        for child in node.children:walk(child, color)
        if node.name in _BLOCKS:finish()

    walk(table)
    finish()
    return rows


def _table_parts(table, lines, artists=()):
    legend, ignored = _legend(table, artists)
    if not legend:return None
    originals, parts, position = [], [], 0
    for row in _rows(table, ignored):
        raw = ''.join(text for text, _ in row)
        if _KO.search(raw) or not _JA_LATIN.search(raw):continue
        # Remove literal numbered references while keeping span offsets aligned.
        keep = [True] * len(raw)
        for match in re.finditer(r'\[\d+\]', raw):
            keep[match.start():match.end()] = [False] * len(match[0])
        text = ''.join(char for char, valid in zip(raw, keep) if valid)
        original = normalized(text)
        if not original:continue
        originals.append(original)
        cursor, pieces = 0, []
        for chunk, color in row:
            normalized_chunk = normalized(''.join(char for index, char in enumerate(chunk, cursor) if keep[index]))
            cursor += len(chunk)
            pieces.append(normalized_chunk)
            end = position + len(normalized_chunk)
            singers = legend.get(color)
            if singers and position < end:
                if parts and parts[-1]['end'] == position and parts[-1]['singers'] == list(singers):
                    parts[-1]['end'] = end
                else:parts.append({'start': position, 'end': end, 'singers': list(singers)})
            position = end
        # Unicode composition across differently colored runs is ambiguous.
        if ''.join(pieces) != original:return None
    if ''.join(originals) != ''.join(normalized(line.original) for line in lines):return None
    if not parts:return None
    fingerprint, length = text_fingerprint(lines)
    return {'text_sha256': fingerprint, 'text_length': length, 'parts': parts}


def extract_colored_parts(html, lines, artists=()):
    """Return verified normalized singer spans, or None without sufficient evidence.

    ``html`` can be a selected table's outerHTML or a complete pasted page.
    ``lines`` must be the independently accepted original lyric sequence. Exact
    whole-document agreement preserves repeated phrases in their source order.
    ``artists`` supplies current recording credits to distinguish namesakes.
    Unknown colors and conflicting legends remain uncovered character spans.
    """
    from bs4 import BeautifulSoup

    if not isinstance(html, str) or not html or len(html) > 1_000_000 or not lines:return None
    soup = BeautifulSoup(html, 'html.parser')
    _prune_hidden(soup)
    candidates = []
    for table in soup.find_all('table'):
        candidate = _table_parts(table, lines, tuple(artists))
        if candidate and candidate not in candidates:candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None
