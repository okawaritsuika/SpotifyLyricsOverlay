"""Conservatively align whole lyric documents while allowing a few typos."""
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from lyrics import normalized


MAX_FUZZY_CHARACTERS = 20_000
MIN_FUZZY_CHARACTERS = 100
MAX_EDITS = 8


def is_short_recording(title):
    """Recognize explicit length edits, never a word inside the song title."""
    title=unicodedata.normalize('NFKC',str(title)).strip()
    return bool(re.search(r'(?:\(|\[|\s[-–—]\s)\s*(?:(?:game|tv)\s*(?:size|ver\.?|version)|'
                          r'short\s*(?:size|ver\.?|version))\s*[\])]?$',title,re.I))


@dataclass(frozen=True)
class LyricMatch:
    edits: int
    source_to_target: tuple[int, ...]
    source_offsets: tuple[int, ...]
    target_offsets: tuple[int, ...]


def _offsets(rows):
    result = []
    offset = 0
    for row in rows:
        result.append(offset)
        offset += len(row)
    return tuple(result)


def _omits_rows(longer, shorter):
    if len(longer) <= len(shorter):
        return False
    index = 0
    for row in longer:
        if row == shorter[index]:
            index += 1
            if index == len(shorter):
                return True
    return False


def _align(source, target, budget):
    """Return one deterministic minimal edit path, using only a narrow band."""
    previous = {}
    traces = []
    for i in range(len(source) + 1):
        low = max(0, i - budget)
        high = min(len(target), i + budget)
        trace = bytearray(high - low + 1)
        current = {}
        for j in range(low, high + 1):
            cost, operation = budget + 1, 0
            if i == 0 and j == 0:
                cost = 0
            if i and j and j - 1 in previous:
                same = source[i - 1] == target[j - 1]
                cost = previous[j - 1] + (not same)
                operation = 1 if same else 2
            # Prefer diagonals on ties, then deletions, for repeated lyrics.
            if i and previous.get(j, budget + 1) + 1 < cost:
                cost, operation = previous[j] + 1, 3
            if j and current.get(j - 1, budget + 1) + 1 < cost:
                cost, operation = current[j - 1] + 1, 4
            if cost <= budget:
                current[j] = cost
                trace[j - low] = operation
        if not current:
            return None
        traces.append((low, trace))
        previous = current
    if len(target) not in previous:
        return None
    edits = previous[len(target)]
    operations = []
    i, j = len(source), len(target)
    while i or j:
        low, trace = traces[i]
        operation = trace[j - low]
        operations.append(operation)
        if operation in (1, 2, 3):
            i -= 1
        if operation in (1, 2, 4):
            j -= 1
    operations.reverse()
    return edits, operations


def match_lyrics(source, target):
    """Match normalized lyrics in order and project source character boundaries.

    Exact concatenations allow arbitrary row splits/merges. Approximate matches
    allow at most 1% edits (eight total), no edit run longer than two characters,
    and at most 20% edits in any row on either side. Insertions and deletions must
    be inside rows, so a row onset cannot disappear into an accepted typo.

    Insertions project the source boundary after the inserted target characters;
    deletions collapse source boundaries. No timing is generated here.
    """
    source_rows = tuple(normalized(row) for row in source)
    target_rows = tuple(normalized(row) for row in target)
    if (not source_rows or not target_rows
            or not all(source_rows) or not all(target_rows)):
        return None
    source_text, target_text = ''.join(source_rows), ''.join(target_rows)
    source_offsets, target_offsets = _offsets(source_rows), _offsets(target_rows)
    if source_text == target_text:
        return LyricMatch(0, tuple(range(len(source_text) + 1)),
                          source_offsets, target_offsets)
    # Repeated characters can otherwise conceal a missing short row by moving
    # its edit into a neighboring long row in an equally good character path.
    if _omits_rows(source_rows, target_rows) or _omits_rows(target_rows, source_rows):
        return None
    size = max(len(source_text), len(target_text))
    if min(len(source_text), len(target_text)) < MIN_FUZZY_CHARACTERS:
        return None
    if size > MAX_FUZZY_CHARACTERS:
        return None
    budget = min(MAX_EDITS, size // 100)
    if abs(len(source_text) - len(target_text)) > budget:
        return None
    aligned = _align(source_text, target_text, budget)
    if aligned is None:
        return None
    edits, operations = aligned
    source_changes, target_changes = [0] * len(source_rows), [0] * len(target_rows)
    source_boundaries = set(source_offsets) | {len(source_text)}
    target_boundaries = set(target_offsets) | {len(target_text)}
    projection = [0] * (len(source_text) + 1)
    i = j = run = 0
    for operation in operations:
        if operation == 1:
            run = 0
        else:
            run += 1
            if run > 2:
                return None
            if operation == 3 and j in target_boundaries:
                return None
            if operation == 4 and i in source_boundaries:
                return None
            source_row = bisect_right(source_offsets, i) - 1
            target_row = bisect_right(target_offsets, j) - 1
            source_changes[source_row] += 1
            target_changes[target_row] += 1
        if operation in (1, 2, 3):
            i += 1
        if operation in (1, 2, 4):
            j += 1
        projection[i] = j
    if any(changes * 5 > len(row) for changes, row in zip(source_changes, source_rows)):
        return None
    if any(changes * 5 > len(row) for changes, row in zip(target_changes, target_rows)):
        return None
    if 3 in operations or 4 in operations:
        # A gap within repeated characters can slide away from an omitted tiny
        # row. Such rows have no typo allowance; retain their complete spans on
        # both sides whenever the alignment changes character offsets.
        source_spans = {(projection[start], projection[start + len(row)], row)
                        for start, row in zip(source_offsets, source_rows)}
        target_spans = {(start, start + len(row), row)
                        for start, row in zip(target_offsets, target_rows)}
        if any(len(row) < 5 and (start, end, row) not in target_spans
               for start, end, row in source_spans):
            return None
        if any(len(row) < 5 and (start, end, row) not in source_spans
               for start, end, row in target_spans):
            return None
    return LyricMatch(edits, tuple(projection), source_offsets, target_offsets)


_SPEAKER = re.compile(r'^\s*(?:[【\[][^】\]\n]{1,30}[】\]]\s*|[^:「『\n]{1,30}:\s*(?=[「『]))')


def phrase_key(text):
    """Normalize a comparison copy, including explicit speaker/part labels."""
    text = _SPEAKER.sub('', unicodedata.normalize('NFKC', text))
    return normalized(''.join(chr(ord(char) - 0x60) if 'ァ' <= char <= 'ヶ' else char
                              for char in text))


@dataclass(frozen=True)
class PhraseBlock:
    left: int
    right: int
    left_end: int
    right_end: int


def align_phrases(left, right):
    """Return only unambiguous ordered phrase blocks, with exclusive ends."""
    return _phrase_blocks(tuple(map(phrase_key, left)), tuple(map(phrase_key, right)))


@lru_cache(maxsize=96)
def _phrase_blocks(left, right):
    # Whole exact text gives all shared boundaries even for large documents.
    if left and right and all(left) and all(right) and ''.join(left) == ''.join(right):
        left_offsets, right_offsets = _offsets(left), _offsets(right)
        end = sum(map(len, left))
        lstarts = {stamp: i for i, stamp in enumerate((*left_offsets, end))}
        rstarts = {stamp: j for j, stamp in enumerate((*right_offsets, end))}
        bounds = sorted(lstarts.keys() & rstarts.keys())
        return tuple(PhraseBlock(lstarts[a], rstarts[a], lstarts[b], rstarts[b])
                     for a, b in zip(bounds, bounds[1:]))

    n, m = len(left), len(right)
    if not n or not m:
        return ()
    if n * m > 300_000:
        left_counts, right_counts = Counter(left), Counter(right)
        unique_right = {key: j for j, key in enumerate(right)
                        if len(key) >= 4 and right_counts[key] == left_counts[key] == 1}
        anchors = [(i, unique_right[key]) for i, key in enumerate(left) if key in unique_right]
        if (len(anchors) < min(n, m) // 3 or
                any(a[1] >= b[1] for a, b in zip(anchors, anchors[1:]))):
            return ()
        result = []
        left_start = right_start = 0
        for i, j in (*anchors, (n, m)):
            result.extend(PhraseBlock(block.left + left_start, block.right + right_start,
                                      block.left_end + left_start, block.right_end + right_start)
                          for block in _phrase_blocks(left[left_start:i], right[right_start:j]))
            if i < n:
                result.append(PhraseBlock(i, j, i + 1, j + 1))
            left_start, right_start = i + 1, j + 1
        return tuple(result)

    def spans(rows):
        result = []
        for i in range(len(rows)):
            prefixes, key = {}, ''
            for j in range(i, min(len(rows), i + 4)):
                if not rows[j]:
                    break
                key += rows[j]
                prefixes[key] = j + 1
            result.append(prefixes)
        return result

    left_spans, right_spans = spans(left), spans(right)
    # Among blocks with a shared start, the shortest exact shared boundary
    # always dominates longer blocks that contain that same boundary.
    edges = {}
    right_starts = {}
    for j, prefixes in enumerate(right_spans):
        for key, end in prefixes.items():
            right_starts.setdefault(key, []).append((j, end))
    for i, prefixes in enumerate(left_spans):
        for key, end in prefixes.items():
            for j, stop in right_starts.get(key, ()):
                edges.setdefault((i, j), (end, stop, len(key)))

    scores = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            score = max(scores[i + 1][j], scores[i][j + 1])
            edge = edges.get((i, j))
            if edge:
                end, stop, weight = edge
                score = max(score, weight + scores[end][stop])
            scores[i][j] = score

    # Walk the optimal-path graph. An onset/annotation is safe only when
    # every optimal path uses the same correspondence for each affected row.
    reachable = [bytearray(m + 1) for _ in range(n + 1)]
    reachable[0][0] = 1
    left_choices = [set() for _ in left]
    right_choices = [set() for _ in right]
    for i in range(n + 1):
        for j in range(m + 1):
            if not reachable[i][j]:
                continue
            score = scores[i][j]
            if i < n and score == scores[i + 1][j]:
                reachable[i + 1][j] = 1
                left_choices[i].add(None)
            if j < m and score == scores[i][j + 1]:
                reachable[i][j + 1] = 1
                right_choices[j].add(None)
            edge = edges.get((i, j))
            if edge:
                end, stop, weight = edge
                if score == weight + scores[end][stop]:
                    reachable[end][stop] = 1
                    block = PhraseBlock(i, j, end, stop)
                    for k in range(i, end):
                        left_choices[k].add(block)
                    for k in range(j, stop):
                        right_choices[k].add(block)
    return tuple(sorted({next(iter(choices)) for choices in left_choices if len(choices) == 1
                         and None not in choices
                         and all(len(right_choices[j]) == 1
                                 for block in choices for j in range(block.right, block.right_end))},
                        key=lambda block: block.left))
