"""Source-attributed singer spans anchored to a complete, verified lyric text.

The catalog stores only character offsets, text hashes and performer facts.
It contains no lyric text, translations or timing information.
"""
import hashlib
import json
from collections import Counter
from pathlib import Path
import re
from urllib.parse import urlsplit

from lyrics import normalized
from lyric_match import match_lyrics


RESOURCE_PATH = Path(__file__).resolve().parent / 'resources' / 'lyric_parts.json'
MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT_LENGTH = 20000


def text_fingerprint(lines):
    text = ''.join(normalized(line.original) for line in lines)
    return hashlib.sha256(text.encode('utf-8')).hexdigest(), len(text)


def _text(value, required=True, limit=2000):
    return (isinstance(value, str) and len(value) <= limit
            and not any(char in value for char in '\0\r\n')
            and (not required or bool(value.strip())))


def _strings(value, limit=100):
    return (isinstance(value, list) and 0 < len(value) <= limit
            and all(_text(item, limit=500) for item in value))


def _source(value):
    if not isinstance(value, dict) or not _text(value.get('url')):return False
    try:
        url = urlsplit(value['url'])
        return (url.scheme == 'https' and bool(url.hostname) and not url.username
                and value.get('kind') in ('fan_part_transcription', 'official', 'user_provided',
                                          'independent_lyrics', 'recording_metadata')
                and _text(value.get('note', ''), required=False))
    except ValueError:return False


def _recording(value):
    if not isinstance(value, dict):return None
    length = value.get('text_length')
    aliases = value.get('album_aliases', [])
    if ('album_aliases' in value and
            (not _strings(aliases, 20) or not _text(value.get('album'))
             or not all(normalized(alias) for alias in aliases))):
        return None
    if (not _text(value.get('title')) or not _strings(value.get('artists'))
            or not _text(value.get('album', ''), required=False)
            or not isinstance(value.get('text_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', value['text_sha256']) is None
            or type(length) is not int or not 0 < length <= MAX_TEXT_LENGTH
            or not isinstance(value.get('sources'), list) or not 0 < len(value['sources']) <= 20
            or not all(_source(source) for source in value['sources'])
            or not isinstance(value.get('parts'), list) or not 0 < len(value['parts']) <= 2000):
        return None
    parts = []
    for part in value['parts']:
        if (not isinstance(part, dict) or type(part.get('start')) is not int
                or type(part.get('end')) is not int
                or not 0 <= part['start'] < part['end'] <= length
                or not _strings(part.get('singers'), 30)):
            return None
        parts.append((part['start'], part['end'], tuple(dict.fromkeys(part['singers']))))
    parts.sort(key=lambda item: (item[0], item[1]))
    # Conflicting or overlapping source spans must be reviewed before use.
    if any(left[1] > right[0] for left, right in zip(parts, parts[1:])):return None
    return {'title': normalized(value['title']),
            'artists': frozenset(normalized(name) for name in value['artists']),
            'album': normalized(value.get('album', '')),
            'album_aliases': frozenset(normalized(alias) for alias in aliases),
            'text_sha256': value['text_sha256'], 'text_length': length,
            'parts': tuple(parts), 'sources': tuple(source['url'] for source in value['sources'])}


class LyricParts:
    def __init__(self, directory=None, resource_path=None):
        self.recordings = []
        self.errors = []
        paths = [Path(resource_path) if resource_path is not None else RESOURCE_PATH]
        if directory is not None:
            paths.extend(Path(directory) / name for name in ('lyric_parts.json','wiki_lyric_parts.json'))
        for path in paths:
            if not path.is_file():continue
            try:
                with path.open('rb') as stream:body = stream.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:raise ValueError('catalog size')
                value = json.loads(body)
                if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                        or value['schema_version'] != 1 or not isinstance(value.get('recordings'), list)
                        or len(value['recordings']) > 5000):
                    raise ValueError('catalog metadata')
                for item in value['recordings']:
                    recording = _recording(item)
                    if recording is None:self.errors.append((path.name, 'invalid recording'))
                    else:self.recordings.append(recording)
            except (OSError, ValueError, TypeError, RecursionError) as error:
                self.errors.append((path.name, type(error).__name__))

    @staticmethod
    def _row_singers(start, end, parts, allow_partial=False):
        cursor, names, complete = start, [], True
        for begin, stop, singers in parts:
            if stop <= start:continue
            if begin >= end:break
            if begin > cursor:complete = False
            names.extend(singers)
            cursor = max(cursor, stop)
        # Partial results describe only the credited fragment; callers must
        # visibly distinguish them from a complete attribution of the row.
        complete = complete and start < end <= cursor
        return tuple(dict.fromkeys(names)) if complete or allow_partial else ()

    @staticmethod
    def _conflicting_claims(start, end, recordings):
        events = []
        for recording in recordings:
            for begin, stop, singers in recording['parts']:
                if stop <= start:continue
                if begin >= end:break
                names = frozenset(singers)
                events.extend(((max(start, begin), 1, names), (min(end, stop), -1, names)))
        active = Counter()
        previous = start
        for position, delta, names in sorted(events, key=lambda event: event[0]):
            if previous < position and len(active) > 1:return True
            active[names] += delta
            if not active[names]:del active[names]
            previous = position
        return False

    def match(self, playback, lines, artists=(), reference_lines=(), allow_partial=False):
        empty = tuple(() for _ in lines)
        if not self.recordings or not lines:return empty
        title = normalized(playback.title)
        names = {normalized(name) for name in (playback.artist, *artists)
                 if isinstance(name, str) and name.strip()}
        album = normalized(playback.album)
        candidates = [recording for recording in self.recordings
                      if title == recording['title'] and names & recording['artists']
                      # Only explicitly reviewed release names may share a
                      # profile. Never infer versions from album substrings.
                      and (not recording['album'] or album == recording['album']
                           or album in recording['album_aliases'])]
        if not candidates:return empty
        fingerprint, length = text_fingerprint(lines)
        exact = [recording for recording in candidates
                 if fingerprint == recording['text_sha256'] and length == recording['text_length']]
        if not exact and reference_lines:
            reference_hash, reference_length = text_fingerprint(reference_lines)
            references = [recording for recording in candidates
                          if reference_hash == recording['text_sha256']
                          and reference_length == recording['text_length']]
            alignment = (match_lyrics([line.original for line in reference_lines],
                                      [line.original for line in lines]) if references else None)
            if alignment:
                projection = alignment.source_to_target
                exact = [{**recording, 'parts': tuple(
                    (projection[start], projection[end], singers)
                    for start, end, singers in recording['parts']
                    if projection[start] < projection[end])} for recording in references]
        candidates = exact
        if not candidates:return empty
        result, position = [], 0
        for line in lines:
            end = position + len(normalized(line.original))
            # A complete claim cannot hide a contradictory claim covering
            # only part of the same row. Compare overlapping character spans.
            if self._conflicting_claims(position, end, candidates):
                result.append(())
                position = end
                continue
            choices = [self._row_singers(position, end, recording['parts'], allow_partial)
                       for recording in candidates]
            known = {frozenset(choice) for choice in choices if choice}
            # Missing coverage in another source does not negate an explicit
            # fact; differing positive claims do need further review.
            result.append(next((choice for choice in choices if choice), ()) if len(known) == 1 else ())
            position = end
        return tuple(result)
