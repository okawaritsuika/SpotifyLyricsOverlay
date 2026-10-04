"""Offline Korean display names; never change playback identity or search terms."""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


def name_key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()


_SEPARATOR = re.compile(r'\s*[,，、;；×]\s*|\s+[xXｘＸ&＆/／+＋]\s+|[\r\n]+')
_CV = re.compile(r'^(CV\s*[.:：]?\s*)(.+)$', re.I)
_NAME_ONLY_CHARACTERS = frozenset(name_key(name) for name in
                                ('토모리 마나카', '카미즈루 코스모', '레트라'))
_AFFILIATION = re.compile(r'\s+from\s+THE\s+IDOLM@STER\s+POPLINKS\s*$', re.I)


def split_artist_affiliation(text):
    """Keep the published project suffix separate from its last performer."""
    match = _AFFILIATION.search(text)
    if match and text[:match.start()].strip():return text[:match.start()], text[match.start():]
    return text, ''


class ArtistNames:
    def __init__(self, entries=()):
        choices = {}
        for entry in entries:
            if not isinstance(entry, dict):continue
            korean = entry.get('ko')
            original=entry.get('name')
            # Keep official Latin-script unit names, including their punctuation.
            # Personal and CV names continue to use the Korean display catalog.
            letters=[char for char in unicodedata.normalize('NFKC',original)
                     if char.isalpha()] if isinstance(original,str) else []
            roman_group=(entry.get('kind')=='group' and letters
                         and all('LATIN' in unicodedata.name(char,'') for char in letters))
            display=original.strip() if roman_group else korean
            if not isinstance(display,str) or not display.strip():continue
            aliases = entry.get('aliases', [])
            names = [original] + (aliases if isinstance(aliases, list) else [])
            if roman_group and isinstance(korean,str):names.append(korean)
            for name in names:
                if isinstance(name, str) and name.strip():
                    choices.setdefault(name_key(name), set()).add(display)
        # A conflicting alias must not silently select a different performer.
        self.names = {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}
        self.conflicts = {key for key, values in choices.items() if len(values) > 1}

    def translate(self, value, *, idolmaster=False):
        if not isinstance(value, str):return ''
        text = value.strip()
        key = name_key(text)
        short = len(key) == 1 or bool(re.fullmatch(r'[a-z0-9_.+-]{1,5}', key))
        if key in self.names and (idolmaster or not short):return self.names[key]
        credit, affiliation = split_artist_affiliation(text)
        if affiliation and key not in self.conflicts:
            return self.translate(credit, idolmaster=idolmaster) + affiliation
        # Spotify often puts the voice actor inside the character's artist name.
        bracketed = re.fullmatch(r'(.*?)\s*[（(]([^()（）]*)[)）]', text)
        if bracketed:
            character, credit = bracketed.groups()
            cv = _CV.match(credit)
            character = self.translate(character, idolmaster=idolmaster)
            if (name_key(character) in _NAME_ONLY_CHARACTERS
                    and _CV.match(unicodedata.normalize('NFKC', credit).strip())):return character
            if cv:
                credit = cv[1] + self.translate(cv[2], idolmaster=idolmaster)
            else:credit = self.translate(credit, idolmaster=idolmaster)
            return character + ' (' + credit + ')'
        # Split only whole performer tokens, preserving unknown names and separators.
        parts = []
        depth = 0
        start = index = 0
        while index < len(text):
            char = text[index]
            if char in '(（[［':depth += 1
            elif char in ')）]］':depth = max(0, depth - 1)
            match = _SEPARATOR.match(text, index) if depth == 0 else None
            if match:
                parts.append(self.translate(text[start:index], idolmaster=idolmaster))
                parts.append(match[0])
                index = start = match.end()
            else:index += 1
        if parts:
            parts.append(self.translate(text[start:], idolmaster=idolmaster))
            return ''.join(parts)
        return text

    def display(self, artists, album=''):
        context = unicodedata.normalize('NFKC', album).casefold()
        idolmaster = any(word in context for word in
                         ('idolm@ster', 'idolmaster', 'アイドルマスター', '初星学園',
                          'shiny colors', 'million live', 'cinderella girls', 'sidem'))
        return ', '.join(self.translate(name, idolmaster=idolmaster) for name in artists if name.strip())


@lru_cache(maxsize=1)
def catalog():
    entries = []
    directory = Path(__file__).resolve().parent / 'resources' / 'artist_names'
    for name in ('imas_legacy.json', 'imas_modern.json'):
        try:
            data = json.loads((directory / name).read_text(encoding='utf-8'))
            if data.get('schema_version') == 1 and isinstance(data.get('entries'), list):
                entries.extend(data['entries'])
        except (OSError, ValueError, AttributeError):
            continue  # Display the supplied artist name if the optional catalog is unavailable.
    return ArtistNames(entries)


def artist_label(artists, album=''):
    return catalog().display(artists, album)


def character_name_only(name):
    """Characters whose display intentionally omits voice-actor annotations."""
    return name_key(catalog().translate(name, idolmaster=True)) in _NAME_ONLY_CHARACTERS
