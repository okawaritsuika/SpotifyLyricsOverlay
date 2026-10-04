"""Offline idol/group colors and member gradients without changing display text."""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

from artist_names import name_key, split_artist_affiliation


RESOURCE_DIRECTORY = Path(__file__).resolve().parent / 'resources'
_SEPARATOR = re.compile(r'\s*[,，、;；×]\s*|\s+[xXｘＸ&＆/／+＋]\s+|[\r\n]+')
_CV = re.compile(r'^CV\s*[.:：]?\s*\S', re.I)
_CONTEXT = ('idolm@ster', 'idolmaster', 'アイドルマスター', '初星学園',
            'shiny colors', 'million live', 'cinderella girls', 'sidem')


def _artist_ranges(text, known_names):
    """Split performer credits without cutting names inside a CV credit."""
    credit, affiliation = split_artist_affiliation(text)
    if affiliation and name_key(text) not in known_names:text = credit
    spans = []
    depth = 0
    start = index = 0
    while index < len(text):
        char = text[index]
        if char in '(（[［':depth += 1
        elif char in ')）]］':depth = max(0, depth - 1)
        match = _SEPARATOR.match(text, index) if depth == 0 else None
        if match:
            spans.append((start, index))
            index = start = match.end()
        else:index += 1
    spans.append((start, len(text)))
    parts = []
    index = 0
    while index < len(spans):
        end = index + 1
        # Preserve exact unit names whose own punctuation is also a separator,
        # for example "High×Joker × another performer".
        for candidate in range(len(spans), index + 1, -1):
            if name_key(text[spans[index][0]:spans[candidate - 1][1]]) in known_names:
                end = candidate
                break
        parts.append((spans[index][0], spans[end - 1][1]))
        index = end
    return parts


class ArtistColors:
    def __init__(self, entries=(), names=()):
        colors = {}
        choices = {}
        groups = {}
        for entry in entries:
            if not isinstance(entry, dict):continue
            name, color, kind = entry.get('name'), entry.get('color'), entry.get('kind')
            if not isinstance(name, str) or not name.strip():continue
            if kind not in ('idol', 'group'):continue
            key = name_key(name)
            choices.setdefault(key, set()).add(key)
            if isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                colors.setdefault(key, set()).add((color.lower(), kind))
            if kind == 'group':groups.setdefault(key, []).append(entry.get('members'))
        # Include owners without a color too, so a shared alias stays ambiguous.
        for entry in names:
            if not isinstance(entry, dict):continue
            name = entry.get('name')
            if not isinstance(name, str) or not name.strip():continue
            key = name_key(name)
            aliases = entry.get('aliases', [])
            for alias in [name, entry.get('ko')] + (aliases if isinstance(aliases, list) else []):
                if isinstance(alias, str) and alias.strip():
                    choices.setdefault(name_key(alias), set()).add(key)
        self.colors = {key: next(iter(values))[0] for key, values in colors.items()
                       if len(values) == 1}
        self.kinds = {key: next(iter(values))[1] for key, values in colors.items() if len(values) == 1}
        self.names = {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}
        self.known_names = set(choices)
        self.gradient_groups = set()
        for key, rosters in groups.items():
            # Published colors win; conflicting colors or incomplete rosters do
            # not silently turn into a guessed subset of the group's members.
            if key in colors:continue
            resolved = {}
            for roster in rosters:
                if not isinstance(roster, list) or not roster or any(not isinstance(name, str) for name in roster):
                    break
                members = tuple(dict.fromkeys(self.names.get(name_key(name)) for name in roster))
                if any(self.kinds.get(member) != 'idol' for member in members):break
                resolved.setdefault(frozenset(members),members)
            else:
                if len(resolved) != 1:continue
                self.colors[key] = tuple(self.colors[member] for member in next(iter(resolved.values())))
                self.kinds[key] = 'group'
                self.gradient_groups.add(key)

    def _color(self, text, idolmaster, kind=None):
        key = name_key(text)
        short = len(key) == 1 or bool(re.fullmatch(r'[a-z0-9_.+-]{1,5}', key))
        if short and not idolmaster:return None
        owner = self.names.get(key)
        if kind is not None and self.kinds.get(owner) != kind:return None
        return self.colors.get(owner)

    def _resolve(self, text, offset, idolmaster):
        offset += len(text) - len(text.lstrip())
        text = text.strip()
        color = self._color(text, idolmaster)
        if color:return ((offset, offset + len(text), color),)
        # Uncolored groups, voice actors and ambiguous aliases stay neutral even if
        # their names contain punctuation that resembles a performer separator.
        if name_key(text) in self.known_names:return ()
        parts = _artist_ranges(text, self.known_names)
        if len(parts) > 1 or parts[0] != (0, len(text)):
            return tuple(span for start, end in parts
                         for span in self._resolve(text[start:end], offset + start, idolmaster))
        bracketed = re.fullmatch(r'(.*?)\s*[（(]([^()（）]*)[)）]', text)
        if bracketed and _CV.match(unicodedata.normalize('NFKC', bracketed[2])):
            color = self._color(bracketed[1], idolmaster, kind='idol')
            if color:return ((offset, offset + len(text), color),)
        return ()

    def spans(self, display_text, album=''):
        if not isinstance(display_text, str):return ()
        return self._spans(display_text, album if isinstance(album, str) else '')

    @lru_cache(maxsize=256)
    def _spans(self, display_text, album):
        context = unicodedata.normalize('NFKC', album).casefold()
        idolmaster = any(word in context for word in _CONTEXT)
        return self._resolve(display_text, 0, idolmaster)


def _read_entries(directory):
    entries = []
    for name in ('imas_legacy.json', 'imas_modern.json'):
        try:
            data = json.loads((directory / name).read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('schema_version') == 1 and isinstance(data.get('entries'), list):
                entries.extend(data['entries'])
        except (OSError, ValueError):
            continue  # An optional or malformed catalog must not interrupt playback.
    return entries


@lru_cache(maxsize=1)
def catalog():
    return ArtistColors(_read_entries(RESOURCE_DIRECTORY / 'artist_colors'),
                        _read_entries(RESOURCE_DIRECTORY / 'artist_names'))


def character_color_spans(display_text, album=''):
    """Return (start, end, hex-or-palette) for groups and character/CV credits."""
    return catalog().spans(display_text, album)
