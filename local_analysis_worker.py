"""Process the local audio queue using the optional audio runtime."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
import time

from app_paths import default_data_dir, runtime_dir
from local_analysis import (AnalysisStore, WorkerBusy, alignment_quality, alignment_recipe, audio_digest,
                            automatic_alignment_engine, input_file, local_alignment_config, worker_lock)
from storage import Store, read_json, validate_times

SAMPLE_RATE = 16000


def _reject_external_io(url, flags, options):
    raise OSError('다른 파일이나 네트워크를 참조하는 재생 목록은 지원하지 않습니다.')


def decode_audio(path, *, sample_limit=None):
    """FFmpeg/PyAV decode of one local file to mono float32 at 16 kHz."""
    import av
    import numpy as np
    blocks = []
    total = 0
    with input_file(path).open('rb') as handle:
        with av.open(handle, io_open=_reject_external_io,
                     container_options={'protocol_whitelist': 'file'}) as container:
            if not container.streams.audio:
                raise ValueError('파일에서 오디오 스트림을 찾지 못했습니다.')
            resampler = av.AudioResampler(format='flt', layout='mono', rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for output in resampler.resample(frame):
                    blocks.append(output.to_ndarray().ravel())
                    total += output.samples
                    if total > 3600 * SAMPLE_RATE:
                        raise ValueError('한 작업에는 최대 1시간 길이의 음원을 등록할 수 있습니다.')
                if sample_limit is not None and total >= sample_limit:
                    break
            else:
                blocks.extend(output.to_ndarray().ravel() for output in resampler.resample(None))
    if not blocks:
        raise ValueError('파일에서 디코딩할 수 있는 오디오가 없습니다.')
    samples = np.concatenate(blocks)
    if not np.isfinite(samples).all():
        raise ValueError('음원에 유효하지 않은 샘플이 있습니다.')
    return samples


def probe_alignment(settings, runtime):
    from audio_job import MODEL_FILES
    from audio_runtime import prepare_cuda_dlls
    from japanese_alignment import CTC, alignment_options
    from importlib.util import find_spec

    options = alignment_options({**(settings.get('audio_alignment') or {}), 'engine': CTC}, runtime)
    model = Path(options['romaji_model_dir'])
    romaji = (model / 'phoneme_vocab.json').is_file() and any(
        (model / name).is_file() for name in ('model-logits.onnx', 'model.onnx'))
    stable = all((runtime / 'models' / 'whisper-small' / name).is_file() for name in MODEL_FILES)
    result = {'romaji': bool(romaji and find_spec('onnxruntime') and find_spec('pyopenjtalk')),
              'stable': bool(stable and find_spec('stable_whisper')), 'romaji_gpu': [], 'stable_gpu': False}
    prepare_cuda_dlls()
    try:
        import onnxruntime as ort
        result['romaji_gpu'] = [name for provider, name in (
            ('CUDAExecutionProvider', 'CUDA'), ('DmlExecutionProvider', 'DirectML'))
            if provider in ort.get_available_providers()]
    except (ImportError, OSError, RuntimeError):
        pass
    try:
        import ctranslate2
        result['stable_gpu'] = ctranslate2.get_cuda_device_count() > 0
    except (ImportError, OSError, RuntimeError):
        pass
    return result


def play_preview(job, start, end):
    import io
    import wave
    import winsound
    import numpy as np

    path = input_file(job['path'])
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= start + 8:
        raise ValueError('최대 8초의 유효한 미리 듣기 구간을 선택하세요.')
    if audio_digest(path) != job['sha256']:
        raise ValueError('등록한 음원 파일이 변경되었습니다. 현재 버전을 다시 등록하세요.')
    first, last = round(start * SAMPLE_RATE), round(end * SAMPLE_RATE)
    samples = decode_audio(path, sample_limit=last)[first:last]
    if not samples.size:
        raise ValueError('선택한 시각에 재생할 오디오가 없습니다.')
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
    winsound.PlaySound(output.getvalue(), winsound.SND_MEMORY | winsound.SND_NODEFAULT)


def _align(samples, spec, duration, settings, runtime, *, start_position=0.0, **context):
    from audio_alignment import AlignmentUnmatched, align_lyrics
    from audio_job import FINAL_GPU_MODEL_FILES
    from japanese_alignment import CTC, LEGACY, alignment_options
    config = local_alignment_config(settings, spec)
    options = alignment_options(config, runtime)
    device = config.get('device', 'cpu')
    language = 'ja' if automatic_alignment_engine(spec['lines']) == CTC else None
    request = {'lines': spec['lines'], 'duration': duration,
               'model': str(runtime / 'models/whisper-small'),
               'final_device': 'auto' if device == 'gpu' else device if device in ('cpu', 'cuda', 'auto') else 'cpu',
               'alignment_language': config.get('language', language), **options, **context}
    turbo = runtime / 'models/whisper-turbo'
    if request['final_device'] != 'cpu' and all((turbo / name).is_file() for name in FINAL_GPU_MODEL_FILES):
        request['final_model'] = str(turbo)
    if spec.get('line_readings'):
        request['line_readings'] = spec['line_readings']
    runtime_info = {}
    fallback = {}

    def emit(event, **data):
        if 'runtime' in data:
            runtime_info.clear()
            runtime_info.update(data['runtime'])
        if data.get('fallback_reason'):
            fallback.update(engine=LEGACY, fallback_reason=data['fallback_reason'])

    try:
        result = align_lyrics(request, samples, start_position, lambda *_: None, emit)
    except AlignmentUnmatched as error:
        result = {'times': [None] * len(spec['lines']), 'matched': 0, 'method': 'unmatched',
                  'alignment_warning': str(error), **fallback}
    result.setdefault('engine', options.get('alignment_engine', LEGACY))
    result.setdefault('requested_engine', config['engine'])
    result.setdefault('runtime', runtime_info)
    result['requested_device'] = device
    validate_times(result['times'], len(spec['lines']))
    result['review_only'] = True
    result['confidence_calibrated'] = False
    if not result.get('line_alignment'):
        coverage = result.get('coverage') or [0.0] * len(spec['lines'])
        confidence = result.get('confidence') or [0.0] * len(spec['lines'])
        result['line_alignment'] = [
            {'index': i, 'start': start, 'end': None,
             'status': 'candidate' if start is not None else 'missing',
             'quality': 'medium' if start is not None and coverage[i] >= .85 and confidence[i] >= .45 else 'low'}
            for i, start in enumerate(result['times'])
        ]
    return result


def review_windows(alignment, duration):
    """Use confident neighboring rows as bounds for each uncertain run."""
    times = alignment.get('times', [])
    details = alignment.get('line_alignment', [])
    anchors = [(index, time) for index, time in enumerate(times)
               if alignment_quality(details[index] if index < len(details) else {}, time) != 'low']
    if not anchors:
        return []
    boundaries = [(-1, 0.0), *anchors, (len(times), duration)]
    return [(left, right, start, end) for (left, start), (right, end) in zip(boundaries, boundaries[1:])
            if right - left > 1 and 1 <= end - start <= 45]


def reanalyze_alignment(store, job, settings, runtime, *, engine=None, recover_missing=False):
    from japanese_alignment import CTC

    spec, result = job['spec'], job['result']
    original = result.get('alignment', {})
    duration = result.get('duration')
    if not duration or original.get('engine') == 'supplied' or not original.get('times'):
        raise ValueError('먼저 음원에서 가사 싱크 후보를 분석하세요.')
    windows = review_windows(original, duration)
    if not windows:
        raise ValueError('다시 분석할 수 있도록 시각 범위가 확인된 미확정 구간이 없습니다.')
    path = input_file(job['path'])
    if audio_digest(path) != job['sha256']:
        raise ValueError('등록한 음원 파일이 변경되었습니다. 현재 버전을 다시 등록하세요.')
    samples = decode_audio(path)
    baseline = original['times']
    times = [None] * len(baseline)
    details = [{} for _ in times]
    attempted = []
    engines = []
    config = local_alignment_config(settings, spec)
    if engine:
        config['engine'] = engine
    elif original.get('requested_engine'):
        config['engine'] = original['requested_engine']
    if recover_missing and config['engine'] == CTC:
        config['profile'] = 'recovery'
    config.setdefault('language', 'ja' if automatic_alignment_engine(spec['lines']) == CTC else None)
    spec = {**spec, 'alignment': config}
    for left, right, start, end in windows:
        low, high = max(0, left), min(len(times), right + 1)
        indices = range(left + 1, right)
        attempted.extend(indices)
        known = [baseline[index] if index in (left, right) else None for index in range(low, high)]
        context = {'confirmed_times': known, 'incremental': True}
        if left >= 0 and right < len(times):
            # Both engine paths use the same audio crop and absolute time bounds.
            context['capture_window'] = [start, end]
        first = round(start * SAMPLE_RATE)
        last = min(len(samples), round(end * SAMPLE_RATE))
        if last <= first:
            continue
        clip_spec = {**spec, 'lines': spec['lines'][low:high]}
        if spec.get('line_readings'):
            clip_spec['line_readings'] = spec['line_readings'][low:high]
        candidate = _align(samples[first:last], clip_spec,
                           duration, settings, runtime, start_position=first / SAMPLE_RATE, **context)
        engines.append({key: candidate[key] for key in
                        ('engine', 'requested_engine', 'requested_device', 'runtime', 'fallback_reason')
                        if key in candidate})
        for index in indices:
            relative = index - low
            value = candidate['times'][relative]
            detail = candidate['line_alignment'][relative]
            recover_for_review = (candidate.get('romaji_profile') == 'recovery' and detail.get('recovered'))
            if (value is not None and start <= value < end
                    and (recover_for_review or alignment_quality(detail, value) != 'low')):
                times[index] = value
            details[index] = {**detail, 'index': index, 'engine': candidate['engine']}
    validate_times(times, len(times))
    review = {'times': times, 'line_alignment': details, 'attempted': attempted, 'requested_engine': config['engine'],
              'matched': sum(value is not None for value in times), 'engines': engines}
    # Publish atomically after all local windows. A cancelled attempt leaves both earlier
    # analyses and the recording's saved timing available.
    store.update(job, job['status'], job['stage'], {**result, 'alignment_review': review}, job['error'])
    return store.get(job['id'])


def segment_ranges(times, details, duration):
    """Do not fill gaps using the next match after a missing lyric."""
    ranges, indices = [], []
    for index, start in enumerate(times):
        if start is None:
            continue
        next_start = times[index + 1] if index + 1 < len(times) else duration
        acoustic_end = details[index].get('end') if index < len(details) else None
        end = min(value for value in (next_start, acoustic_end, duration) if value is not None)
        if acoustic_end is None and (next_start is None or end - start > 15):
            continue
        if end - start >= .25:
            ranges.append((round(start * 1000), round(end * 1000)))
            indices.append(index)
    return ranges, indices


def _voice(store, job, path, result, settings, runtime, analyzers=None):
    from character_voice import SAMPLE_RATE as VOICE_RATE, VoiceAnalyzer
    spec = job['spec']
    config = {**(settings.get('character_voice') or {}), **spec.get('voice', {})}
    strategy = config.pop('strategy', None)
    config.setdefault('models_dir', str(runtime / 'models/character-voice'))
    config.setdefault('device', 'auto')
    analyzers = {} if analyzers is None else analyzers
    if spec['kind'] == 'group' and strategy == 'overlap-assignment':
        return _overlap_voice(store, job, path, result, config, analyzers)
    for option in ('overlap_calibration', 'matched_profiles', 'verify_targets'):
        config.pop(option, None)
    config_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if config_id not in analyzers:
        analyzers.clear()
        analyzers[config_id] = VoiceAnalyzer(store.directory / 'voice_cache', **config)
    analyzer = analyzers[config_id]
    kind = 'speech' if spec['kind'] == 'speech' else 'singing'
    model = analyzer.model_id(kind)
    if spec['kind'] in ('solo', 'speech'):
        audio = analyzer.prepare(path, isolated=bool(spec.get('isolated')), kind=kind, source_hash=job['sha256'])
        samples = analyzer.enroll(audio, ranges=spec.get('ranges'))
        if not samples:
            raise ValueError('프로필에 사용할 충분한 음성 구간이 없습니다.')
        store.save_profile(job, model, [{**sample, 'preparation': audio.preparation_id, 'sample_rate': VOICE_RATE}
                                        for sample in samples])
        return {'model': model, 'kind': kind, 'samples': len(samples), 'preparation': audio.preparation_id,
                'sample_rate': VOICE_RATE, 'audio_sha256': job['sha256'],
                'device': analyzer.actual_device, 'fallback_reason': analyzer.fallback_reason}

    times = spec.get('times')
    details = []
    if times is None:
        times = result['alignment']['times']
        details = result['alignment']['line_alignment']
    ranges, indices = segment_ranges(times, details, result['duration'])
    profiles, names = store.profiles(spec['participants'], model, exclude_sha256=job['sha256'],
                                     exclude_track_id=spec.get('track_id') or '')
    profiles_id = hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest()
    missing = [item for item, values in profiles.items() if not values]
    rows, preparation = {}, None
    if ranges and (len(missing) < len(profiles) or analyzer.separator_backend == 'unmixx'):
        isolated = bool(spec.get('isolated'))
        preparation = analyzer.preparation_id(isolated=isolated, kind=kind)
        previous = result.get('voice_previous', {})
        if (previous.get('config_id'), previous.get('model'), previous.get('profiles_sha256'),
                previous.get('preparation')) == (config_id, model, profiles_id, preparation):
            rows.update({row['index']: row for row in previous.get('rows', []) if row['status'] == 'estimated'})
        pending = [(index, bounds) for index, bounds in zip(indices, ranges) if index not in rows]
        if pending:
            audio = analyzer.prepare(path, isolated=isolated, kind=kind, source_hash=job['sha256'])
            scores = analyzer.score(audio, ranges=[bounds for _, bounds in pending], profiles=profiles)
            if len(scores) != len(pending):
                raise ValueError('가창자 분석 결과의 구간 수가 일치하지 않습니다.')
            rows.update({index: {**score, 'index': index} for (index, _), score in zip(pending, scores)})
    elif ranges:
        rows = {index: {'index': index, 'start_ms': start, 'end_ms': end, 'status': 'unavailable',
                        'scores': dict.fromkeys(profiles), 'singers': [], 'estimated_count': None}
                for index, (start, end) in zip(indices, ranges)}
    return {'model': model, 'names': names, 'missing_profiles': missing,
            'rows': [rows[index] for index in indices if index in rows],
            'participants': spec['participants'], 'song_id': spec.get('song_id') or spec.get('track_id'),
            'audio_sha256': job['sha256'], 'profiles_sha256': profiles_id, 'config_id': config_id,
            'device': analyzer.actual_device, 'fallback_reason': analyzer.fallback_reason,
            'timing_source': 'supplied' if spec.get('times') is not None else 'estimated',
            'preparation': preparation}


def _overlap_voice(store, job, path, result, config, analyzers):
    from singing_analysis import STRATEGY, SingingAnalysis, matched_profiles

    spec = job['spec']
    model_dir = Path(config['models_dir'])
    calibration = Path(config.get('overlap_calibration') or model_dir / 'singing-v2.json')
    calibration_digest = audio_digest(calibration) if calibration.is_file() else None
    selected = {'strategy': STRATEGY, 'models_dir': str(model_dir), 'device': config['device'],
                'calibration_sha256': calibration_digest, 'verify_targets': bool(config.get('verify_targets')),
                'matched_profiles': bool(config.get('matched_profiles', True))}
    config_id = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    if config_id not in analyzers:
        analyzers.clear()
        analyzers[config_id] = SingingAnalysis(store.directory / 'voice_cache', models_dir=model_dir,
                                               device=config['device'], calibration_path=calibration,
                                               verify_targets=selected['verify_targets'])
    analyzer = analyzers[config_id]
    models = analyzer.model_ids()
    profiles, names = {}, {}
    for backend, model in models.items():
        profiles[backend], known = store.profiles(spec['participants'], model, exclude_sha256=job['sha256'],
                                                  exclude_track_id=spec.get('track_id') or '')
        names.update(known)
    if selected['matched_profiles']:
        profiles = matched_profiles(profiles)
    profiles_id = hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest()
    missing = [name for name in spec['participants'] if any(not values[name] for values in profiles.values())]
    times = spec.get('times')
    details = []
    if times is None:
        times = result['alignment']['times']
        details = result['alignment']['line_alignment']
    ranges, indices = segment_ranges(times, details, result['duration'])
    isolated = bool(spec.get('isolated'))
    preparation = analyzer.analyzers['ecapa'].preparation_id(isolated=isolated) if ranges else None
    previous = result.get('voice_previous', {})
    rows = {}
    if (previous.get('config_id'), previous.get('models'), previous.get('profiles_sha256'),
            previous.get('preparation')) == (config_id, models, profiles_id, preparation):
        rows.update({row['index']: row for row in previous.get('rows', []) if row['status'] == 'estimated'})
    pending = [(index, bounds) for index, bounds in zip(indices, ranges) if index not in rows]
    memory = previous.get('cuda_memory') if not pending else None
    if pending:
        import torch

        measure_cuda = config['device'] != 'cpu' and torch.cuda.is_available()
        if measure_cuda:
            torch.cuda.reset_peak_memory_stats()
        audio = analyzer.prepare(path, isolated=isolated, source_hash=job['sha256'])
        scores = analyzer.score(audio, ranges=[bounds for _, bounds in pending], profiles=profiles)
        rows.update({index: {**row, 'index': index} for (index, _), row in zip(pending, scores, strict=True)})
        if measure_cuda:
            memory = {'peak_allocated_bytes': torch.cuda.max_memory_allocated(),
                      'peak_reserved_bytes': torch.cuda.max_memory_reserved()}
    return {'model': STRATEGY, 'models': models, 'names': names, 'missing_profiles': missing,
            'strategy': STRATEGY, 'calibration_sha256': analyzer.calibration_id,
            'rows': [rows[index] for index in indices], 'participants': spec['participants'],
            'song_id': spec.get('song_id') or spec.get('track_id'), 'audio_sha256': job['sha256'],
            'profiles_sha256': profiles_id, 'config_id': config_id, 'preparation': preparation,
            'timing_source': 'supplied' if spec.get('times') is not None else 'estimated',
            'device': {name: item.actual_device for name, item in analyzer.analyzers.items()},
            'separator_device': analyzer.separator.device if analyzer.separator else None,
            'cuda_memory': memory,
            'fallbacks': {name: item.fallback_reason for name, item in analyzer.analyzers.items()
                          if item.fallback_reason}}


def _supplied_alignment(times):
    return {'times': times, 'matched': sum(value is not None for value in times),
            'method': 'supplied', 'engine': 'supplied',
            'line_alignment': [{'index': i, 'start': value, 'end': None,
                                'status': 'retained' if value is not None else 'missing'}
                               for i, value in enumerate(times)]}


def playlist_clock(path, track, times):
    from local_library import recording_tags_match

    verified = recording_tags_match(path, track)
    pairs = [(expected, actual - expected)
             for expected, actual in zip(track.get('anchors', ()), times)
             if expected is not None and actual is not None]
    clock = {'usable': verified, 'recording_tags_match': verified, 'anchor_count': len(pairs)}
    if not verified:
        clock['reason'] = 'recording_tags_unverified'
    if len(pairs) >= 3 and pairs[-1][0] - pairs[0][0] >= 15:
        offset = median(delta for _, delta in pairs)
        error = median(abs(delta) for _, delta in pairs)
        clock.update(median_offset=round(offset, 3), median_absolute_error=round(error, 3))
        if abs(offset) > 1 or error > 2:
            clock.update(usable=False, reason='playlist_clock_mismatch')
    return clock


def process_job(store, job, settings, runtime, analyzers=None):
    """Checkpoint each result before advancing; failed and interrupted stages can resume."""
    spec, result = job['spec'], job['result']
    stage = job['stage']
    store.update(job, 'running', stage, result)
    try:
        path = input_file(job['path'])
        initial_stat = path.stat()
        if audio_digest(path) != job['sha256']:
            raise ValueError('등록한 뒤 파일 내용이 바뀌었습니다. 현재 버전을 다시 등록하세요.')

        def decode_current():
            samples = decode_audio(path)
            final_stat = path.stat()
            if (final_stat.st_size, final_stat.st_mtime_ns) != (initial_stat.st_size, initial_stat.st_mtime_ns):
                raise ValueError('디코딩 중 파일 내용이 바뀌었습니다. 현재 버전을 다시 등록하세요.')
            return samples

        recipe = spec.get('alignment_recipe')
        if recipe and 'alignment' not in result and alignment_recipe(spec, Path(runtime)) != recipe:
            raise ValueError('등록한 뒤 정렬 모델이나 설정 버전이 바뀌었습니다. 음원을 다시 등록하세요.')
        cached = store.cached_alignment(job) if 'alignment' not in result else None
        samples = None
        if 'duration' not in result:
            started = time.perf_counter()
            if cached:
                duration = cached[1]
            else:
                samples = decode_current()
                duration = len(samples) / SAMPLE_RATE
            expected = spec.get('expected_duration')
            if expected is not None and (not math.isfinite(expected) or abs(expected - duration) > 2):
                raise ValueError('로컬 파일 길이가 연결한 녹음과 2초 넘게 다릅니다.')
            if any(stamp is not None and stamp >= duration for stamp in spec.get('times') or []):
                raise ValueError('입력한 가사 시작 시각이 음원 길이를 벗어났습니다.')
            result.update(duration=duration, seconds={'decode': time.perf_counter() - started})
            stage = 'decoded'
            store.update(job, 'running', stage, result)
        if spec['kind'] in ('lyrics', 'group') and 'alignment' not in result:
            started = time.perf_counter()
            supplied = spec.get('times') if spec['kind'] == 'group' else None
            if cached:
                result['alignment'] = cached[2]
                result['alignment_reused_from'] = cached[0]
            elif supplied is not None and not spec.get('recompute_alignment'):
                result['alignment'] = _supplied_alignment(supplied)
            else:
                if samples is None:
                    samples = decode_current()
                try:
                    result['alignment'] = _align(samples, spec, result['duration'], settings, runtime)
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    if supplied is None:
                        raise
                    result['alignment'] = _supplied_alignment(supplied)
                    result['alignment']['alignment_error'] = f'{type(error).__name__}: {error}'
            result['seconds']['alignment'] = time.perf_counter() - started
            stage = 'aligned'
            store.update(job, 'running', stage, result)
        del samples
        if 'playlist_recording' in spec and 'playlist_clock' not in result:
            result['playlist_clock'] = playlist_clock(path, spec['playlist_recording'], result['alignment']['times'])
        if spec['kind'] != 'lyrics' and 'voice' not in result:
            started = time.perf_counter()
            result['voice'] = _voice(store, job, path, result, settings, runtime, analyzers)
            result.pop('voice_previous', None)
            result['seconds']['voice'] = time.perf_counter() - started
            stage = 'voice'
            store.update(job, 'running', stage, result)
        store.update(job, 'done', stage, result)
    except Exception as error:
        store.update(job, 'failed', stage, result, f'{type(error).__name__}: {error}')
    return store.get(job['id'])


def run_queue(store, *, job_id=None, job_ids=None, retry=False, refresh_voice=False, uncertain_only=False,
              review_alignment=False, review_engine=None, recover_missing=False, runtime=None, emit=print):
    from japanese_alignment import release_ctc

    settings = Store(store.directory).settings()
    runtime = runtime if runtime is not None else runtime_dir()
    failed = False
    analyzers = {}
    requested = [job_id] if job_id else job_ids
    with worker_lock(store.directory), ExitStack() as cleanup:
        cleanup.callback(release_ctc)
        pending = store.pending() if requested is None else list(requested)
        while pending:
            for current in pending:
                job = store.get(current)
                if job is None:
                    raise ValueError('해당 작업 ID를 찾지 못했습니다.')
                if review_alignment:
                    job = reanalyze_alignment(store, job, settings, runtime, engine=review_engine,
                                              recover_missing=recover_missing)
                elif refresh_voice and job['spec']['kind'] == 'group':
                    previous = job['result'].pop('voice', None)
                    if previous:
                        job['result']['voice_last_completed'] = previous
                    if uncertain_only and previous:
                        job['result']['voice_previous'] = previous
                    elif not uncertain_only:
                        job['result'].pop('voice_previous', None)
                    job['stage'] = 'aligned' if 'alignment' in job['result'] else job['stage']
                    store.update(job, 'queued', job['stage'], job['result'])
                    job['status'] = 'queued'
                if not review_alignment and job['status'] != 'done' and (job['status'] != 'failed' or retry):
                    job = process_job(store, job, settings, runtime, analyzers)
                failed |= job['status'] == 'failed'
                emit({key: job[key] for key in ('id', 'status', 'stage', 'error')})
            pending = store.pending() if requested is None else []
    return int(failed)


def import_manifest(store, manifest, seen=None, *, runtime=None):
    from local_library import LocalLibrary
    manifest = Path(manifest).resolve()
    value = read_json(manifest, {})
    if not isinstance(value, dict) or not isinstance(value.get('inputs'), list):
        raise ValueError('manifest에 inputs 배열을 지정하세요.')
    folders = value.get('folders', [])
    if not isinstance(folders, list) or not all(isinstance(folder, str) for folder in folders):
        raise ValueError('folders에는 분석 권한이 있는 로컬 폴더를 지정하세요.')
    library = LocalLibrary(store.directory) if folders or any(
        isinstance(item, dict) and not item.get('path') for item in value['inputs']) else None
    for folder in folders:
        library.scan(manifest.parent / folder, persist=False)
    songs = {song.id: song for song in Store(store.directory).library()}
    for item in value['inputs']:
        if not isinstance(item, dict):
            raise ValueError('inputs의 각 항목은 JSON 객체여야 합니다.')
        spec = dict(item)
        path = spec.pop('path', None)
        song = songs.get(spec.get('song_id'))
        if song:
            spec.setdefault('lines', [line.original for line in song.lines])
            spec.setdefault('title', song.title)
            spec.setdefault('artist', song.artist)
            spec.setdefault('album', song.album)
        if spec.pop('link_playback', False):
            values = [spec.get(key) for key in ('title', 'artist', 'album')]
            if not all(isinstance(item, str) for item in values):
                raise ValueError('재생과 연결하려면 Spotify에 표시되는 제목·가수·앨범을 지정하세요.')
            spec['recording'] = '\0'.join(values)
        if path is None:
            linked = [file for file in library.for_song(spec['song_id']) if file['exists']] if spec.get('song_id') else []
            candidates = linked or library.exact_recordings(spec)
            if len(candidates) != 1:
                yield {'song_id': spec.get('song_id'), 'status': 'ambiguous_audio' if candidates else 'audio_missing'}
                continue
            path = candidates[0]['path']
        path = manifest.parent / path
        if not path.is_file():
            yield {'song_id': spec.get('song_id'), 'path': str(path), 'status': 'audio_missing'}
            continue
        try:
            if seen is not None:
                info = input_file(path).stat()
                key = str(path), json.dumps(spec, sort_keys=True)
                stamp = info.st_size, info.st_mtime_ns
                if seen.get(key) == stamp:
                    continue
            job = store.register(path, spec, runtime=runtime)
            if seen is not None:
                seen[key] = stamp
        except (OSError, ValueError) as error:
            yield {'path': str(path), 'status': 'registration_failed', 'error': str(error)}
            continue
        yield {'id': job, 'status': 'registered'}


def main(argv=None):
    from japanese_alignment import CTC, HYBRID, LEGACY

    os.environ.setdefault('OMP_NUM_THREADS', '4')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--runtime-dir', type=Path, default=runtime_dir())
    parser.add_argument('--manifest', type=Path, help='Register local files and process pending jobs.')
    parser.add_argument('--watch', type=int, metavar='SECONDS', help='Refresh manifest inputs on a fixed interval.')
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--job', help='Process one specific job ID.')
    commands.add_argument('--show', help='Read the stored job and its detailed results.')
    commands.add_argument('--export-labels', nargs=2, metavar=('JOB_ID', 'OUTPUT'),
                          help='Export explicitly reviewed singer rows for evaluation.')
    commands.add_argument('--probe-alignment', action='store_true', help='Report installed alignment runtimes.')
    commands.add_argument('--preview', nargs=3, metavar=('JOB_ID', 'START', 'END'),
                          help='Play a short excerpt from a registered file.')
    parser.add_argument('--retry', action='store_true', help='Resume a failed job selected with --job.')
    parser.add_argument('--wait-for-queue', action='store_true', help='Wait for the active worker before starting this job.')
    parser.add_argument('--refresh-voice', action='store_true', help='Rescore a group after enrolling profiles.')
    parser.add_argument('--uncertain-only', action='store_true', help='Reuse current estimated rows on refresh.')
    parser.add_argument('--review-alignment', action='store_true', help='Realign only bounded uncertain rows.')
    parser.add_argument('--review-engine', choices=(CTC, LEGACY, HYBRID),
                        help='Use a different engine for review candidates.')
    parser.add_argument('--recover-missing', action='store_true', help='Try CTC recovery for review candidates.')
    args = parser.parse_args(argv)
    if (args.retry or args.refresh_voice or args.wait_for_queue or args.review_alignment) and not args.job:
        parser.error('--retry, --refresh-voice, --review-alignment, and --wait-for-queue require --job')
    if args.uncertain_only and not args.refresh_voice:
        parser.error('--uncertain-only requires --refresh-voice')
    if args.refresh_voice and args.review_alignment:
        parser.error('--refresh-voice and --review-alignment are mutually exclusive')
    if (args.review_engine or args.recover_missing) and not args.review_alignment:
        parser.error('--review-engine and --recover-missing require --review-alignment')
    if args.watch is not None and (args.watch < 5 or not args.manifest or args.job or args.show or args.export_labels):
        parser.error('--watch requires --manifest and a polling interval of at least five seconds')
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        if args.probe_alignment:
            emit(probe_alignment(Store(args.data_dir).settings(), args.runtime_dir))
            return 0
        store = AnalysisStore(args.data_dir)
        if args.preview:
            job_id, start, end = args.preview
            job = store.get(job_id)
            if not job:
                raise ValueError('해당 작업 ID를 찾지 못했습니다.')
            play_preview(job, float(start), float(end))
            return 0
        if args.export_labels:
            from storage import atomic_json
            job_id, destination = args.export_labels
            atomic_json(Path(destination).resolve(), store.reviewed_labels(job_id))
            return 0
        if args.show:
            job = store.get(args.show)
            if not job:
                raise ValueError('해당 작업 ID를 찾지 못했습니다.')
            emit(job)
            return 0
        seen = {} if args.watch else None
        while True:
            registration_failed = False
            if args.manifest:
                for item in import_manifest(store, args.manifest, seen, runtime=args.runtime_dir):
                    registration_failed |= item['status'] == 'registration_failed'
                    emit(item)
            try:
                code = run_queue(store, job_id=args.job, retry=args.retry, refresh_voice=args.refresh_voice,
                                 uncertain_only=args.uncertain_only, review_alignment=args.review_alignment,
                                 review_engine=args.review_engine, recover_missing=args.recover_missing,
                                 runtime=args.runtime_dir, emit=emit)
            except WorkerBusy:
                if args.wait_for_queue:
                    time.sleep(.5)
                    continue
                if not args.watch:
                    raise
                code = 0
            if not args.watch:
                return code or int(registration_failed)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        emit({'status': 'error', 'error': str(error)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
