"""Convert the supplied lyric lines to Japanese phones and retain text spans."""

from dataclasses import dataclass, replace
import re
from typing import Mapping, Sequence
import unicodedata


_KANA = re.compile(r'[ぁ-ゖァ-ヺー]+')
_MORAE = re.compile(r'.[ぁぃぅぇぉゃゅょゎ]?')
_PAUSES = {'pau', 'sil', 'sp'}
_VOWELS = {'A': 'a', 'I': 'i', 'U': 'u', 'E': 'e', 'O': 'o'}
_MORA_END = {*_VOWELS.values(), 'N', 'cl'}


@dataclass(frozen=True)
class PhoneSpan:
    """Map a half-open phone range to an original morpheme or override."""

    start: int
    end: int
    phone_start: int
    phone_end: int


@dataclass(frozen=True)
class PreparedLine:
    """Spans index the atomic phones, and the CTC sequence marks optional vowel length."""

    index: int
    text: str
    reading: str = ''
    phones: tuple[str, ...] = ()
    morae: tuple[str, ...] = ()
    spans: tuple[PhoneSpan, ...] = ()
    ctc_phones: tuple[str, ...] = ()
    error: str | None = None


def _apply_overrides(text, pattern, overrides):
    if pattern is None:
        return text, [(i, i + 1) for i in range(len(text))]

    pieces, source = [], []
    offset = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        pieces.append(text[offset:start])
        source.extend((i, i + 1) for i in range(offset, start))
        reading = overrides[match.group()]
        pieces.append(reading)
        source.extend([(start, end)] * len(reading))
        offset = end
    pieces.append(text[offset:])
    source.extend((i, i + 1) for i in range(offset, len(text)))
    return ''.join(pieces), source


def _hiragana(text):
    return ''.join(chr(ord(c) - 0x60) if 'ァ' <= c <= 'ヶ' else c for c in text)


def _phone_mora_boundaries(phones):
    return [0, *(index + 1 for index, phone in enumerate(phones) if phone in _MORA_END)]


def _ctc_phones(phones, morae, boundaries):
    if len(boundaries) != len(morae) + 1 or boundaries[-1] != len(phones):
        return phones
    result = []
    for mora, start, end in zip(morae, boundaries, boundaries[1:]):
        part = phones[start:end]
        if mora == 'ー' and len(part) == 1 and result and result[-1] == part[0]:
            result[-1] += ':'
        else:
            result.extend(part)
    return result


def _prepare_line(text, pattern, overrides, g2p_mapping):
    pronunciation, source = _apply_overrides(text, pattern, overrides)
    if not pronunciation.strip():
        raise ValueError('no sung text')
    phones, morae, spans, ctc_phones = [], [], [], []
    for word in g2p_mapping(pronunciation, normalize_mode='NFKC', predict_nani=False):
        start, end = word['char_span']
        written = pronunciation[start:end]
        normalized_written = unicodedata.normalize('NFKC', written).replace('・', '')
        if word['is_unknown'] and not _KANA.fullmatch(normalized_written):
            if any(char.isalnum() for char in written):
                raise ValueError(f'unknown reading: {written}')
        spoken = tuple(_VOWELS.get(phone, phone) for phone in word['phonemes'] if phone not in _PAUSES)
        if not spoken:
            if any(char.isalnum() for char in written):
                raise ValueError(f'unreadable text: {written}')
            continue
        if not 0 <= start < end <= len(source):
            raise ValueError('no original text span for phonemes')
        kana = word['pron'].replace('’', '')
        if not _KANA.fullmatch(kana):
            raise ValueError(f'invalid reading: {written}')
        kana = _hiragana(kana)
        matches = list(_MORAE.finditer(kana))
        word_morae = [match.group() for match in matches]
        boundaries = _phone_mora_boundaries(spoken)
        if _hiragana(written) == kana and len(boundaries) == len(matches) + 1 and boundaries[-1] == len(spoken):
            for match, first, last in zip(matches, boundaries, boundaries[1:]):
                spans.append(PhoneSpan(source[start + match.start()][0], source[start + match.end() - 1][1],
                                       len(phones) + first, len(phones) + last))
        else:
            spans.append(PhoneSpan(source[start][0], source[end - 1][1], len(phones), len(phones) + len(spoken)))
        morae.extend(word_morae)
        ctc_phones.extend(_ctc_phones(spoken, word_morae, boundaries))
        phones.extend(spoken)
    if not phones:
        raise ValueError('no sung phonemes')
    return ''.join(morae), tuple(phones), tuple(morae), tuple(spans), tuple(ctc_phones)


def prepare_lines(lines: Sequence[str], overrides: Mapping[str, str] | None = None) -> tuple[PreparedLine, ...]:
    """Return one entry per input row. Overrides map exact text to kana or an empty string."""
    import pyopenjtalk

    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in (overrides or {}).items()):
        raise ValueError('reading overrides require text keys and values')
    corrections = {key: unicodedata.normalize('NFKC', value) for key, value in (overrides or {}).items()}
    if any(not key or (value and not _KANA.fullmatch(value)) for key, value in corrections.items()):
        raise ValueError('reading overrides require nonempty source text and a kana or empty value')
    keys = sorted(corrections, key=len, reverse=True)
    pattern = re.compile('|'.join(map(re.escape, keys))) if keys else None
    readings = {}
    result = []
    for index, text in enumerate(lines):
        if text not in readings:
            try:
                readings[text] = PreparedLine(index, text, *_prepare_line(text, pattern, corrections, pyopenjtalk.g2p_mapping))
            except ValueError as error:
                readings[text] = PreparedLine(index, text, error=str(error))
        result.append(replace(readings[text], index=index))
    return tuple(result)


def prepare_supplied_readings(prepared, supplied, overrides=None):
    """Prefer a kana reading associated with the same original row."""
    if not isinstance(supplied, (list, tuple)) or len(supplied) != len(prepared):
        return prepared
    rows = []
    for row, hint in zip(prepared, supplied):
        reading = unicodedata.normalize('NFKC', hint) if isinstance(hint, str) else ''
        reading = ''.join(reading.split()).replace('・', '')
        if _KANA.fullmatch(reading) and not any(key in row.text for key in (overrides or {})):
            candidate = prepare_lines([row.text], {row.text: reading})[0]
            if not candidate.error:
                row = replace(candidate, index=row.index)
        rows.append(row)
    return tuple(rows)
