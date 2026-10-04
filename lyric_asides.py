"""Map recorded main-lyric onsets across explicitly parenthesized backing calls."""
from bisect import bisect_right
import unicodedata

from lyrics import normalized
from lyric_annotations import _parenthetical_spans, _reduced_annotation_rows
from lyric_match import _align, _offsets, _omits_rows


def aside_onsets(source, target):
    """Return exact main-row pairs after a bounded whole-document comparison.

    The caller must verify the complete recording title, artist and album.
    Parentheses may account for at most 20% of either document. The remaining
    lyric allows 2% / twelve edits, never more than two consecutive edits. Only
    exact main-row starts get a pair. Split/merged rows may share their first
    onset, but later splits get no invented time. A parenthetical prefix has
    no known main-vocal onset and is never paired.
    """
    source, target = tuple(source), tuple(target)
    reduced = []
    calls = []
    for rows in (source, target):
        length = sum(len(normalized(row)) for row in rows)
        if not 100 <= length <= 6000:
            return None
        spans = _parenthetical_spans(rows)
        if spans is None or any(end - start > 120 for start, end in spans):
            return None
        skipped = sum(end - start for start, end in spans)
        if skipped * 5 > length:
            return None
        document = ''.join(normalized(row) for row in rows)
        calls.append(sorted(document[start:end] for start, end in spans))
        reduced.append(_reduced_annotation_rows(rows, spans))
    # Both transcriptions must identify backing calls explicitly. A document
    # with additional calls and no corresponding convention in the reference
    # still uses the existing annotation-only fallback.
    if not all(calls) or calls[0] == calls[1]:
        return None
    (left, left_rows), (right, right_rows) = reduced
    if not left or not right or _omits_rows(left, right) or _omits_rows(right, left):
        return None
    first, second = ''.join(left), ''.join(right)
    if min(len(first), len(second)) < 100:
        return None
    budget = min(12, max(len(first), len(second)) // 50)
    aligned = _align(first, second, budget)
    if aligned is None:
        return None
    edits, operations = aligned
    left_starts, right_starts = _offsets(left), _offsets(right)
    left_boundaries = set(left_starts) | {len(first)}
    right_boundaries = set(right_starts) | {len(second)}
    left_changes, right_changes = [0] * len(left), [0] * len(right)
    projection = [0] * (len(first) + 1)
    i = j = run = 0
    for operation in operations:
        if operation == 1:
            run = 0
        else:
            run += 1
            if run > 2 or operation == 3 and j in right_boundaries or operation == 4 and i in left_boundaries:
                return None
            left_changes[bisect_right(left_starts, i) - 1] += 1
            right_changes[bisect_right(right_starts, j) - 1] += 1
        if operation in (1, 2, 3):
            i += 1
        if operation in (1, 2, 4):
            j += 1
        projection[i] = j
    for rows, changes in ((left, left_changes), (right, right_changes)):
        if any(count > max(2, len(row) // 5) for row, count in zip(rows, changes)):
            return None
    targets = {start: (index, row)
               for row, (index, start, end) in zip(right, right_rows)}
    pairs = []
    for row, (index, start, end) in zip(left, left_rows):
        found = targets.get(projection[start])
        if found is None or not (row.startswith(found[1]) or found[1].startswith(row)):
            continue
        other = found[0]
        if any(unicodedata.normalize('NFKC', text).lstrip().startswith('(')
               for text in (source[index], target[other])):
            continue
        pairs.append((index, other))
    if len(pairs) < 2:
        return None
    return edits, tuple(pairs)
