"""Compare RomajiASR profiles on shared emissions and optional HDemucs stems."""

import argparse
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from benchmark_japanese_alignment import (
    ROOT, NO_WINDOW, gpu_samples, load_case, measure, peak_rss_bytes, read_json, summarize,
)
from japanese_alignment import CTC, PROFILES, align_japanese, ctc_emissions
from storage import Store, atomic_json


def separate(path, model_dir, device):
    from character_voice import SAMPLE_RATE, VoiceAnalyzer, _decode
    from scipy.signal import resample_poly
    import torch

    torch.set_num_threads(4)
    analyzer = VoiceAnalyzer(model_dir, models_dir=model_dir, device=device, singer_model='spectral')
    analyzer.actual_device = device
    vocals = analyzer._separate(_decode(Path(path), stereo=True))
    assert SAMPLE_RATE == 44100
    return resample_poly(vocals, 160, 441)


def compare_stem(mix, stem):
    times = list(mix['times'])
    details = [dict(row) for row in mix['line_alignment']]
    for i, (original, alternative, row) in enumerate(zip(times, stem['times'], stem['line_alignment'])):
        if alternative is None or details[i]['quality'] == 'high':
            continue
        left = next((value for value in reversed(times[:i]) if value is not None), -1)
        right = next((value for value in times[i+1:] if value is not None), mix['duration'])
        # Require two neighbors for an added onset. Existing low-quality
        # placements can move only with nearby support and better phone coverage.
        if not left < alternative < right:
            continue
        if original is None:
            if left < 0 or right >= mix['duration'] or right - left > 15 or row['quality'] != 'high':
                continue
        elif (abs(original - alternative) > .35 or row.get('phone_coverage', 0)
              <= details[i].get('phone_coverage', 0)):
            continue
        times[i] = alternative
        details[i] = {**row, 'stem_selected': True}
    return {**mix, 'times': times, 'line_alignment': details}


def run_profiles(payload):
    from faster_whisper.audio import decode_audio

    request = payload['request']
    samples = decode_audio(payload['audio'], sampling_rate=16000)
    if payload['tail']:
        samples = samples[:-int(payload['tail'] * 16000)]
    inference_time = 0.

    @lru_cache(maxsize=1)
    def infer_mix(model):
        nonlocal inference_time
        started = perf_counter()
        result = ctc_emissions(model, samples)
        inference_time = perf_counter() - started
        return result

    results = {}
    def align(profile, audio, infer):
        start = perf_counter()
        result = align_japanese({**request, 'romaji_profile': profile}, audio, payload['start'],
                                lambda *_: None, infer=infer)
        return {**result, 'alignment_seconds': perf_counter() - start}

    with gpu_samples(request['romaji_device'] != 'cpu') as gpu:
        for profile in payload['profiles']:
            results[profile] = align(profile, samples, lambda model, _: infer_mix(model))
        if payload.get('stems'):
            profile = payload['profiles'][-1]
            try:
                start = perf_counter()
                vocals = separate(payload['audio'], Path(payload['stem_model_dir']), payload['stem_device'])
                stem = align(profile, vocals[:len(samples)], None)
                stem['separation_seconds'] = perf_counter() - start - stem['alignment_seconds']
                results['stem'] = stem
                results['selective_stem'] = compare_stem(results[profile], stem)
            except (ImportError, ValueError, OSError, RuntimeError) as error:
                results['stem'] = results['selective_stem'] = {'error': str(error)}

    return {'profiles': results, 'shared_mix_inference_seconds': inference_time,
            'worker_peak_rss_bytes': peak_rss_bytes(),
            'sampled_worker_gpu_mib': max(gpu['process_mib'], default=None),
            'sampled_system_gpus': gpu['devices']}


def main():
    if sys.argv[1:] == ['--worker']:
        print(json.dumps(run_profiles(json.load(sys.stdin)), allow_nan=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--track', action='append')
    parser.add_argument('--split', choices=('calibration', 'evaluation'))
    parser.add_argument('--device', choices=('cpu', 'cuda', 'dml', 'auto'), default='cpu')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES, default=list(PROFILES))
    parser.add_argument('--stems', action='store_true')
    parser.add_argument('--ctc-diagnostics', action='store_true')
    parser.add_argument('--stem-device', choices=('cpu', 'cuda'), default='cpu')
    args = parser.parse_args()
    entries = read_json(args.manifest)['tracks']
    entries = [entry for entry in entries if (not args.track or entry['song_id'] in args.track)
               and (not args.split or entry['split'] == args.split)]
    store = Store(args.data)
    songs = {song.id: song for song in store.library()} if any('capture' in entry for entry in entries) else {}
    profiles = args.profiles + (['stem', 'selective_stem'] if args.stems else [])
    report = {'runs': [], 'excluded_tracks': []}
    for entry in entries:
        try:
            case = load_case(entry, args.manifest.resolve().parent, store, songs)
        except (OSError, ValueError, KeyError) as error:
            report['excluded_tracks'].append({'id': entry['song_id'], 'error': str(error)})
            continue
        # Pass only inputs available during an app run. References stay here.
        payload = {'audio': case['audio'], 'start': case['start'], 'tail': case['tail'],
                   'request': {'lines': case['lines'], 'duration': case['duration'],
                               'alignment_engine': CTC, 'romaji_device': args.device,
                               'ctc_diagnostics': args.ctc_diagnostics,
                               'romaji_model_dir': str(ROOT / 'models/romaji-asr')},
                   'profiles': args.profiles, 'stems': args.stems, 'stem_device': args.stem_device,
                   'stem_model_dir': str(ROOT / 'models/character-voice')}
        try:
            process = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--worker'],
                                     input=json.dumps(payload), capture_output=True, text=True,
                                     encoding='utf-8', errors='replace', creationflags=NO_WINDOW, timeout=1200)
            if process.returncode:
                raise ValueError(process.stderr.strip()[-3000:])
            result = json.loads(process.stdout.splitlines()[-1])
            variants = result.pop('profiles')
        except (OSError, IndexError, ValueError, subprocess.TimeoutExpired) as error:
            variants = {name: {'error': str(error)} for name in profiles}
            result = {}
        for name, variant in variants.items():
            run = {'id': case['id'], 'title': case['title'], 'split': case['split'],
                   'audio': case['audio'], 'engine': name, **result}
            if 'error' in variant:
                run['error'] = variant['error']
            else:
                run.update(measure(case, variant), runtime=variant['runtime'],
                           alignment_seconds=round(variant['alignment_seconds'], 3))
                if 'separation_seconds' in variant:
                    run['separation_seconds'] = round(variant['separation_seconds'], 3)
            report['runs'].append(run)
            print(case['id'], name, run.get('reference_within_500ms', run.get('error')), flush=True)
        report['summary'] = summarize(report['runs'], profiles)
        atomic_json(args.output, report)
    atomic_json(args.output, report)
    return int(not report['runs'] or report['excluded_tracks'] or any('error' in run for run in report['runs']))


if __name__ == '__main__':
    raise SystemExit(main())
