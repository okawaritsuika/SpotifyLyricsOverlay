"""Reuse library translations after identity and whole-text verification.

This module supplies annotations only. It never selects a saved recording,
returns its clock, changes a Song, or writes the library or playback cache.
"""
from functools import lru_cache
import re
import unicodedata

from artist_names import catalog, name_key
from lyric_annotations import match_verified_annotations
from lyrics import Line, normalized


def _artist_identity(value):
    value = unicodedata.normalize('NFKC', value).strip()
    # Only a trailing, explicitly identified voice-actor credit is decoration.
    value = re.sub(r'\s*\(\s*CV\s*[.:：]?[^()]+\)\s*$', '', value, flags=re.I).strip()
    if not value or name_key(value) in catalog().conflicts:return ''
    return name_key(catalog().translate(value))


@lru_cache(maxsize=32)
def _sequence_annotations(originals, supplied):
    matches = match_verified_annotations(originals, supplied)
    if matches is None:return (None,) * len(originals)
    return tuple(Line(original, extra.reading if normalized(original)==normalized(extra.original) else '',
                      extra.korean) if extra else None for original,extra in zip(originals,matches))


def match_saved_annotations(playback, lines, songs, display_lines=None):
    """Return safe library annotations in current row order, or empty rows.

    Title spelling and the complete named performer must match; the existing
    verified Japanese/Korean display catalog supplies aliases. Albums may use
    an imported short label, since the full lyric is independently verified.
    Repeated phrases follow the verified document sequence. Conflicting local
    translations stay unassigned instead of choosing an arbitrary document.
    ``display_lines`` optionally reads existing saved annotation overrides.
    """
    title = normalized(playback.title)
    artist = _artist_identity(playback.artist)
    result = [None] * len(lines)
    if not title or not artist or not lines:return tuple(result)
    originals = tuple(line.original for line in lines)
    candidates = []
    for song in songs:
        if normalized(song.title)!=title or _artist_identity(song.artist)!=artist:continue
        supplied = tuple(display_lines(song) if display_lines else song.lines)
        if any(line.korean.strip() for line in supplied):
            candidates.append(_sequence_annotations(originals, supplied))
    for index, original in enumerate(originals):
        choices = [rows[index] for rows in candidates if rows[index] is not None]
        readings = {row.reading.strip() for row in choices if row.reading.strip()}
        meanings = {row.korean.strip() for row in choices if row.korean.strip()}
        reading = next(iter(readings)) if len(readings)==1 else ''
        meaning = next(iter(meanings)) if len(meanings)==1 else ''
        if reading or meaning:result[index] = Line(original, reading, meaning)
    return tuple(result)
