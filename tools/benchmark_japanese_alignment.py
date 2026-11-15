"""Align local audio or recorded clips against independent line onsets."""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
from statistics import mean, median, quantiles
import subprocess
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lyrics import normalized
from lyric_match import match_lyrics
from storage import Store, atomic_json, validate_times
from sync import DEFAULT_LYRIC_ADVANCE_MS, MAX_LYRIC_ADVANCE_MS, lyric_display_time
from timing_quality import timing_origins

ENGINES = ('stable_ts_legacy', 'romaji_ctc', 'hybrid_japanese')
THRESHOLDS_MS = (100, 150, 200, 300, 500, 1000)
DEVICE_NAMES = {'cpu', 'cuda', 'dml', 'auto'}
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def spotify_record(data_dir, track_id, key, duration):
    if len(track_id) != 22 or not track_id.isascii() or not track_id.isalnum():
        raise ValueError('Invalid Spotify track ID')
    path = data_dir / 'playlist_lyrics' / 'tracks' / (track_id + '.json')
    entry = read_json(path)
    track = entry['track']
    identity = '\0'.join((track['title'], track['artists'][0], track['album']))
    if track['id'] != track_id or identity != key or abs(track['duration'] - duration) > 1:
        raise ValueError('Spotify recording metadata differs from the capture')
    return entry, path


def spotify_reference(entry, lines):
    direct = entry['direct']
    if direct['source'] != 'Spotify' or direct['provider_id'] != entry['track']['id']:
        raise ValueError('Spotify reference recording differs from the capture')
    originals = [row['original'] for row in direct['lines']]
    validate_times(direct['times'], len(originals))
    match = match_lyrics(lines, originals)
    if match is None or match.edits:
        raise ValueError('Spotify reference lyric sequence differs from the capture')
    onsets = dict(zip(match.target_offsets, direct['times']))
    return [onsets.get(offset) for offset in match.source_offsets]


def load_case(entry, manifest_dir, store, songs):
    if 'audio' in entry:
        return load_local_case(entry, manifest_dir)
    song = songs[entry['song_id']]
    split = entry['split']
    if split not in ('calibration', 'evaluation'):
        raise ValueError('Each track needs a calibration or evaluation split')
    capture = (manifest_dir / entry['capture']).resolve()
    request = read_json(capture / 'request.json')
    info = read_json(capture / 'capture-info.json')
    lines = [line.original for line in song.lines]
    key = request['key']
    if key.split('\0')[0] != song.title or request.get('lines') != lines:
        raise ValueError('Capture title and original rows must match the saved song')
    duration, start, tail = request['duration'], info['start_position'], info.get('clip_tail', 0)
    if (not all(number(x) for x in (duration, start, tail))
            or not 0 <= start < duration or tail < 0):
        raise ValueError('Invalid capture timeline')
    audio = capture / 'capture.wav'
    with wave.open(str(audio), 'rb') as stream:
        end = min(duration, start + stream.getnframes() / stream.getframerate() - tail)
    if end <= start:
        raise ValueError('The capture has no usable audio')
    spotify, path = spotify_record(store.directory, entry['spotify_track_id'], key, duration)
    reference_kind = entry.get('reference', 'spotify')
    if reference_kind == 'spotify':
        reference, reference_path = spotify_reference(spotify, lines), str(path)
        source = 'Spotify'
    elif reference_kind == 'manual':
        saved = store.timing(song)
        origins = timing_origins(saved) if 'origins' in saved or saved.get('source') == '직접 기록' else [None] * len(lines)
        reference = [round(value + saved['offset'], 6) if origin == 'manual' else None
                     for value, origin in zip(saved['times'], origins)]
        reference_path = str(store.directory / 'timings' / (song.id + '.json'))
        source = 'manual'
    else:
        raise ValueError('Invalid reference source')
    absent = reference_rows(entry, lines, reference, start, end)
    if absent and (start > 3 or end < duration - 2):
        raise ValueError('Known-unsung rows require a full capture')
    return {'id': song.id, 'title': song.title, 'recording_key': key, 'split': split,
            'audio': str(audio), 'start': start, 'end': end, 'tail': tail,
            'duration': duration, 'lines': lines, 'reference': reference,
            'absent': absent, 'reference_source': source, 'reference_path': reference_path}


def load_local_case(entry, manifest_dir):
    from faster_whisper.audio import decode_audio

    song_id, split = entry['song_id'], entry['split']
    if not isinstance(song_id, str) or not song_id or split not in ('calibration', 'evaluation'):
        raise ValueError('Each track needs a song ID and a calibration or evaluation split')
    lines = entry['lines']
    if not isinstance(lines, list) or not lines or any(not isinstance(line, str) or not line.strip() for line in lines):
        raise ValueError('A local track needs its original lyric rows')
    source = entry['reference_source']
    if not isinstance(source, str) or not source.strip():
        raise ValueError('A local track needs a reference source')
    audio = (manifest_dir / entry['audio']).resolve()
    digest = sha256()
    with audio.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    duration = len(decode_audio(str(audio), sampling_rate=16000)) / 16000
    reference = entry['reference_times']
    absent = reference_rows(entry, lines, reference, 0, duration)
    if any(value is not None and value >= duration for value in reference):
        raise ValueError('A reference onset exceeds the local recording duration')
    return {'id': song_id, 'title': entry.get('title', song_id), 'recording_key': digest.hexdigest(), 'split': split,
            'audio': str(audio), 'start': 0, 'end': duration, 'tail': 0, 'duration': duration, 'lines': lines,
            'reference': reference, 'absent': absent, 'reference_source': source, 'reference_path': None}


def reference_rows(entry, lines, reference, start, end):
    validate_times(reference, len(lines))
    if not any(value is not None and start <= value < end for value in reference):
        raise ValueError('The audio has no independent reference onsets')
    absent = set(entry.get('absent_indices', []))
    if any(type(index) is not int or not 0 <= index < len(lines) or reference[index] is not None for index in absent):
        raise ValueError('Invalid known-unsung row annotations')
    return absent


def error_metrics(errors):
    count = len(errors)
    percentiles = quantiles(errors, n=100, method='inclusive') if count > 1 else errors * 99
    return {'count': count,
            'mae_ms': round(mean(errors) * 1000, 1) if count else None,
            'median_ae_ms': round(median(errors) * 1000, 1) if count else None,
            'p90_ae_ms': round(percentiles[89] * 1000, 1) if count else None,
            'p95_ae_ms': round(percentiles[94] * 1000, 1) if count else None,
            'max_ae_ms': round(max(errors) * 1000, 1) if count else None,
            **{f'within_{threshold}ms': round(sum(e <= threshold / 1000 + 1e-9 for e in errors) / count, 4) if count else None
               for threshold in THRESHOLDS_MS},
            **{f'at_least_{threshold}s': round(sum(e >= threshold - 1e-9 for e in errors) / count, 4) if count else None
               for threshold in (1, 2)}}


def measure(case, prediction, display_advance_ms=DEFAULT_LYRIC_ADVANCE_MS):
    stamps = prediction['times']
    validate_times(stamps, len(case['lines']))
    reference = case['reference']
    eligible = {index for index, ref in enumerate(reference)
                if ref is not None and case['start'] <= ref < case['end']}
    keys = [normalized(text) for text in case['lines']]
    repeats = {}
    for index in eligible:
        repeats.setdefault(keys[index], []).append(index)
    rows = []
    details = prediction.get('line_alignment') or [{} for _ in stamps]
    if len(details) != len(stamps):
        raise ValueError('Alignment diagnostics do not match the original row count')
    for index, (ref, stamp, detail) in enumerate(zip(reference, stamps, details)):
        row = {key: detail[key] for key in ('status', 'reason', 'engine', 'acoustic_score',
               'margin', 'end', 'ctc_start', 'pyshiro_start', 'refine_range', 'quality', 'reading',
               'phones', 'phone_coverage', 'recovered', 'first_pass', 'stem_selected') if key in detail}
        display_onset = lyric_display_time(stamp, display_advance_ms) if stamp is not None else None
        row.update(index=index, reference=ref, onset=stamp, display_onset=display_onset)
        if index in eligible:
            others = [reference[other] for other in repeats[keys[index]] if other != index]
            delta = round(stamp - ref, 6) if stamp is not None else None
            display_delta = round(display_onset - ref, 6) if display_onset is not None else None
            row.update(reference_status='known', signed_error=delta, repeated=len(others) > 0,
                       display_signed_error=display_delta,
                       wrong_repeat=(delta is not None and abs(delta) >= 2
                                     and any(abs(stamp - other) <= .5 for other in others)))
        elif index in case['absent']:
            row['reference_status'] = 'absent'
        else:
            row['reference_status'] = 'unknown' if ref is None else 'outside_capture'
        rows.append(row)
    errors = [row['signed_error'] for row in rows if row.get('signed_error') is not None]
    shift = median(errors) if errors else None
    for row in rows:
        if row.get('signed_error') is not None:
            row['offset_corrected_error'] = round(row['signed_error'] - shift, 6)
    return {**aggregate(rows), 'median_signed_offset_ms': round(shift * 1000, 1) if errors else None,
            'rows': rows}


def preference_metrics(errors, reference_count):
    preferred = sum(-.5 <= error <= .1 for error in errors)
    late = sum(error > 0 for error in errors)
    return {'preferred_rows': preferred,
            'reference_in_preferred_range': round(preferred / reference_count, 4) if reference_count else None,
            'late_rows': late, 'late_ratio': round(late / len(errors), 4) if errors else None,
            'over_100ms_late_rows': sum(error > .1 for error in errors)}


def aggregate(rows):
    known = [row for row in rows if row['reference_status'] == 'known']
    absent = [row for row in rows if row['reference_status'] == 'absent']
    missing = sum(row['onset'] is None for row in known)
    matched = [row for row in known if row['onset'] is not None]
    signed = [row['signed_error'] for row in matched]
    errors = [abs(error) for error in signed]
    display_errors = [row['display_signed_error'] for row in matched]
    trace_rows = [row for row in known if 'first_pass' in row]
    first_pass_missing = [row for row in trace_rows if not row['first_pass']['phone_path_ms']]
    repeat_rows = sum(row['repeated'] for row in known)
    wrong_repeats = sum(row['wrong_repeat'] for row in known)
    return {'raw': error_metrics(errors),
            'raw_preference': preference_metrics(signed, len(known)),
            'display': {**error_metrics([abs(error) for error in display_errors]),
                        **preference_metrics(display_errors, len(known))},
            'offset_corrected': error_metrics([abs(row['offset_corrected_error']) for row in known if row['onset'] is not None]),
            **{f'reference_within_{ms}ms': round(sum(e <= ms / 1000 + 1e-9 for e in errors) / len(known), 4)
               if known else None for ms in THRESHOLDS_MS},
            'reference_rows': len(known), 'missing_rows': missing,
            'recovered_rows': sum(bool(row.get('recovered')) for row in matched),
            'first_pass_missing_rows': len(first_pass_missing) if trace_rows else None,
            'first_pass_missing_recovered_rows': (sum(row['onset'] is not None for row in first_pass_missing)
                                                  if trace_rows else None),
            'failed_500ms': len(known) - sum(e <= .5 + 1e-9 for e in errors),
            'missing_ratio': round(missing / len(known), 4) if known else None,
            'unknown_reference_rows': sum(row['reference_status'] == 'unknown' for row in rows),
            'out_of_capture_rows': sum(row['reference_status'] == 'outside_capture' for row in rows),
            'repeat_reference_rows': repeat_rows, 'wrong_repeat_onsets': wrong_repeats,
            'wrong_repeat_ratio': round(wrong_repeats / repeat_rows, 4) if repeat_rows else None,
            'absent_reference_rows': len(absent),
            'false_matches_on_absent': sum(row['onset'] is not None for row in absent) if absent else None}


def peak_rss_bytes():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        process = ctypes.windll.kernel32.GetCurrentProcess()
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        return counters.PeakWorkingSetSize if query(process, ctypes.byref(counters), counters.cb) else None
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == 'darwin' else rss * 1024


@contextmanager
def gpu_samples(enabled):
    values = {'process_mib': [], 'devices': {}}
    executable = shutil.which('nvidia-smi') if enabled else None
    stopped = threading.Event()

    def query(option):
        try:
            result = subprocess.run([executable, option, '--format=csv,noheader,nounits'],
                                    capture_output=True, timeout=3, text=True,
                                    creationflags=NO_WINDOW, check=True)
            return result.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return ()

    def poll():
        while not stopped.is_set():
            for line in query('--query-compute-apps=pid,used_gpu_memory'):
                pid, _, size = line.partition(',')
                if pid.strip() == str(os.getpid()) and size.strip().isdigit():
                    values['process_mib'].append(int(size))
            for line in query('--query-gpu=index,memory.used,utilization.gpu'):
                fields = [field.strip() for field in line.split(',')]
                if len(fields) != 3 or not all(field.isdigit() for field in fields):
                    continue
                index, memory, util = map(int, fields)
                gpu = values['devices'].setdefault(index, {'peak_memory_mib': 0, 'peak_utilization_pct': 0})
                gpu['peak_memory_mib'] = max(gpu['peak_memory_mib'], memory)
                gpu['peak_utilization_pct'] = max(gpu['peak_utilization_pct'], util)
            stopped.wait(.5)

    thread = threading.Thread(target=poll, daemon=True) if executable else None
    if thread:
        thread.start()
    try:
        yield values
    finally:
        stopped.set()
        if thread:
            thread.join()


def worker(payload):
    """One model run per process so runtime setup and memory are comparable."""
    from audio_alignment import AlignmentUnmatched, align_stable_ts
    from faster_whisper.audio import decode_audio

    start = time.perf_counter()
    samples = decode_audio(payload['audio'], sampling_rate=16000)
    if payload['tail']:
        samples = samples[:max(0, len(samples) - int(payload['tail'] * 16000))]
    decode_seconds = time.perf_counter() - start
    request = payload['request']
    runtime = {}
    def emit(_kind, **event):
        runtime.update(event.get('runtime', {}))
    if request['alignment_engine'] == ENGINES[0]:
        align = align_stable_ts
        device = request['final_device']
    else:
        from japanese_alignment import align_japanese
        align = align_japanese
        device = request['romaji_device']
    with gpu_samples(device != 'cpu') as gpu:
        start = time.perf_counter()
        try:
            result = align(request, samples, payload['start'], lambda *_: None, emit=emit)
        except AlignmentUnmatched:
            result = {'times': [None] * len(request['lines']), 'method': 'unmatched'}
        elapsed = time.perf_counter() - start
    return {**result, 'runtime': result.get('runtime') or runtime,
            'alignment_seconds': round(elapsed, 3), 'decode_seconds': round(decode_seconds, 3),
            'worker_peak_rss_bytes': peak_rss_bytes(), 'sampled_worker_gpu_mib': max(gpu['process_mib'], default=None),
            'sampled_system_gpus': gpu['devices']}


def payload_for(case, engine, args):
    # Ground-truth rows, offsets, provenance and annotations stay in the driver.
    return {'audio': case['audio'], 'start': case['start'], 'tail': case['tail'],
            'request': {'lines': case['lines'], 'duration': case['duration'],
                        'alignment_engine': engine, 'model': str(args.legacy_model.resolve()),
                        'final_model': str(args.legacy_gpu_model.resolve()),
                        'final_device': args.legacy_device, 'romaji_device': args.romaji_device,
                        'romaji_model_dir': str(args.romaji_model.resolve()), 'pyshiro': args.pyshiro,
                        'ctc_diagnostics': getattr(args, 'ctc_diagnostics', False),
                        'romaji_profile': getattr(args, 'profile', 'baseline')}}


def run_case(case, engine, args):
    report = {key: case[key] for key in ('id', 'title', 'split', 'audio', 'duration', 'start',
                                        'end', 'tail', 'reference_source', 'reference_path')}
    report['engine'] = engine
    try:
        process = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker'],
                                 input=json.dumps(payload_for(case, engine, args)), capture_output=True,
                                 encoding='utf-8', errors='replace', timeout=args.timeout, creationflags=NO_WINDOW)
        if process.returncode:
            return {**report, 'error': process.stderr.strip()[-4000:] or f'Exit {process.returncode}'}
        result = json.loads(process.stdout.strip().splitlines()[-1])
        actual_engine = result.get('engine', engine)
        if actual_engine != engine:
            return {**report, 'actual_engine': actual_engine,
                    'error': result.get('refinement_warning') or f'Requested engine fell back to {actual_engine}'}
        onsets = [stamp for stamp in result['times'] if number(stamp)]
        report['order_reversals'] = sum(later <= earlier for earlier, later in zip(onsets, onsets[1:]))
        return {**report, **{key: result[key] for key in ('method', 'runtime', 'alignment_seconds',
            'decode_seconds', 'worker_peak_rss_bytes', 'sampled_worker_gpu_mib', 'sampled_system_gpus')},
            **{key: result[key] for key in ('alignment_version', 'romaji_profile') if key in result},
            **measure(case, result, getattr(args, 'display_advance_ms', DEFAULT_LYRIC_ADVANCE_MS))}
    except (subprocess.TimeoutExpired, ValueError, OSError, IndexError, KeyError) as error:
        return {**report, 'error': str(error)}


def summarize(reports, engines):
    summaries = {}
    for split in ('calibration', 'evaluation'):
        runs = [run for run in reports if run['split'] == split and 'error' not in run]
        rows_by_engine = {engine: {(run['id'], row['index']): row
                                  for run in runs if run['engine'] == engine for row in run['rows']}
                          for engine in engines}
        common = set.intersection(*(set(rows) for rows in rows_by_engine.values()))
        common = sorted(key for key in common if all(rows[key].get('signed_error') is not None
                                                     for rows in rows_by_engine.values()))
        summary = {}
        for engine, rows in rows_by_engine.items():
            timings = [run for run in runs if run['engine'] == engine]
            attempted = [run for run in reports if run['split'] == split and run['engine'] == engine]
            paired = {}
            for other, other_rows in rows_by_engine.items():
                if engine == other:
                    continue
                shared = sorted(key for key in rows.keys() & other_rows.keys()
                                if rows[key].get('signed_error') is not None
                                and other_rows[key].get('signed_error') is not None)
                paired[other] = error_metrics([abs(rows[key]['signed_error']) for key in shared])
            summary[engine] = {**aggregate(list(rows.values())), 'tracks_completed': len(timings),
                'order_reversals': sum(run.get('order_reversals', 0) for run in attempted),
                'common_matched_raw': error_metrics([abs(rows[key]['signed_error']) for key in common]),
                'pairwise_common_matched_raw': paired,
                'alignment_seconds_total': round(sum(run.get('alignment_seconds', 0) for run in timings), 3),
                'alignment_seconds_median': median(run.get('alignment_seconds', 0) for run in timings) if timings else None,
                'worker_peak_rss_bytes': max((run['worker_peak_rss_bytes'] for run in timings
                                             if run.get('worker_peak_rss_bytes') is not None), default=None),
                'sampled_worker_gpu_mib': max((run['sampled_worker_gpu_mib'] for run in timings
                                              if run.get('sampled_worker_gpu_mib') is not None), default=None)}
        summaries[split] = summary
    return summaries


def main():
    if sys.argv[1:] == ['--worker']:
        print(json.dumps(worker(json.load(sys.stdin)), allow_nan=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    from japanese_alignment import PROFILES
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engines', choices=ENGINES, nargs='+', default=list(ENGINES))
    parser.add_argument('--split', choices=('calibration', 'evaluation'))
    parser.add_argument('--track', action='append', help='Evaluate this song ID only. Repeat for multiple IDs.')
    parser.add_argument('--legacy-device', choices=('cpu', 'cuda', 'auto'), default='cpu')
    parser.add_argument('--romaji-device', choices=sorted(DEVICE_NAMES), default='cpu')
    parser.add_argument('--profile', choices=PROFILES, default='baseline')
    parser.add_argument('--display-advance-ms', type=int, default=DEFAULT_LYRIC_ADVANCE_MS,
                        help=f'Overlay lead to score separately, from 0 to {MAX_LYRIC_ADVANCE_MS} ms')
    parser.add_argument('--ctc-diagnostics', action='store_true', help='Include first-pass paths and alternative onsets')
    parser.add_argument('--legacy-model', type=Path, default=ROOT / 'models' / 'whisper-small')
    parser.add_argument('--legacy-gpu-model', type=Path, default=ROOT / 'models' / 'whisper-turbo')
    parser.add_argument('--romaji-model', type=Path, default=ROOT / 'models' / 'romaji-asr')
    parser.add_argument('--pyshiro-python', type=Path)
    parser.add_argument('--pyshiro-checkpoint', type=Path)
    parser.add_argument('--pyshiro-phonemap', type=Path)
    parser.add_argument('--timeout', type=int, default=1200, help='Seconds allowed per track and engine')
    args = parser.parse_args()
    if not 0 <= args.display_advance_ms <= MAX_LYRIC_ADVANCE_MS:
        parser.error(f'Display lead must be between 0 and {MAX_LYRIC_ADVANCE_MS} ms')
    sys.stdout.reconfigure(errors='backslashreplace')
    args.pyshiro = {key: str(path.resolve()) for key, path in
                    zip(('python', 'checkpoint', 'phonemap'),
                        (args.pyshiro_python, args.pyshiro_checkpoint, args.pyshiro_phonemap)) if path}
    entries = read_json(args.manifest)['tracks']
    if args.track:
        entries = [entry for entry in entries if entry.get('song_id') in args.track]
    store = Store(args.data.resolve())
    songs = {song.id: song for song in store.library()} if any('audio' not in entry for entry in entries) else {}
    cases = []
    excluded = []
    for entry in entries:
        try:
            cases.append(load_case(entry, args.manifest.resolve().parent, store, songs))
        except (OSError, ValueError, KeyError, TypeError, wave.Error) as error:
            excluded.append({'id': entry.get('song_id'), 'reason': str(error)})
    if len({case['recording_key'] for case in cases}) != len(cases):
        parser.error('Each recording may occur only once across calibration and evaluation')
    if len({case['id'] for case in cases}) != len(cases):
        parser.error('Each song ID must be unique')
    if args.split:
        cases = [case for case in cases if case['split'] == args.split]
    report = {'tracks_eligible': len(cases), 'excluded_tracks': excluded, 'runs': [],
              'romaji_profile': args.profile, 'display_advance_ms': args.display_advance_ms,
              'metrics': {
                  'error_rates': 'Fractions of matched and independently timed rows.',
                  'reference_within_*ms': 'Fraction of all independently timed rows within each threshold, counting missing rows as failures.',
                  'preferred_range': 'From 500 ms before through 100 ms after the reference. Missing onsets count as failures.',
                  'late_ratio': 'Fraction of matched, independently timed rows shown after the reference.',
                  'display': 'Overlay timing after the configured lead is subtracted and clamped at the start of the track.',
                  'order_reversals': 'Non-increasing onsets. Invalid timelines are rejected and recorded as errors.',
                  'offset_correction': 'Subtract the median signed error separately for each track and engine.',
                  'wrong_repeat_onsets': 'Identical normalized lyric row within 500 ms of a different known occurrence and at least 2 s away from its own reference.',
                  'wrong_repeat_ratio': 'Wrong repeat onsets divided by all repeated reference rows.',
                  'worker_peak_rss_bytes': 'Peak resident bytes of the alignment process, excluding subprocesses.',
                  'sampled_worker_gpu_mib': 'Maximum process VRAM observed by nvidia-smi. Null when unavailable.',
                  'sampled_system_gpus': 'Maximum total VRAM and utilization per NVIDIA GPU observed while the worker ran. May include other processes.',
                  'alignment_seconds': 'Model load and alignment in a fresh process. Audio decoding is reported separately.',
              }}
    for case in cases:
        for engine in args.engines:
            run = run_case(case, engine, args)
            report['runs'].append(run)
            report['summary'] = summarize(report['runs'], args.engines)
            atomic_json(args.output, report)
            print(case['title'], engine, run.get('error', run.get('raw')), flush=True)
    if not cases:
        atomic_json(args.output, report)
    return int(not cases or any('error' in run for run in report['runs']))


if __name__ == '__main__':
    raise SystemExit(main())
