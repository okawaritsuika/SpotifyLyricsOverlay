"""Compare singer identity models on existing local audio and an archived gold run."""
import argparse
from collections import Counter
from itertools import combinations
import json
from pathlib import Path
import sys
from time import perf_counter
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from character_voice import SAMPLE_RATE, VoiceAnalyzer, VoiceAudio, _decode, _quality, _sha256


# The entire composition and all four versions of the test song are held out.
SPLIT = {
    'idol:天海春香': ((51, 123), 164, 798),
    'idol:如月千早': ((53, 117), 166, 807),
    'idol:星井美希': ((79, 144), 172, 816),
    'idol:高槻やよい': ((26, 120), 154, 834),
}
WINDOW_MS = 2400


def archived_escape(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = [row for row in data['rows'] if row['category'] in ('solo', 'duet') and row.get('scores')]
    candidates = tuple(data['references'])
    return {
        'source': str(path),
        'measurement': 'recomputed from archived scores; audio was not rerun',
        'model': data['model'],
        'device': data['device'],
        'original_seconds': data['elapsed_seconds'],
        'excluded_rows': data['unscored_rows'],
        'mixed_rows': data['gold_categories']['mixed'],
        'archived_thresholds': data['thresholds'],
        'metrics': metrics([
            (tuple(row['expected']), row['scores'], tuple(row['predicted']), row['status'] == 'estimated')
            for row in rows
        ], candidates),
        'historical_threshold_0_5_diagnostic': metrics([
            (tuple(row['expected']), row['scores'],
             tuple(name for name, score in row['scores'].items() if score is not None and score >= .5),
             any(score is not None and score >= .5 for score in row['scores'].values()))
            for row in rows
        ], candidates),
        'maximum_raw_score': max(score for row in rows for score in row['scores'].values() if score is not None),
    }


def fraction(count, total):
    return count / total if total else None


def metrics(scored, candidates):
    counts = Counter()
    by_count = {str(i): Counter() for i in range(len(candidates) + 1)}
    for truth, scores, prediction, decisive in scored:
        expected, predicted = set(truth), set(prediction)
        row = by_count[str(len(truth))]
        row['cases'] += 1
        row['decisive'] += bool(decisive)
        row['exact'] += bool(decisive and expected == predicted)
        row['matched_set'] += expected == predicted
        row['selected_multiple'] += len(predicted) > 1
        ranked = [(score, name) for name, score in scores.items() if score is not None]
        if len(truth) == 1:
            row['top1'] += bool(ranked and max(ranked)[1] in expected)
        counts['tp'] += len(predicted & expected)
        counts['fp'] += len(predicted - expected)
        counts['fn'] += len(expected - predicted)
        counts['tn'] += len(set(candidates) - expected - predicted)
        counts['complete'] += bool(decisive)
    tp, fp, fn, tn = (counts[k] for k in ('tp', 'fp', 'fn', 'tn'))
    return {
        'by_singer_count': {key: dict(value) for key, value in by_count.items() if value['cases']},
        'solo_top1': fraction(by_count['1']['top1'], by_count['1']['cases']),
        'solo_exact_match': fraction(by_count['1']['exact'], by_count['1']['cases']),
        'duet_exact_match': fraction(by_count['2']['exact'], by_count['2']['cases']),
        'solo_false_multiple': fraction(by_count['1']['selected_multiple'], by_count['1']['cases']),
        'precision': fraction(tp, tp + fp),
        'recall': fraction(tp, tp + fn),
        'f1': fraction(2 * tp, 2 * tp + fp + fn),
        'false_positive_rate': fraction(fp, fp + tn),
        'false_negative_rate': fraction(fn, fn + tp),
        'coverage': fraction(counts['complete'], len(scored)),
        'candidate_decisions': {'true_positive': tp, 'false_positive': fp, 'false_negative': fn, 'true_negative': tn},
    }


def load_sources(manifest, audio_dir):
    import av

    entries = {row['position']: row for row in json.loads(manifest.read_text(encoding='utf-8-sig'))['entries']}
    paths = {}
    facts = {}
    hashes = set()
    for candidate, (enrollment, calibration, test) in SPLIT.items():
        for position in (*enrollment, calibration, test):
            row = entries[position]
            if row['solo_credit']['character_id'] != candidate or len(row['artists']) != 1:
                raise ValueError(f'Invalid canonical solo credit for #{position}')
            paths_for_position = list(audio_dir.glob(f'{position:04d} *.mp3'))
            if len(paths_for_position) != 1:
                raise ValueError(f'Expected one local audio file for #{position}: {paths_for_position}')
            path, = paths_for_position
            with av.open(str(path)) as audio:
                duration = audio.duration / av.time_base
                tags = audio.metadata
            if (tags.get('artist') != row['artists'][0] or tags.get('title') != row['title']
                    or tags.get('album') != row['album'] or abs(duration - row['duration']) > 2):
                raise ValueError(f'Local audio metadata does not match #{position}: {path.name}')
            digest = _sha256(path)
            if digest in hashes:
                raise ValueError(f'Recording leaked across splits: {path.name}')
            hashes.add(digest)
            paths[position] = path
            facts[position] = {'filename': path.name, 'sha256': digest, 'track_id': row['track_id'],
                               'duration_s': round(duration, 3), 'candidate': candidate}
    return paths, facts


def accompaniment_offset(stereo, reference):
    from scipy.signal import correlate, resample_poly

    side = resample_poly(stereo[0, :18 * SAMPLE_RATE] - stereo[1, :18 * SAMPLE_RATE], 1, 16)
    needle = resample_poly(reference[0, :18 * SAMPLE_RATE] - reference[1, :18 * SAMPLE_RATE], 1, 16)
    rate = SAMPLE_RATE / 16
    length = int(10 * rate)
    start = int(4 * rate)
    needle = needle[start:start + length]
    correlation = correlate(side, needle, mode='valid', method='fft')
    best = int(np.argmax(correlation))
    denominator = np.linalg.norm(needle) * np.linalg.norm(side[best:best + length])
    coefficient = float(correlation[best] / denominator) if denominator > 0 else 0.
    if coefficient < .45:
        raise ValueError(f'Test arrangements do not align (normalized side-channel correlation {coefficient:.3f})')
    return (best - start) * 16, coefficient


def prepare(args, paths, facts):
    directory = args.output.parent / 'singing-benchmark-cache'
    directory.mkdir(parents=True, exist_ok=True)
    test_positions = [values[2] for values in SPLIT.values()]
    reference = _decode(paths[test_positions[0]], stereo=True)[:, :18 * SAMPLE_RATE].copy()
    decoder = VoiceAnalyzer(directory, models_dir=args.models_dir, device=args.device, singer_model='spectral')
    stems = {}
    for position, path in paths.items():
        stereo = _decode(path, stereo=True)
        offset, coefficient = accompaniment_offset(stereo, reference) if position in test_positions else (0, None)
        start = SAMPLE_RATE * 20 + offset
        end = start + SAMPLE_RATE * 18
        clip = directory / f'{facts[position]["sha256"]}-{start}-{end}.wav'
        if not clip.is_file():
            samples = (np.clip(stereo[:, start:end], -1., 1.) * 32767).astype('<i2')
            with wave.open(str(clip), 'wb') as wav:
                wav.setparams((2, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
                wav.writeframes(samples.T.tobytes())
        started = perf_counter()
        stems[position] = decoder.prepare(clip)
        facts[position].update(clip_start_s=start / SAMPLE_RATE, clip_end_s=end / SAMPLE_RATE,
                               accompaniment_correlation=coefficient, prepare_s=perf_counter() - started)
        print(f'#{position} stem {facts[position]["prepare_s"]:.2f}s, lag {offset / SAMPLE_RATE:+.4f}s', flush=True)
    return stems


def pitch_contour(samples):
    """Estimate periodic pitch for labeling test mixtures independently of identity models."""
    from scipy.signal import find_peaks, resample_poly

    rate = SAMPLE_RATE / 4
    down = resample_poly(samples, 1, 4)
    frame_length = int(rate * .055)
    frames = np.lib.stride_tricks.sliding_window_view(down, frame_length)[::int(rate * .020)]
    lag_min, lag_max = int(rate / 900), int(rate / 110)
    output = np.full(len(frames), np.nan)
    for index, frame in enumerate(frames):
        frame = frame - frame.mean()
        if np.sqrt(np.mean(frame ** 2)) < .002:
            continue
        spectrum = np.fft.rfft(frame, n=2048)
        acf = np.fft.irfft(np.abs(spectrum) ** 2)[:lag_max + 1]
        sums = np.r_[0., np.cumsum(frame ** 2)]
        lags = np.arange(lag_max + 1)
        normalizer = np.sqrt(sums[frame_length - lags] * (sums[-1] - sums[lags]))
        acf = acf / np.maximum(normalizer, 1e-9)
        peaks, _ = find_peaks(acf[lag_min:], height=.65)
        if len(peaks):
            peaks += lag_min
            lag = peaks[acf[peaks] >= acf[peaks].max() * .9][0]
            output[index] = 1200 * np.log2(rate / lag)
    return output


def excerpt(stem, start_ms):
    start = start_ms * SAMPLE_RATE // 1000
    stop = (start_ms + WINDOW_MS) * SAMPLE_RATE // 1000
    samples = stem.samples[start:stop]
    quality = _quality(samples)
    if quality['active_fraction'] < .75 or quality['tonal_fraction'] < .60 or quality['rms_dbfs'] < -38:
        return None
    return samples * (.04 / np.sqrt(np.mean(samples ** 2)))


def transpose_third(samples):
    """Synthesize a just major third above the held-out melody at the same tempo."""
    import torch
    from torchaudio.functional import phase_vocoder, resample

    waveform = torch.from_numpy(samples)
    window = torch.hann_window(1024)
    spectrum = torch.stft(waveform, 1024, hop_length=256, window=window, return_complex=True)
    advance = torch.linspace(0, torch.pi * 256, spectrum.shape[0])[:, None]
    stretched = phase_vocoder(spectrum, .8, advance)
    stretched_audio = torch.istft(stretched, 1024, hop_length=256, window=window, length=len(samples) * 5 // 4)
    audio = resample(stretched_audio, orig_freq=5, new_freq=4).numpy()[:len(samples)]
    return audio * (.04 / np.sqrt(np.mean(audio ** 2)))


def probes(stems, *, test, harmony=False):
    cases = []
    candidates = tuple(SPLIT)
    labels = ('same_melody', 'shifted_melody') if test else ('different_compositions',)
    for label in labels:
        for start in range(0, 13001, WINDOW_MS):
            samples = {}
            for index, name in enumerate(candidates):
                position = SPLIT[name][2 if test else 1]
                shift = index * 620 if label == 'shifted_melody' else 0
                samples[name] = excerpt(stems[position], start + shift)
            active = [name for name in candidates if samples[name] is not None]
            contours = {name: pitch_contour(samples[name]) for name in active} if test else {}
            for size in range(1, len(active) + 1):
                if size == 1 and label == 'shifted_melody':
                    continue
                for singers in combinations(active, size):
                    mixture = np.sum([samples[name] for name in singers], axis=0, dtype=np.float32) / np.sqrt(np.float32(size))
                    pitch_agreement = None
                    if test and size > 1:
                        pitches = np.stack([contours[name] for name in singers])
                        jointly_voiced = np.isfinite(pitches).all(axis=0)
                        if np.mean(jointly_voiced) >= .2:
                            pitch_agreement = float(np.mean(np.ptp(pitches[:, jointly_voiced], axis=0) <= 65))
                    cases.append((singers, mixture, label, start, pitch_agreement))
                    if harmony and label == 'same_melody' and size == 2 and (pitch_agreement or 0.) >= .75:
                        a, b = singers
                        shifted = transpose_third(samples[b])
                        interval = pitch_contour(shifted) - contours[a]
                        jointly_voiced = np.isfinite(interval)
                        fraction_near_third = (float(np.mean(np.abs(interval[jointly_voiced] - 386) <= 85))
                                               if jointly_voiced.any() else None)
                        third_mix = (samples[a] + shifted) / np.sqrt(np.float32(2))
                        cases.append((singers, third_mix, 'synthetic_major_third', start, fraction_near_third))
    silence = np.zeros(WINDOW_MS * SAMPLE_RATE // 1000, dtype=np.float32)
    cases.append(((), silence, 'silence', 0, None))
    return cases


def score(analyzer, profiles, cases):
    return [analyzer.score(VoiceAudio(samples, 'singing', 'benchmark-controlled-mixture'),
                           ranges=[(0, WINDOW_MS)], profiles=profiles)[0]['scores']
            for _, samples, _, _, _ in cases]


def calibrate(cases, scored, candidates=SPLIT):
    thresholds = {}
    for name in candidates:
        positive, negative = [], []
        for case, scores in zip(cases, scored, strict=True):
            value = scores[name]
            if value is not None:
                (positive if name in case[0] else negative).append(value)
        if positive and negative:
            edges = sorted(set([-1., *positive, *negative, 1.]))
            choices = [(a + b) / 2 for a, b in zip(edges, edges[1:])]
            # Balanced accuracy on separate songs, with ties resolved toward fewer false positives.
            thresholds[name] = max(choices, key=lambda threshold: (
                fraction(sum(v > threshold for v in positive), len(positive))
                + fraction(sum(v <= threshold for v in negative), len(negative)), threshold))
    return thresholds


def evaluate(cases, scored, thresholds, margin):
    results = []
    for (truth, _, label, start, pitch_agreement), scores in zip(cases, scored, strict=True):
        selected = tuple(name for name, value in scores.items()
                         if value is not None and name in thresholds and value > thresholds[name] + margin)
        decisive = bool(selected) and all(value is not None and name in thresholds
                                          and abs(value - thresholds[name]) > margin for name, value in scores.items())
        if label == 'silence' and not any(value is not None for value in scores.values()):
            decisive = True
        results.append({'truth': truth, 'scores': scores, 'predicted': selected, 'decisive': decisive,
                        'scenario': label, 'start_ms': start, 'joint_pitch_agreement': pitch_agreement})
    groups = {
        'all': results,
        'same_melody': [row for row in results if row['scenario'] == 'same_melody'],
        'verified_unison': [row for row in results if row['scenario'] == 'same_melody'
                            and (row['joint_pitch_agreement'] or 0.) >= .75],
        'shifted_melody': [row for row in results if row['scenario'] == 'shifted_melody'],
    }
    if any(row['scenario'] == 'synthetic_major_third' for row in results):
        groups['synthetic_major_third'] = [row for row in results if row['scenario'] == 'synthetic_major_third']
    return {'metrics': {key: metrics([(row['truth'], row['scores'], row['predicted'], row['decisive']) for row in rows], SPLIT)
                        for key, rows in groups.items()}, 'rows': results}


def matched_coverage(variants):
    ranked = {}
    for name, variant in variants.items():
        if 'rows' not in variant:
            continue
        thresholds = variant['thresholds']
        decisive = [row for row in variant['rows'] if row['decisive'] and row['scenario'] != 'silence']
        ranked[name] = sorted(decisive, key=lambda row: min(abs(score - thresholds[singer])
                                                           for singer, score in row['scores'].items()), reverse=True)
    if not ranked:
        return {}
    common = min(map(len, ranked.values()))
    return {'common_decisive_vocal_cases': common, 'variants': {
        name: metrics([(row['truth'], row['scores'], row['predicted'], True) for row in rows[:common]], SPLIT)
        for name, rows in ranked.items()
    }}


def normalize_scores(scores, calibration):
    stats = {}
    for name in calibration[0] if calibration else ():
        reference = [row[name] for row in calibration if row[name] is not None]
        if reference:
            center = float(np.median(reference))
            scale = max(float(np.median(np.abs(np.asarray(reference) - center))), 1e-6)
            stats[name] = (center, scale)
    return [{name: float(np.tanh((value - stats[name][0]) / (3 * stats[name][1])))
             if value is not None and name in stats else None for name, value in row.items()} for row in scores]


def combine(left, right, left_cal, right_cal):
    return [{name: (value + b[name]) / 2 if value is not None and b[name] is not None else None
             for name, value in a.items()}
            for a, b in zip(normalize_scores(left, left_cal), normalize_scores(right, right_cal), strict=True)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'output/playlist-youtube/idolmaster-solo-links.json')
    parser.add_argument('--audio-dir', type=Path, default=ROOT / 'output/mp3')
    parser.add_argument('--escape-archive', type=Path, default=ROOT / 'output/voice-models/escape-gold-parts-test.json')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models/character-voice')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--archive-only', action='store_true')
    parser.add_argument('--harmony', action='store_true', help='Also test phase-vocoder major thirds of verified unison pairs')
    parser.add_argument('--backend', action='append', choices=('ecapa', 'sony', 'sony-contrastive'))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')

    results = {'escape_archived_ecapa': archived_escape(args.escape_archive)}
    save()
    if args.archive_only:
        return
    paths, facts = load_sources(args.manifest, args.audio_dir)
    started = perf_counter()
    stems = prepare(args, paths, facts)
    calibration = probes(stems, test=False)
    test = probes(stems, test=True, harmony=args.harmony)
    results['held_out_local'] = {'device_requested': args.device, 'audio_sources': facts, 'splits': SPLIT,
                                 'preparation_and_mixing_s': perf_counter() - started,
                                 'calibration_cases': len(calibration), 'test_cases': len(test), 'variants': {}}
    variants = results['held_out_local']['variants']
    save()
    for backend in args.backend or ('sony', 'sony-contrastive', 'ecapa'):
        analyzer = VoiceAnalyzer(args.output.parent / 'singing-benchmark-cache', device=args.device,
                                 singer_model=backend, models_dir=args.models_dir, aggregation='max')
        try:
            started = perf_counter()
            profiles = {name: [{**example, 'audio_sha256': facts[position]['sha256']}
                               for position in positions for example in analyzer.enroll(stems[position])]
                        for name, (positions, _, _) in SPLIT.items()}
        except (ImportError, FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            variants[backend] = {'error': f'{type(error).__name__}: {error}'}
            save()
            continue
        enrollment_s = perf_counter() - started
        aggregations = {'ecapa': ('max',), 'sony': ('max', 'topk', 'mean'), 'sony-contrastive': ('max', 'topk')}
        for aggregation in aggregations[backend]:
            analyzer.aggregation = aggregation
            started = perf_counter()
            cal_scores = score(analyzer, profiles, calibration)
            thresholds = calibrate(calibration, cal_scores)
            calibration_s = perf_counter() - started
            started = perf_counter()
            test_scores = score(analyzer, profiles, test)
            evaluation_s = perf_counter() - started
            key = f'{backend}:{aggregation}'
            variants[key] = {'model_id': analyzer.model_id('singing'), 'actual_device': analyzer.actual_device,
                             'exemplars': {k: len(v) for k, v in profiles.items()},
                             'enrollment_s': enrollment_s, 'calibration_s': calibration_s, 'evaluation_s': evaluation_s,
                             'thresholds': thresholds, 'margin': .025, **evaluate(test, test_scores, thresholds, .025),
                             'calibration_scores': cal_scores}
            print(key, variants[key]['metrics']['all']['by_singer_count'], flush=True)
            save()
    if 'sony:topk' in variants and 'ecapa:max' in variants:
        left, right = variants['sony:topk'], variants['ecapa:max']
        left_cal, right_cal = left['calibration_scores'], right['calibration_scores']
        thresholds = calibrate(calibration, combine(left_cal, right_cal, left_cal, right_cal))
        test_scores = combine([row['scores'] for row in left['rows']], [row['scores'] for row in right['rows']],
                              left_cal, right_cal)
        variants['sony-topk+ecapa-max'] = {'model_ids': [left['model_id'], right['model_id']], 'thresholds': thresholds,
                                          'margin': .025, **evaluate(test, test_scores, thresholds, .025)}
    results['held_out_local']['matched_coverage'] = matched_coverage(variants)
    save()


if __name__ == '__main__':
    main()
