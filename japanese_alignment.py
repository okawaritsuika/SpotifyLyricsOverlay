"""Opt-in Japanese lyric alignment in a disposable local audio worker."""
from contextlib import ExitStack
from pathlib import Path
import sys

from audio_alignment import normalize_ranges, uncovered_ranges
from recording_context import known_duration
from storage import validate_times

LEGACY = 'stable_ts_legacy'
CTC = 'romaji_ctc'
HYBRID = 'hybrid_japanese'
SAMPLE_RATE = 16000
ALIGNMENT_VERSION = 2
DEFAULT_PROFILE = 'retry'
PROFILES = ('baseline', 'readings', 'global', 'onset', 'retry', 'recovery')


class JapaneseRuntimeError(RuntimeError):
    """The optional alignment stack is unavailable for this recording."""


class _PyshiroRefinementError(RuntimeError):
    """The optional second pass failed after CTC candidates were computed."""


def alignment_options(settings, runtime):
    """Resolve paths in the UI before handing a JSON request to the worker."""
    if not isinstance(settings, dict) or settings.get('engine') not in (CTC, HYBRID):
        return {}
    runtime = Path(runtime)
    def local(value):
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else runtime / path)
    options = {
        'alignment_engine': settings['engine'],
        'romaji_model_dir': local(settings['model_dir'] if isinstance(settings.get('model_dir'), str)
                                  and settings['model_dir'] else 'models/romaji-asr'),
        'romaji_device': settings.get('device', 'cpu'),
        'romaji_realtime': settings.get('realtime') is True,
        'romaji_profile': settings.get('profile') if settings.get('profile') in PROFILES else (
            DEFAULT_PROFILE if settings['engine'] == CTC else 'baseline'),
    }
    if isinstance(settings.get('readings'), dict):
        options['japanese_readings'] = settings['readings']
    if settings['engine'] == HYBRID:
        pyshiro = settings.get('pyshiro')
        pyshiro = pyshiro if isinstance(pyshiro, dict) else {}
        defaults = {'python': 'models/japanese-alignment-venv/Scripts/python.exe',
                    'checkpoint': 'models/pyshiro/checkpoint/pyshiro-jp-v2.hsmm',
                    'phonemap': 'models/pyshiro/checkpoint/pyshiro-jp-v2_phonemap.json'}
        options['pyshiro'] = {key: local(pyshiro[key] if isinstance(pyshiro.get(key), str)
                                        and pyshiro[key] else value) for key, value in defaults.items()}
    return options


def prepare_ctc(request, device):
    try:
        from japanese_phonetics import prepare_lines, prepare_supplied_readings
        from romaji_ctc import load_model
        prepared = prepare_lines(request['lines'], request.get('japanese_readings'))
        if request.get('romaji_profile', 'baseline') != 'baseline':
            prepared = prepare_supplied_readings(prepared, request.get('line_readings'),
                                                 request.get('japanese_readings'))
        if device in ('auto', 'cuda'):
            from audio_runtime import prepare_cuda_dlls
            prepare_cuda_dlls()
        model = load_model(request['romaji_model_dir'], device)
    except Exception as error:
        raise JapaneseRuntimeError(f'RomajiASR 준비 실패 ({type(error).__name__})') from error
    return model, prepared


def ctc_emissions(model, samples):
    try:
        return model.emissions(samples)
    except Exception as error:
        raise JapaneseRuntimeError(f'RomajiASR 추론 실패 ({type(error).__name__})') from error


def release_ctc():
    module = sys.modules.get('romaji_ctc')
    if module is not None:
        module.release_model()


def review_ranges(draft, fingerprint, recording, engine, duration, *, profile=DEFAULT_PROFILE):
    """Resume only a compatible uncommitted draft, without using its onsets as anchors."""
    old_duration = known_duration(draft.get('duration'))
    if (engine not in (CTC, HYBRID) or draft.get('engine') != engine or not draft.get('review_only')
            or draft.get('alignment_version') != ALIGNMENT_VERSION or draft.get('romaji_profile') != profile
            or draft.get('fingerprint') != fingerprint or draft.get('recording_key') != recording
            or old_duration is None or abs(old_duration - duration) > 2):
        return []
    ranges = normalize_ranges(draft.get('analyzed_ranges', []), old_duration)
    return normalize_ranges([[start, min(end, duration)] for start, end in ranges if start < duration], duration)


def merge_review_results(previous, result, ranges):
    """Keep candidate rows from disjoint clips, leaving contradictory placements unresolved."""
    times = [value if value is None or value < result['duration'] else None for value in previous['times']]
    validate_times(times, len(result['times']))
    details = list(previous['line_alignment'])
    for index, stamp in enumerate(result['times']):
        if stamp is None:
            continue
        old = times[index]
        detail = result['line_alignment'][index]
        if old is not None:
            if abs(old - stamp) > .65:
                times[index] = None
                details[index] = {**detail, 'status': 'conflict', 'reason': 'different_recorded_occurrences',
                                  'start': None, 'end': None}
            continue
        before = max((value for value in times[:index] if value is not None), default=-1)
        after = min((value for value in times[index+1:] if value is not None), default=result['duration'])
        if before < stamp < after:
            times[index] = stamp
            details[index] = detail
        else:
            details[index] = {**detail, 'status': 'conflict', 'reason': 'recorded_occurrence_out_of_order',
                              'start': None, 'end': None}
    combined = normalize_ranges(ranges + result['analyzed_ranges'], result['duration'])
    matched = sum(stamp is not None for stamp in times)
    gaps = uncovered_ranges(0, result['duration'], combined)
    return {**result, 'times': times, 'line_alignment': details, 'analyzed_ranges': combined,
            'matched': matched, 'coverage': [float(value is not None) for value in times],
            'partial': matched < len(times) or any(end - start >= 2 for start, end in gaps)}


def recovery_targets(prepared, phones, overrides):
    """Try the written initial kana or a complete-mora prefix of a longer line."""
    mora_ends = {'a', 'i', 'u', 'e', 'o', 'a:', 'i:', 'u:', 'e:', 'o:', 'N', 'cl'}
    spellings, prefixes = [], []
    for row, target in zip(prepared, phones):
        text = row.text.lstrip()
        spelling = ()
        if not any(key in row.text for key in overrides):
            if text.startswith(('は', 'ハ')) and target[:2] == ('w', 'a'):
                spelling = ('h', *target[1:])
            elif text.startswith(('へ', 'ヘ')) and target[:1] == ('e',):
                spelling = ('h', *target)
        spellings.append(spelling)

        cut = len(target) - 1
        while cut > 0 and target[cut - 1] not in mora_ends:
            cut -= 1
        prefixes.append(target[:cut] if len(row.morae) >= 6 and cut >= 7 else ())
    return spellings, prefixes


def align_japanese(request, samples, start_position, progress, emit=None, *, infer=None):
    """Align original rows to one continuous clip in Spotify coordinates."""
    engine = request.get('alignment_engine', CTC)
    profile = request.get('romaji_profile', 'baseline')
    if profile not in PROFILES:
        raise ValueError('알 수 없는 RomajiASR 정렬 프로필입니다.')
    duration = request['duration']
    end = min(duration, start_position + len(samples) / SAMPLE_RATE)
    if not 0 <= start_position < end:
        raise ValueError('일본어 정렬의 수집 시각이 곡 범위를 벗어났습니다.')
    known = request.get('confirmed_times', [None] * len(request['lines']))
    validate_times(known, len(request['lines']))
    if any(value is not None and value >= duration for value in known):
        raise ValueError('저장된 가사 시각이 곡 길이를 벗어났습니다.')
    covered = normalize_ranges(request.get('analyzed_ranges', []), duration)
    ranges = uncovered_ranges(start_position, end, covered)
    device = request.get('romaji_device', request.get('final_device', 'cpu'))
    model, prepared = prepare_ctc(request, device)
    from romaji_ctc import align_lines, recover_gaps, recover_uncertain, refine_onsets
    infer = infer or ctc_emissions
    retry_cpu = False
    try:
        emissions = infer(model, samples)
    except JapaneseRuntimeError:
        if device == 'cpu':
            raise
        retry_cpu = True
    if retry_cpu:
        del model
        release_ctc()
        model, prepared = prepare_ctc(request, 'cpu')
        emissions = infer(model, samples)
    provider = getattr(model, 'provider', 'CPUExecutionProvider')
    runtime = {'model': 'romajiasr-v1.0.0', 'device': {
        'CUDAExecutionProvider': 'cuda', 'DmlExecutionProvider': 'dml'
    }.get(provider, 'cpu'), 'provider': provider}
    if getattr(model, 'fallback_reason', None):
        runtime.update(fallback=True, fallback_reason='romaji_provider_unavailable',
                       fallback_detail=str(model.fallback_reason))
    elif retry_cpu:
        runtime.update(fallback=True, fallback_reason='romaji_gpu_inference_failed')
    if emit:
        emit('progress', state='analyzing', runtime=runtime, text='RomajiASR 음소 정렬 중')
    progress(.35, 1)

    # Each previous/next saved onset bounds its neighbors. Saved rows are
    # carried by the timing store, so this result only contributes new rows.
    lower = start_position
    bounds = []
    for value in known:
        bounds.append([lower, end])
        if value is not None:
            lower = max(lower, value + .001)
    upper = end
    for index in reversed(range(len(known))):
        bounds[index][1] = upper
        if known[index] is not None:
            upper = min(upper, known[index] - .001)
    bounds_ms = [(int(round((left - start_position) * 1000)),
                  int(round((right - start_position) * 1000))) for left, right in bounds]
    supported = [bool(row.ctc_phones and model.supports_phones(row.ctc_phones)) for row in prepared]
    phones = [row.ctc_phones if supported[i] and known[i] is None and bounds[i][0] < bounds[i][1] else ()
              for i, row in enumerate(prepared)]
    trace = [] if request.get('ctc_diagnostics') else None
    options = {'diagnostics': trace} if trace is not None else {}
    confirmed_window = bool(known and known[0] is not None and known[-1] is not None)
    if profile != 'baseline':
        options['pronunciation'] = True
    aligned = align_lines(emissions, phones, search_bounds=bounds_ms, **options)
    if profile != 'baseline':
        if profile in ('global', 'onset', 'retry', 'recovery'):
            aligned = recover_gaps(emissions, phones, aligned, bounds_ms)
        if profile in ('retry', 'recovery'):
            aligned = recover_gaps(emissions, phones, aligned, bounds_ms, relaxed=True)
        if profile == 'recovery':
            spellings, prefixes = recovery_targets(prepared, phones, request.get('japanese_readings') or {})
            aligned = recover_uncertain(emissions, spellings, aligned, bounds_ms, min_coverage=.70,
                                        confirmed_window=confirmed_window)
            aligned = recover_uncertain(emissions, prefixes, aligned, bounds_ms, min_coverage=.85,
                                        min_margin=.50, min_score=-1.3, confirmed_window=confirmed_window)
        if profile in ('onset', 'retry', 'recovery'):
            aligned = refine_onsets(emissions, aligned, bounds_ms)
        if profile == 'recovery':
            targets = [target if len(target) >= 6 else () for target in phones]
            aligned = recover_uncertain(emissions, targets, aligned, bounds_ms, min_coverage=.70,
                                        min_margin=.40, min_score=-1.8, local_retry=True,
                                        confirmed_window=confirmed_window)
    progress(.65, 1)

    def collect_rows(refiner, report):
        times = [None] * len(prepared)
        coverage = [0.0] * len(prepared)
        details = []
        previous = -1.0
        last_candidate = 0
        next_candidates = [len(samples) * 1000 // SAMPLE_RATE] * len(aligned)
        next_candidate = next_candidates[-1] if next_candidates else 0
        for index in reversed(range(len(aligned))):
            next_candidates[index] = next_candidate
            candidate = aligned[index]
            if candidate is not None and not candidate.ambiguous:
                next_candidate = candidate.start_ms
        for index, (row, candidate) in enumerate(zip(prepared, aligned)):
            detail = {'index': index, 'status': 'missing', 'estimated': False, 'engine': CTC,
                      'start': None, 'end': None, 'acoustic_score': None, 'margin': None,
                      'quality': 'low', 'reading': row.reading, 'phones': list(row.ctc_phones)}
            if trace is not None:
                detail['first_pass'] = trace[index]
            details.append(detail)
            if known[index] is not None:
                detail.update(status='retained', start=known[index])
            elif row.error:
                detail['reason'] = 'reading_unavailable'
            elif row.phones and not supported[index]:
                detail['reason'] = 'unsupported_model_phones'
            elif candidate is not None:
                phone_coverage = getattr(candidate, 'phone_coverage', 1.0)
                recovered = getattr(candidate, 'recovered', False)
                detail.update(acoustic_score=float(candidate.score), margin=float(candidate.margin),
                              phone_coverage=phone_coverage, recovered=recovered,
                              ctc_start=round(start_position + candidate.start_ms / 1000, 3))
                if candidate.ambiguous:
                    detail.update(status='conflict', reason='ambiguous_ctc_placement')
                    continue
                begin_ms, end_ms = candidate.start_ms, candidate.end_ms
                if refiner is not None:
                    clip_left = max(0, last_candidate, begin_ms - 1600)
                    clip_right = min(len(samples) * 1000 // SAMPLE_RATE, next_candidates[index],
                                     max(end_ms + 1000, clip_left + 5000))
                    detail['refine_range'] = [round(start_position + v / 1000, 3)
                                              for v in (clip_left, clip_right)]
                    try:
                        refined = (refiner.refine(samples, row.phones, clip_left, clip_right,
                                                  reading=row.reading, start_limit_ms=clip_left,
                                                  end_limit_ms=clip_right)
                                   if 1200 <= clip_right - clip_left <= 20000 else None)
                    except Exception as error:
                        raise _PyshiroRefinementError(f'pyshiro v2 추론 실패 ({type(error).__name__})') from error
                    if refined is None:
                        detail['reason'] = 'no_refined_boundary'
                    else:
                        detail['pyshiro_start'] = round(start_position + refined.start_ms / 1000, 3)
                        detail['pyshiro_score'] = float(refined.score)
                        if abs(refined.start_ms - begin_ms) > 650:
                            detail.update(status='conflict', reason='ctc_pyshiro_disagreement')
                            last_candidate = end_ms
                            continue
                        begin_ms, end_ms = refined.start_ms, refined.end_ms
                        detail['engine'] = HYBRID
                last_candidate = candidate.end_ms
                stamp = round(start_position + begin_ms / 1000, 3)
                left, right = bounds[index]
                finish = min(end, right, round(start_position + end_ms / 1000, 3))
                if (previous < stamp < finish and left <= stamp < right
                        and any(a <= stamp < b for a, b in ranges)):
                    if recovered or phone_coverage < .65 or candidate.margin < .40:
                        quality = 'low'
                    elif phone_coverage >= .9 and candidate.margin >= .85:
                        quality = 'high'
                    else:
                        quality = 'medium'
                    detail.update(status='candidate', estimated=True, start=stamp, end=finish, quality=quality)
                    times[index] = stamp
                    coverage[index] = 1.0
                    previous = stamp
                else:
                    detail.update(status='conflict', reason='outside_recorded_or_allowed_range')
            else:
                detail['reason'] = 'no_admissible_ctc_path'
            report(.65 + .35 * (index + 1) / max(1, len(prepared)), 1)
        return times, coverage, details

    engine_details = {}
    try:
        with ExitStack() as stack:
            refiner = None
            if engine == HYBRID:
                try:
                    from pyshiro_refine import PyshiroRefiner
                    config = request.get('pyshiro', {})
                    refiner = PyshiroRefiner(config['python'], config['checkpoint'], config['phonemap'])
                    stack.callback(refiner.close)
                except Exception as error:
                    raise _PyshiroRefinementError(f'pyshiro v2 준비 실패 ({type(error).__name__})') from error
            times, coverage, details = collect_rows(refiner, progress)
    except _PyshiroRefinementError as error:
        engine = CTC
        engine_details = {'requested_engine': HYBRID, 'refinement_warning': str(error)}
        if emit:
            emit('progress', state='analyzing', text=f'{error} · RomajiASR 단독 후보를 저장합니다.')
        times, coverage, details = collect_rows(None, lambda *_: None)
    progress(1, 1)
    validate_times(times, len(times))
    matched = sum(value is not None for value in times)
    return {
        'times': times, 'coverage': coverage, 'matched': matched, 'duration': duration,
        'method': engine, 'engine': engine, 'model': runtime['model'], 'runtime': runtime,
        'alignment_version': ALIGNMENT_VERSION, 'romaji_profile': profile,
        'review_only': True, 'confidence_calibrated': False, 'line_alignment': details,
        'incremental': bool(request.get('incremental')), 'analyzed_ranges': ranges if matched else [],
        'partial': start_position > 3 or end < duration - 2 or matched < len(prepared),
        'capture_range': [start_position, end],
        **engine_details,
    }
