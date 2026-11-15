"""Local RomajiASR logits and ordered CTC alignment of supplied lyric lines."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np


SAMPLE_RATE = 16_000
# The released HuBERT front end has unpadded convolution kernels 10,3,3,3,3,2,2
# and strides 5,2,2,2,2,2,2. Each output observes 400 samples on a 320-sample grid.
FRAME_HOP = 320
RECEPTIVE_FIELD = 400
FRAME_MS = 20
_CONTEXT_FRAMES = 75
_CORE_FRAMES = 450
_MAX_CANDIDATES = 72
_CANDIDATE_SEPARATION = 10
_AMBIGUITY_SEPARATION = 30
_MIN_GLOBAL_MARGIN = 0.22
_CANCEL = Callable[[], bool] | None


class AlignmentCancelled(RuntimeError):
    pass


def _check_cancelled(cancelled: _CANCEL) -> None:
    if cancelled is not None and cancelled():
        raise AlignmentCancelled("RomajiASR alignment cancelled")


@dataclass(frozen=True)
class Emissions:
    log_probs: np.ndarray
    vocabulary: Mapping[str, int]
    blank_id: int
    frame_ms: int = FRAME_MS


@dataclass(frozen=True)
class LineAlignment:
    start_ms: int
    end_ms: int
    score: float
    margin: float
    ambiguous: bool
    phone_coverage: float = 1.0
    first_token: int = 0
    recovered: bool = False


def _vocabulary(directory: Path) -> dict[str, int]:
    vocabulary = json.loads((directory / "phoneme_vocab.json").read_text(encoding="utf-8-sig"))
    if (not isinstance(vocabulary, dict) or "<blank>" not in vocabulary
            or any(type(value) is not int for value in vocabulary.values())
            or sorted(vocabulary.values()) != list(range(len(vocabulary)))):
        raise ValueError("Invalid RomajiASR phoneme_vocab.json")
    return vocabulary


def convert_release_logits(model_dir: str | Path) -> Path:
    """Expose the original LM head before the release's terminal ArgMax."""
    import onnx

    directory = Path(model_dir)
    vocabulary = _vocabulary(directory)
    model = onnx.load(str(directory / "model.onnx"))
    graph = model.graph
    if len(graph.output) != 1 or graph.output[0].name != "pred_ids" or not graph.node:
        raise ValueError("Expected the RomajiASR v1.0.0 pred_ids ONNX release")
    argmax = graph.node[-1]
    attributes = {item.name: item.i for item in argmax.attribute}
    if (argmax.op_type != "ArgMax" or list(argmax.output) != ["pred_ids"]
            or attributes.get("axis") != -1 or attributes.get("keepdims") != 0
            or len(argmax.input) != 1):
        raise ValueError("The RomajiASR graph does not end in a CTC ArgMax")
    logits = argmax.input[0]
    head = next((node for node in graph.node if logits in node.output), None)
    if head is None or head.op_type != "Add":
        raise ValueError("Expected a linear CTC output head before ArgMax")
    bias = next((item for item in graph.initializer
                 if item.name in head.input and "lm_head.bias" in item.name), None)
    if (bias is None or list(bias.dims) != [len(vocabulary)]
            or bias.data_type not in (onnx.TensorProto.FLOAT16, onnx.TensorProto.FLOAT)):
        raise ValueError("RomajiASR CTC output head and vocabulary differ")
    del graph.node[-1]
    del graph.output[:]
    graph.output.append(onnx.helper.make_tensor_value_info(
        logits, bias.data_type, ["batch_size", "num_frames", len(vocabulary)]))
    onnx.checker.check_model(model)
    destination = directory / "model-logits.onnx"
    temporary = destination.with_suffix(".onnx.tmp")
    try:
        onnx.save(model, str(temporary))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class _RomajiASR:
    def __init__(self, directory: Path, device: str):
        import onnxruntime as ort

        self.vocabulary = _vocabulary(directory)
        self.blank_id = self.vocabulary["<blank>"]
        logits_path = directory / "model-logits.onnx"
        self._path = logits_path if logits_path.is_file() else directory / "model.onnx"
        if not self._path.is_file():
            raise FileNotFoundError(f"RomajiASR ONNX model missing from {directory}")
        choices = {"auto": ("CUDAExecutionProvider", "DmlExecutionProvider"),
                   "gpu": ("CUDAExecutionProvider", "DmlExecutionProvider"),
                   "cuda": ("CUDAExecutionProvider",), "dml": ("DmlExecutionProvider",),
                   "cpu": ()}
        if device not in choices:
            raise ValueError(f"Unknown RomajiASR device {device!r}")
        available = ort.get_available_providers()
        provider = next((item for item in choices[device] if item in available),
                        "CPUExecutionProvider")
        self._ort = ort
        self.fallback_reason = None
        if provider == "CPUExecutionProvider" and device not in ("auto", "cpu"):
            self.fallback_reason = f"RomajiASR {device} provider unavailable"
        try:
            self._open_session(provider)
        except Exception as exc:
            if provider == "CPUExecutionProvider":
                raise
            self.fallback_reason = str(exc)
            self._open_session("CPUExecutionProvider")

    def _open_session(self, provider: str) -> None:
        ort = self._ort
        if provider == "CUDAExecutionProvider":
            from audio_runtime import prepare_cuda_dlls

            prepare_cuda_dlls()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = provider != "DmlExecutionProvider"
        providers = [provider] if provider == "CPUExecutionProvider" else [provider, "CPUExecutionProvider"]
        session = ort.InferenceSession(str(self._path), sess_options=options, providers=providers)
        session.disable_fallback()
        self.provider = session.get_providers()[0]
        if self.provider != provider:
            self.fallback_reason = f"ONNX Runtime selected {self.provider} instead of {provider}"
        self._inputs = {item.name: item for item in session.get_inputs()}
        output = session.get_outputs()[0]
        if len(output.shape) != 3 or output.type not in ("tensor(float16)", "tensor(float)"):
            raise ValueError("RomajiASR requires CTC logits. Convert pred_ids with python -m romaji_ctc MODEL_DIR")
        if isinstance(output.shape[-1], int) and output.shape[-1] != len(self.vocabulary):
            raise ValueError("RomajiASR logits and phoneme vocabulary differ")
        audio_input = self._inputs.get("input_values")
        if (audio_input is None or len(audio_input.shape) != 2
                or audio_input.type not in ("tensor(float16)", "tensor(float)")
                or isinstance(audio_input.shape[0], int) and audio_input.shape[0] != 1
                or self._inputs.keys() - {"input_values", "attention_mask"}):
            raise ValueError("Unexpected RomajiASR ONNX input signature")
        self._fixed_samples = audio_input.shape[1] if isinstance(audio_input.shape[1], int) else None
        self._output = output.name
        self._session = session

    def supports_phones(self, phones: Sequence[str]) -> bool:
        return bool(_targets(phones, self.vocabulary))

    def emissions(self, audio_16k: np.ndarray, *, cancelled: _CANCEL = None) -> Emissions:
        audio = np.asarray(audio_16k, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError("RomajiASR expects mono 16 kHz audio")
        frame_count = max(0, (len(audio) - RECEPTIVE_FIELD) // FRAME_HOP + 1)
        result = np.empty((frame_count, len(self.vocabulary)), dtype=np.float32)
        window_frames = ((self._fixed_samples - RECEPTIVE_FIELD) // FRAME_HOP + 1
                         if self._fixed_samples else _CORE_FRAMES + 2 * _CONTEXT_FRAMES)
        if window_frames < 1:
            raise ValueError("RomajiASR input window is shorter than its convolution receptive field")
        context = min(_CONTEXT_FRAMES, window_frames // 4)
        core = min(_CORE_FRAMES, window_frames - 2 * context)
        for first in range(0, frame_count, core):
            _check_cancelled(cancelled)
            last = min(frame_count, first + core)
            window_first = max(0, first - context)
            window_last = min(frame_count, last + context)
            clip = audio[window_first * FRAME_HOP:(window_last - 1) * FRAME_HOP + RECEPTIVE_FIELD]
            try:
                logits = self._infer(clip)
            except Exception as exc:
                if self.provider == "CPUExecutionProvider":
                    raise
                self.fallback_reason = str(exc)
                self._open_session("CPUExecutionProvider")
                logits = self._infer(clip)
            expected = window_frames if self._fixed_samples else window_last - window_first
            if logits.shape != (1, expected, len(self.vocabulary)):
                raise ValueError(f"Unexpected RomajiASR output shape {logits.shape}")
            log_probs = logits[0, first - window_first:last - window_first].astype(np.float32)
            if not np.isfinite(log_probs).all():
                raise ValueError("Nonfinite RomajiASR logits")
            log_probs -= log_probs.max(axis=1, keepdims=True)
            log_probs -= np.log(np.exp(log_probs).sum(axis=1, keepdims=True))
            result[first:last] = log_probs
        return Emissions(result, self.vocabulary, self.blank_id)

    def _infer(self, audio: np.ndarray) -> np.ndarray:
        input_type = self._inputs["input_values"].type
        input_dtype = np.float16 if input_type == "tensor(float16)" else np.float32
        samples = self._fixed_samples or len(audio)
        values = np.zeros((1, samples), dtype=input_dtype)
        values[0, :len(audio)] = audio
        feeds = {"input_values": values}
        mask_input = self._inputs.get("attention_mask")
        if mask_input is not None:
            mask_dtype = np.int64 if mask_input.type == "tensor(int64)" else np.int32
            mask = np.zeros((1, samples), dtype=mask_dtype)
            mask[0, :len(audio)] = 1
            feeds["attention_mask"] = mask
        return self._session.run([self._output], feeds)[0]


@lru_cache(maxsize=1)
def _load_model(directory: Path, device: str, revision: tuple) -> _RomajiASR:
    return _RomajiASR(directory, device)


def load_model(model_dir: str | Path, device: str = "cpu") -> _RomajiASR:
    directory = Path(model_dir).resolve()
    revision = []
    for name in ("model-logits.onnx", "model.onnx", "phoneme_vocab.json"):
        try:
            info = (directory / name).stat()
            revision.append((name, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            continue
    return _load_model(directory, device.lower(), tuple(revision))


def release_model() -> None:
    """Allow the disposable worker to free the ONNX session before a fallback."""
    _load_model.cache_clear()


_ALIASES = {"q": ("cl",), "hu": ("fu",), "fu": ("hu",), "si": ("shi",),
            "zi": ("ji",), "tu": ("tsu",), "ti": ("chi",), "wo": ("o",)}
_VOWELS = frozenset("aiueo")


def _targets(phones: Sequence[str], vocabulary: Mapping[str, int], pronunciation: bool = False
             ) -> list[list[tuple[int, int, float]]]:
    """Return arcs (start position, token ID, cost) grouped by target end position."""
    units: list[str] = []
    optional_length: set[int] = set()
    for phone in phones:
        if len(phone) == 2 and phone[0] in _VOWELS and phone[1] == ":":
            units.extend(phone[0] * 2)
            optional_length.add(len(units) - 1)
        else:
            units.append(phone)
    arcs: list[list[tuple[int, int, float]]] = [[] for _ in range(len(units) + 1)]
    reachable = {0}
    for start, phone in enumerate(units):
        if start not in reachable:
            continue
        forms = [(start + 1, phone)]
        if start + 1 < len(units) and phone not in (*_VOWELS, "cl", "q", "N") and units[start + 1] in _VOWELS:
            forms.append((start + 2, phone + units[start + 1]))
        for end, form in forms:
            labels = [(form, 0.0), *((label, .16) for label in _ALIASES.get(form, ()))]
            for label, cost in labels:
                if label.startswith("<") or label in ("SOS", "EOS") or label not in vocabulary:
                    continue
                token = vocabulary[label]
                arcs[end].append((start, token, cost))
                reachable.add(end)
                if end in optional_length:
                    arcs[end + 1].append((start, token, cost + 0.12))
                    reachable.add(end + 1)
                if (pronunciation and end < len(units) and end not in optional_length
                        and units[end] in _VOWELS and form.endswith(units[end])):
                    arcs[end + 1].append((start, token, cost + .40))
                    reachable.add(end + 1)
    return arcs if units and len(units) in reachable else []


@dataclass
class _State:
    token: int
    score: np.ndarray
    start: np.ndarray
    entry: np.ndarray
    predecessor: np.ndarray


def _state(token: int, emission: np.ndarray, previous: Sequence[int], states: list[_State],
           frames: np.ndarray, allowed_start: np.ndarray | None = None, cost: float = 0.0) -> int:
    """Viterbi CTC transition and self-loop, scanning the full timeline at once."""
    incoming = np.full(len(frames), -np.inf, dtype=np.float32)
    start = np.zeros(len(frames), dtype=np.int32)
    predecessor = np.full(len(frames), -1, dtype=np.int32)
    if allowed_start is not None:
        incoming[allowed_start] = 0.0
        start[allowed_start] = frames[allowed_start]
    for index in previous:
        state = states[index]
        better = state.score[:-1] > incoming[1:]
        incoming[1:][better] = state.score[:-1][better]
        start[1:][better] = state.start[:-1][better]
        predecessor[1:][better] = index
    prefix = np.cumsum(emission, dtype=np.float64)
    entry_score = incoming + emission - cost - prefix
    best = np.maximum.accumulate(entry_score)
    changed = np.r_[True, entry_score[1:] > best[:-1]]
    entry = np.maximum.accumulate(np.where(changed, frames, 0))
    states.append(_State(token, (best + prefix).astype(np.float32), start[entry],
                         entry, predecessor[entry]))
    return len(states) - 1


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    score: float
    weight: float
    phone_coverage: float
    first_token: int
    phone_path: tuple = ()


def _phone_evidence(states: Sequence[_State], index: int, frame: int,
                    emissions: Emissions, relaxed: bool, trace: bool, local_retry: bool):
    # Sparse substitutions and devoicing occur in sung lyrics. Keep the onset
    # supported and limit both the total and the consecutive weak token counts.
    total = weak = consecutive_weak = 0
    strength = -np.inf
    first_token = 0
    path = []
    while index >= 0:
        state = states[index]
        entry = int(state.entry[frame])
        if state.token != emissions.blank_id:
            first_token = state.token
            strength = float(np.max(emissions.log_probs[entry:frame + 1, state.token]))
            if trace:
                path.append((state.token, entry, frame + 1, round(strength, 3)))
            total += 1
            if strength < -3.5:
                weak += 1
                consecutive_weak += 1
                if consecutive_weak > (4 if local_retry else 3):
                    return None
            else:
                consecutive_weak = 0
        index = int(state.predecessor[frame])
        frame = entry - 1
    weak_limit = .40 if relaxed or local_retry else .45
    if strength >= -3.5 and weak <= int(total * weak_limit):
        return (total - weak) / total, first_token, tuple(reversed(path))
    return None


def _line_candidates(emissions: Emissions, phones: Sequence[str], bound: tuple[int, int] | None,
                     cancelled: _CANCEL, pronunciation: bool, relaxed: bool,
                     trace: bool, local_retry: bool = False) -> list[_Candidate]:
    arcs = _targets(phones, emissions.vocabulary, pronunciation)
    if not arcs or len(emissions.log_probs) == 0:
        return []
    log_probs = emissions.log_probs
    blank = emissions.blank_id
    frames = np.arange(len(log_probs), dtype=np.int32)
    allowed = np.ones(len(frames), dtype=np.bool_) if bound is None else (
        (frames * emissions.frame_ms >= bound[0]) & (frames * emissions.frame_ms <= bound[1]))
    states: list[_State] = []
    endings: list[list[int]] = [[] for _ in arcs]
    blanks: list[int | None] = [None] * len(arcs)
    for end, options in enumerate(arcs[1:], 1):
        _check_cancelled(cancelled)
        for start, token, cost in options:
            previous = [index for index in endings[start] if states[index].token != token]
            if blanks[start] is not None:
                previous.append(blanks[start])
            emission = log_probs[:, token]
            allowed_start = allowed if start == 0 else None
            if relaxed or local_retry:
                if allowed_start is not None:
                    allowed_start = allowed_start & (emission >= -3.5)
                emission = np.maximum(emission, -5.)
            index = _state(token, emission, previous, states, frames, allowed_start, cost)
            endings[end].append(index)
        if options and end + 1 < len(arcs):
            gaps = np.maximum(log_probs[:, blank], -1.2) if relaxed or local_retry else log_probs[:, blank]
            blanks[end] = _state(blank, gaps - 0.035, endings[end], states, frames)

    # Log-likelihood thresholds are acoustic heuristics and need song-level validation.
    minimum = -0.6 if len(phones) <= 2 else -2.25
    max_span = int(min(28.0, max(8.0, len(phones) * 0.60)) * 1000 / emissions.frame_ms)
    options = []
    for index in endings[-1]:
        state = states[index]
        quality = state.score / len(phones)
        valid = ((quality > minimum) & (frames - state.start < max_span)
                 & (log_probs[:, state.token] >= -4.2))
        for frame in np.flatnonzero(valid):
            options.append((float(quality[frame]), int(state.start[frame]), int(frame), index))
    options.sort(key=lambda item: item[0], reverse=True)
    candidates: list[_Candidate] = []
    used_starts: set[int] = set()
    for score, start, end, index in options:
        if start in used_starts:
            continue
        evidence = _phone_evidence(states, index, end, emissions, relaxed, trace, local_retry)
        if evidence is None:
            continue
        used_starts.update(range(start - _CANDIDATE_SEPARATION, start + _CANDIDATE_SEPARATION + 1))
        # A line can be skipped freely. Reward credible placements and prefer lines
        # with more phonetic evidence over a coincidental match of a very short line.
        weight = (score - minimum) * min(3.0, math.sqrt(len(phones)) / 2.0)
        candidates.append(_Candidate(start, end + 1, score, weight, *evidence))
        if len(candidates) == _MAX_CANDIDATES:
            break
    return candidates


def align_lines(emissions: Emissions, line_phones: Sequence[Sequence[str]], *,
                search_bounds: Sequence[tuple[int, int] | None] | None = None,
                cancelled: _CANCEL = None, pronunciation: bool = False,
                relaxed: bool = False, diagnostics: list | None = None,
                local_retry: bool = False) -> list[LineAlignment | None]:
    """Select non-overlapping placements in lyric order, permitting unmatched lines."""
    if search_bounds is not None and len(search_bounds) != len(line_phones):
        raise ValueError("RomajiASR search bounds must match the lyric line count")
    options = []
    by_target: dict[tuple[tuple[str, ...], tuple[int, int] | None], list[_Candidate]] = {}
    for index, phones in enumerate(line_phones):
        _check_cancelled(cancelled)
        bound = search_bounds[index] if search_bounds is not None else None
        key = (tuple(phones), bound)
        if key not in by_target:
            by_target[key] = _line_candidates(emissions, phones, bound, cancelled, pronunciation, relaxed,
                                              diagnostics is not None, local_retry)
        options.append(by_target[key])

    # Forward value at t uses only placements ending at/before t. Backward value
    # at t uses only placements starting at/after t. Gaps cost nothing across lines.
    num_frames = len(emissions.log_probs)
    forward = np.zeros((len(options) + 1, num_frames + 1), dtype=np.float32)
    for line, candidates in enumerate(options):
        prior = forward[line]
        row = forward[line + 1]
        row[:] = prior
        for candidate in candidates:
            row[candidate.end] = max(row[candidate.end], prior[candidate.start] + candidate.weight)
        np.maximum.accumulate(row, out=row)

    chosen: list[_Candidate | None] = [None] * len(options)
    end = num_frames
    for line in range(len(options) - 1, -1, -1):
        prior = forward[line]
        best = max((candidate for candidate in options[line] if candidate.end <= end),
                   key=lambda item: prior[item.start] + item.weight, default=None)
        if best is not None and prior[best.start] + best.weight > prior[end] + 1e-5:
            chosen[line] = best
            end = best.start

    result: list[LineAlignment | None] = [None] * len(options)
    if diagnostics is not None:
        labels = {token: label for label, token in emissions.vocabulary.items()}
        for selected, candidates in zip(chosen, options):
            diagnostics.append({
                'alternatives_ms': [[row.start * emissions.frame_ms, row.end * emissions.frame_ms,
                                     round(row.score, 4)] for row in candidates[:5]],
                'phone_path_ms': [[labels[token], begin * emissions.frame_ms, end * emissions.frame_ms, strength]
                                  for token, begin, end, strength in selected.phone_path] if selected else [],
            })
    suffix = np.zeros(num_frames + 1, dtype=np.float32)
    for line in range(len(options) - 1, -1, -1):
        _check_cancelled(cancelled)
        prior = forward[line]
        selected = chosen[line]
        if selected is not None:
            score = prior[selected.start] + selected.weight + suffix[selected.end]
            other = float(np.max(prior + suffix))  # Best path leaving this line unmatched.
            for candidate in options[line]:
                if abs(candidate.start - selected.start) >= _AMBIGUITY_SEPARATION:
                    other = max(other, prior[candidate.start] + candidate.weight + suffix[candidate.end])
            margin = max(0.0, float(score - other))
            result[line] = LineAlignment(selected.start * emissions.frame_ms,
                                         selected.end * emissions.frame_ms, selected.score,
                                         margin, margin < _MIN_GLOBAL_MARGIN,
                                         selected.phone_coverage, selected.first_token)
        row = suffix.copy()
        for candidate in options[line]:
            row[candidate.start] = max(row[candidate.start], candidate.weight + suffix[candidate.end])
        np.maximum.accumulate(row[::-1], out=row[::-1])
        suffix = row
    return result


def _missing_gaps(alignment):
    anchors = [-1, *(i for i, row in enumerate(alignment) if row and not row.ambiguous), len(alignment)]
    return ((before, after) for before, after in zip(anchors, anchors[1:]) if after > before + 1)


def _gap_frames(emissions, alignment, before, after):
    hop = emissions.frame_ms
    start = alignment[before].end_ms // hop if before >= 0 else 0
    end = alignment[after].start_ms // hop if after < len(alignment) else len(emissions.log_probs)
    return start, end


def _gap_bounds(bounds, before, after, offset, length):
    return [(max(0, left - offset), min(length, right - offset)) for left, right in bounds[before + 1:after]]


def _place_recovery(row, offset):
    return replace(row, start_ms=row.start_ms + offset, end_ms=row.end_ms + offset, recovered=True)


def recover_gaps(emissions: Emissions, phones, alignment, bounds, *, relaxed=False):
    """Re-solve missing runs within the intervals left by existing CTC matches."""
    result = list(alignment)
    hop = emissions.frame_ms
    for before, after in _missing_gaps(alignment):
        start, end = _gap_frames(emissions, alignment, before, after)
        targets = phones[before + 1:after]
        if start >= end or not any(targets):
            continue
        # Don't broaden the acoustic search across long unanchored breaks or
        # short targets that can match unrelated syllables in a loose pass.
        if relaxed and (before < 0 and after == len(phones) or
                        (end - start) * hop > min(45000, 6000 + 9500 * len(targets))):
            continue
        if relaxed:
            targets = [target if len(target) >= 4 else () for target in targets]
        offset = start * hop
        clip_bounds = _gap_bounds(bounds, before, after, offset, (end - start) * hop)
        clip = replace(emissions, log_probs=emissions.log_probs[start:end])
        recovered = align_lines(clip, targets, search_bounds=clip_bounds, pronunciation=True, relaxed=relaxed)
        for index, row in enumerate(recovered, before + 1):
            if row is not None and not row.ambiguous:
                result[index] = _place_recovery(row, offset)
    return result


def recover_uncertain(emissions: Emissions, targets, alignment, bounds, *, min_coverage,
                      min_margin=0., min_score=-math.inf, local_retry=False, confirmed_window=False):
    """Propose review onsets inside short gaps with at least one CTC anchor."""
    result = list(alignment)
    hop = emissions.frame_ms
    for before, after in _missing_gaps(alignment):
        if before < 0 and after == len(alignment) and not confirmed_window:
            continue
        start, end = _gap_frames(emissions, alignment, before, after)
        length = (end - start) * hop
        gap_targets = targets[before + 1:after]
        if not 0 < length <= 15000 or not any(gap_targets):
            continue
        offset = start * hop
        clip = replace(emissions, log_probs=emissions.log_probs[start:end])
        clip_bounds = _gap_bounds(bounds, before, after, offset, length)
        candidates = align_lines(clip, gap_targets, search_bounds=clip_bounds,
                                 pronunciation=True, local_retry=local_retry)
        for index, (row, (left, right)) in enumerate(zip(candidates, clip_bounds), before + 1):
            if (row is not None and not row.ambiguous and row.phone_coverage >= min_coverage
                    and row.margin >= min_margin and row.score >= min_score
                    and left <= row.start_ms < row.end_ms <= right):
                result[index] = _place_recovery(row, offset)
    return result


def refine_onsets(emissions: Emissions, alignment, bounds):
    """Extend a first token over nearby, contiguous acoustic evidence."""
    result = list(alignment)
    previous_end = 0
    hop = emissions.frame_ms
    for index, row in enumerate(alignment):
        if row is None or row.ambiguous:
            continue
        start = row.start_ms // hop
        limit = max(previous_end, math.ceil(bounds[index][0] / hop), start - 5)
        token = row.first_token
        # A threshold relative to both the token peak and blank prevents a
        # drum or a low-level posterior tail from moving the acoustic onset.
        track = emissions.log_probs[:, token]
        threshold = max(-3.5, float(track[start]) - 2.0)
        while (start > limit and track[start - 1] >= threshold
               and track[start - 1] >= emissions.log_probs[start - 1, emissions.blank_id] - 1.0):
            start -= 1
        result[index] = replace(row, start_ms=start * hop)
        previous_end = row.end_ms // hop
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Expose logits from the RomajiASR v1.0.0 ONNX export")
    parser.add_argument("model_dir", type=Path)
    print(convert_release_logits(parser.parse_args().model_dir))
