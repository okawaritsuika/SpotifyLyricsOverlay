"""Local, explicitly supplied lyric annotations; never supplies text or clocks.

Short recording edits may reuse exact complete phrases from the same named
song and recording artist. Conflicting repetitions never select a singer.
"""
from dataclasses import dataclass, field
import hashlib
import itertools
import json
from pathlib import Path
import re
import unicodedata

from lyrics import normalized
from lyric_parts import LyricParts
from storage import atomic_json, read_json


MAX_BYTES = 2 * 1024 * 1024
MAX_SINGERS_PER_LINE = 10
SINGER_KEYS = '1234567890'
MAX_ROWS = 1500
MAX_BLOCK_ROWS = 4
MAX_BLOCK_CHARACTERS = 160
_SHORT_SUFFIX = re.compile(r'(?:\(|\[|\s[-–—]\s)\s*(?:(?:game|tv|short)\s*'
                           r'(?:size|ver\.?|version))\s*[\])]?$', re.I)


def _song_key(title):
    return normalized(_SHORT_SUFFIX.sub('', unicodedata.normalize('NFKC', title).strip()))


def _text(value, required=False, limit=4000):
    return (isinstance(value, str) and len(value) <= limit and '\0' not in value
            and (not required or bool(value.strip())))


@dataclass(frozen=True)
class UserAnnotation:
    reading: str = ''
    korean: str = ''
    singers: tuple[str, ...] = ()
    _meaning_key: str = field(default='', compare=False, repr=False)
    singer_partial: bool = False


EMPTY = UserAnnotation()


class SingerRoster:
    """Store the ordered number-key choices for a recording."""
    def __init__(self, directory):
        self.directory = Path(directory) / 'singer_rosters'
        self.key = self.path = self.names = self.error = None

    def select(self, key):
        if key == self.key:
            return
        self.key = key
        self.path = self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')
        self.names = self.error = None
        try:
            missing = object()
            value = read_json(self.path, missing)
            if value is not missing:
                if not isinstance(value, list) or not self.valid_names(value):
                    raise ValueError('저장된 곡 참여 가창자 명단의 형식을 확인하세요.')
                self.names = tuple(value)
        except (OSError, ValueError) as error:
            self.error = error

    def save(self, names):
        if self.error:
            raise self.error
        if not self.valid_names(names):
            raise ValueError('곡 참여 가창자는 서로 다른 이름으로 10명까지 지정할 수 있습니다.')
        if names != self.names:
            atomic_json(self.path, names)
            self.names = names

    def remove(self):
        """Restore automatic performer choices for the selected recording."""
        if self.error:
            raise self.error
        if self.path is None:
            raise ValueError('가창자 명단을 초기화할 곡을 선택하세요.')
        if self.names is None:
            return False
        self.path.unlink(missing_ok=True)
        self.names = None
        return True

    @staticmethod
    def valid_names(names):
        return (len(names) <= len(SINGER_KEYS) and all(_text(name, True) for name in names)
                and len(set(names)) == len(names))


class SingerEdits:
    """Store singer changes and single-line resets for an exact lyric layout."""
    def __init__(self, directory):
        self.directory = Path(directory) / 'singer_edits'
        self.key = None
        self.path = None
        self.rows = {}
        self.cleared = set()
        self.overrides = ()
        self.assigned_names = ()
        self.error = None

    def select(self, key, *, path_key=None):
        if key == self.key:return False
        self.key = key
        self.path = self.path_for(path_key or key) if key else None
        self.error = None
        rows, cleared = {}, set()
        if self.path:
            try:
                document = read_json(self.path, {})
                if not isinstance(document, dict):raise ValueError('singer rows')
                for index, entry in document.items():
                    carries = isinstance(entry, dict) and entry.keys() == {'carry'}
                    names = entry['carry'] if carries else entry
                    if (not index.isascii() or not index.isdecimal() or int(index) >= len(key[1])
                            or not isinstance(names, list) or len(names) > MAX_SINGERS_PER_LINE
                            or any(not _text(name, True, 4000) for name in names)):
                        raise ValueError('singer row')
                    row = int(index)
                    if names:rows[row] = tuple(names)
                    if carries or not names:cleared.add(row)
            except (OSError, ValueError) as error:
                rows, cleared = {}, set()
                self.error = error if isinstance(error, OSError) else ValueError(
                    '저장된 가창자 입력 파일의 형식을 확인하세요.')
        self._set_rows(rows, cleared)
        return True

    def path_for(self,key):
        return self.directory / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + '.json')

    def document(self, rows, cleared):
        result = {row: {'carry': names} if row in cleared else names for row,names in rows.items()}
        result.update({row: () for row in cleared - rows.keys()})
        return result

    def _set_rows(self, rows, cleared):
        self.rows, self.cleared = rows, cleared
        self.assigned_names = tuple(dict.fromkeys(name for row in sorted(rows) for name in rows[row]))
        overrides = []
        carry = None
        for index in range(len(self.key[1]) if self.key else 0):
            carry = rows.get(index, carry)
            overrides.append(() if index in cleared else carry)
        self.overrides = tuple(overrides)

    def has_explicit(self, index):
        return index in self.rows or index in self.cleared

    def save(self, index, names, *, keep_carry=False):
        if self.error:raise self.error
        if names and len(names) > MAX_SINGERS_PER_LINE:
            raise ValueError(f'한 줄에 가창자를 {MAX_SINGERS_PER_LINE}명까지 지정할 수 있습니다.')
        rows, cleared = self.rows.copy(), self.cleared.copy()
        if names:rows[index] = names
        elif not keep_carry:rows.pop(index, None)
        if names == ():cleared.add(index)
        else:cleared.discard(index)
        if rows != self.rows or cleared != self.cleared:
            # A reset may hide an assignment on this line while keeping it for later lines.
            atomic_json(self.path, self.document(rows,cleared))
            self._set_rows(rows, cleared)


def _same(values, default=''):
    choices = set(values)
    return next(iter(choices)) if len(choices) == 1 else default


def _same_meaning(values):
    choices = list(values)
    # Joining complete source rows inserts this separator. Ignore only that
    # join and spacing when the same supplied sentence also exists as one row.
    keys = {value._meaning_key for value in choices}
    return min((value.korean for value in choices), key=lambda value: (value.count(' / '), len(value))) if len(keys) == 1 else ''


class UserAnnotations:
    def __init__(self, directory):
        self.directory = Path(directory) / 'user_lyrics'
        self.documents = []
        self.errors = []
        self.parts = LyricParts(directory)
        self.errors.extend(self.parts.errors)
        if not self.directory.is_dir():return
        for path in sorted(self.directory.glob('*.json'))[:64]:
            try:
                with path.open('rb') as stream:body = stream.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:raise ValueError('document size')
                self.documents.append(self._document(json.loads(body)))
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
                self.errors.append((path.name, type(error).__name__))

    @staticmethod
    def _document(value):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not _text(value.get('title'), True)
                or not isinstance(value.get('source'), dict)
                or value['source'].get('kind') != 'user_provided'):
            raise ValueError('document metadata')
        artists = value.get('artists')
        rows = value.get('lines')
        if (not isinstance(artists, list) or not 0 < len(artists) <= 200
                or any(not _text(artist, True, 512) for artist in artists)
                or not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS):
            raise ValueError('document artists or rows')
        checked = []
        for row in rows:
            if (not isinstance(row, dict) or not _text(row.get('original'), True)
                    or not _text(row.get('reading', '')) or not _text(row.get('korean', ''))):
                raise ValueError('lyric row')
            singers = row.get('singers', [])
            if (not isinstance(singers, list) or len(singers) > 30
                    or any(not _text(singer, True, 200) for singer in singers)):
                raise ValueError('lyric singers')
            alternatives = row.get('original_variants', [])
            if (not isinstance(alternatives, list) or len(alternatives) > 3
                    or any(not isinstance(item, dict) or item.get('source') != 'local_spotify_original'
                           or not _text(item.get('text'), True) for item in alternatives)):
                raise ValueError('reviewed original variants')
            keys = tuple(dict.fromkeys([normalized(row['original'])]
                                       + [normalized(item['text']) for item in alternatives]))
            checked.append((keys, UserAnnotation(
                row.get('reading', '').strip(), row.get('korean', '').strip(),
                tuple(dict.fromkeys(singer.strip() for singer in singers)))))
        blocks = {}
        for first in range(len(checked)):
            for last in range(first, min(first + MAX_BLOCK_ROWS, len(checked))):
                if not all(checked[last][0]):break
                rows = [annotation for _, annotation in checked[first:last+1]]
                meanings = []
                for row in rows:
                    if not meanings or meanings[-1] != row.korean:meanings.append(row.korean)
                singers = _same((row.singers for row in rows), ())
                for pieces in itertools.islice(itertools.product(*(keys for keys, _ in checked[first:last+1])), 32):
                    key = ''.join(pieces)
                    if len(key) > MAX_BLOCK_CHARACTERS:continue
                    annotation = UserAnnotation(
                        rows[0].reading if first == last and key == checked[first][0][0] else '',
                        ' / '.join(meanings) if all(row.korean for row in rows) else '', singers,
                        re.sub(r'\s+', '', ''.join(meanings)) if all(row.korean for row in rows) else '')
                    blocks.setdefault(key, []).append(annotation)
        return {'title': _song_key(value['title']),
                'artists': {normalized(artist) for artist in artists}, 'blocks': blocks}

    def match(self, playback, lines, artists=(), reference_lines=()):
        """Return annotations for complete exact blocks; originals stay untouched."""
        result = [EMPTY] * len(lines)
        title = _song_key(playback.title)
        names = {normalized(name) for name in (playback.artist, *artists) if isinstance(name, str) and name.strip()}
        documents = [document for document in self.documents
                     if title and document['title'] == title and document['artists'] & names]
        if not documents or not lines:
            return self._with_parts(playback, lines, artists, reference_lines, result)
        keys = [normalized(line.original) for line in lines]
        first = 0
        while first < len(keys):
            key = ''
            following = first + 1
            for last in range(first, min(first + MAX_BLOCK_ROWS, len(keys))):
                if not keys[last]:break
                key += keys[last]
                if len(key) > MAX_BLOCK_CHARACTERS:break
                choices = [annotation for document in documents for annotation in document['blocks'].get(key, ())]
                if not choices:continue
                # Empty labels count as a different claim, so no singer spills
                # from a labelled refrain into an unlabelled occurrence.
                annotation = UserAnnotation(
                    _same(row.reading for row in choices) if first == last else '',
                    _same_meaning(choices),
                    _same((row.singers for row in choices), ()))
                for index in range(first, last + 1):result[index] = annotation
                following = last + 1
                break
            first = following
        return self._with_parts(playback, lines, artists, reference_lines, result)

    def _with_parts(self, playback, lines, artists, reference_lines, result):
        complete = self.parts.match(playback, lines, artists, reference_lines)
        known = self.parts.match(playback, lines, artists, reference_lines, allow_partial=True)
        return tuple(UserAnnotation(row.reading, row.korean, row.singers or names or partial,
                                    singer_partial=bool(partial and not row.singers and not names))
                     for row, names, partial in zip(result, complete, known))
