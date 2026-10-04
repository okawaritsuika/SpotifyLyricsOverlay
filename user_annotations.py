"""Local, explicitly supplied lyric annotations; never supplies text or clocks.

Short recording edits may reuse exact complete phrases from the same named
song and recording artist. Conflicting repetitions never select a singer.
"""
from dataclasses import dataclass, field
import itertools
import json
from pathlib import Path
import re
import unicodedata

from lyrics import normalized
from lyric_parts import LyricParts


MAX_BYTES = 2 * 1024 * 1024
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
