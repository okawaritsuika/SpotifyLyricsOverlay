"""Evaluate optional singing separators on held-out local solo and mixed vocal regions."""
import argparse
from collections import defaultdict
from itertools import combinations, permutations
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import numpy as np

from benchmark_singing_voice import (ROOT, SAMPLE_RATE, SPLIT, VoiceAnalyzer, VoiceAudio, WINDOW_MS,
                                     _quality, calibrate, evaluate, excerpt, load_sources,
                                     matched_coverage, metrics, prepare, probes)
from benchmark_escape_voice import CANDIDATES as ESCAPE_CANDIDATES, enrollment, reference_stems as load_escape_stems, verify_audio
from singing_voice_separation import SingerInformedSeparator, UnmixxSeparator


def power(samples):
    return float(np.mean(np.square(samples)))


def si_sdr(estimate, target):
    estimate = estimate - estimate.mean()
    target = target - target.mean()
    projected = target * (np.dot(estimate, target) / max(np.dot(target, target), 1e-12))
    return float(10 * np.log10(max(power(projected), 1e-12) / max(power(estimate - projected), 1e-12)))


def solo_enrollment(stems, separator):
    enrollments = {}
    for singer, (positions, _, _) in SPLIT.items():
        windows = [stem.samples[start:start + 3 * SAMPLE_RATE]
                   for stem in (stems[position] for position in positions)
                   for start in range(0, len(stem.samples) - 3 * SAMPLE_RATE + 1, 2 * SAMPLE_RATE)]
        reference = max(windows, key=lambda samples: (_quality(samples)['active_fraction'], power(samples)))
        enrollments[singer] = separator.enroll(reference)
    return enrollments


def profile_scores(analyzer, profiles, samples):
    result = analyzer.score(VoiceAudio(samples, 'singing', 'separation-benchmark'),
                            ranges=[(0, WINDOW_MS)], profiles=profiles)
    return result[0]['scores']


def reference_stems(stems, case, *, test):
    truth, _, label, start, _ = case
    shifts = {name: index * 620 for index, name in enumerate(SPLIT)} if label == 'shifted_melody' else {}
    return {name: excerpt(stems[SPLIT[name][2 if test else 1]], start + shifts.get(name, 0)) / np.sqrt(len(truth))
            for name in truth}


def strongest_margin(scores, singer):
    if scores[singer] is None:
        return 0.
    return scores[singer] - max((value for name, value in scores.items() if name != singer and value is not None),
                                default=scores[singer])


def optional_features(method, separator, analyzer, profiles, enrollments, case, reference):
    truth, mixture, _, _, _ = case
    scores = profile_scores(analyzer, profiles, mixture)
    if not truth:
        return {'mixture': scores, 'separated': scores, 'acoustics': {}}
    mixture_power = max(power(mixture), 1e-12)
    if method == 'unmixx':
        estimates = separator.separate(mixture)
        scored = [profile_scores(analyzer, profiles, stem) for stem in estimates]
        stems = sorted(zip((power(stem) / mixture_power for stem in estimates), scored, strict=True),
                       key=lambda item: item[0], reverse=True)
        separable = 0 < len(reference) <= 2
        sdr = (max(sum(si_sdr(estimates[index], target) for index, target in zip(order, reference.values()))
                   for order in permutations(range(2), len(reference))) / len(reference) if separable else None)
        baseline = float(np.mean([si_sdr(mixture, target) for target in reference.values()])) if separable else None
        return {'mixture': scores,
                'separated': {singer: max((row[singer] for row in scored if row[singer] is not None), default=None)
                              for singer in profiles},
                'stems': stems,
                'acoustics': {'relative_output_energy': [energy for energy, _ in reversed(stems)],
                              'reconstruction_error': power(mixture - estimates.sum(axis=0)) / mixture_power,
                              'si_sdri_db': sdr - baseline if separable else None}}

    separated = {}
    acoustics = {}
    features = {}
    for singer, enrollment in enrollments.items():
        target, residual = separator.extract(mixture, enrollment)
        target_scores = profile_scores(analyzer, profiles, target)
        residual_scores = profile_scores(analyzer, profiles, residual)
        energy = power(target) / mixture_power
        gain = 1 - power(residual) / mixture_power
        separated[singer] = target_scores[singer]
        acoustics[singer] = {'relative_output_energy': energy, 'residual_gain': gain,
                             'si_sdri_db': (si_sdr(target, reference[singer]) - si_sdr(mixture, reference[singer])
                                            if singer in reference else None)}
        if target_scores[singer] is not None:
            features[singer] = [target_scores[singer], scores[singer] or 0., strongest_margin(target_scores, singer),
                                target_scores[singer] - (residual_scores[singer] or 0.), energy, gain]
    return {'mixture': scores, 'separated': separated, 'acoustics': acoustics, 'features': features}


def calibrated_film(calibration, evaluation, results_cal, results_test, candidates=SPLIT):
    from scipy.special import expit
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    pairs = [(vector, name in case[0]) for case, result in zip(calibration, results_cal, strict=True)
             for name, vector in result.get('features', {}).items()]
    if len({label for _, label in pairs}) < 2:
        return None
    vectors, labels = zip(*pairs)
    scaler = StandardScaler().fit(vectors)
    predictor = LogisticRegression(class_weight='balanced', random_state=0).fit(scaler.transform(vectors), labels)

    def predict(results):
        scored = []
        for row in results:
            features = row.get('features', {})
            scores = dict.fromkeys(candidates)
            if features:
                vectors = scaler.transform(list(features.values()))
                scores.update(zip(features, expit(predictor.decision_function(vectors)).tolist(), strict=True))
            scored.append(scores)
        return scored

    thresholds = calibrate(calibration, predict(results_cal), candidates)
    return thresholds, assess(evaluation, predict(results_test), thresholds, candidates)


def energy_ratio(result):
    primary, secondary = result['stems']
    return secondary[0] / max(primary[0], 1e-12)


def energy_sweep(rows):
    values = sorted({0., *(ratio for ratio, _ in rows), 1.})
    thresholds = sorted({0., 1., *((left + right) / 2 for left, right in zip(values, values[1:]))})
    solo = [ratio for ratio, count in rows if count == 1]
    duet = [ratio for ratio, count in rows if count == 2]
    output = []
    for cutoff in thresholds:
        false_duet = sum(ratio >= cutoff for ratio in solo)
        true_duet = sum(ratio >= cutoff for ratio in duet)
        output.append({'cutoff': cutoff, 'solo': len(solo), 'duet': len(duet),
                       'solo_predicted_duet': false_duet, 'duet_predicted_duet': true_duet,
                       'balanced_accuracy': .5 * ((1 - false_duet / len(solo)) + true_duet / len(duet))})
    return output


def best_energy_cutoff(rows):
    return max(energy_sweep(rows), key=lambda row: (row['balanced_accuracy'], row['cutoff']))['cutoff']


def gated_rows(cases, results, thresholds, energy_cutoff, cosine_offset, margin_cutoff):
    rows = []
    for (truth, _, label, _, agreement), result in zip(cases, results, strict=True):
        selected = set()
        confidence = []
        if result.get('stems'):
            eligible = result['stems'] if energy_ratio(result) >= energy_cutoff else result['stems'][:1]
            for _, scores in eligible:
                for name, score in scores.items():
                    # VoiceAnalyzer has already screened activity, tone and signal quality.
                    if score is None or name not in thresholds:
                        continue
                    gap = min(score - (thresholds[name] + cosine_offset),
                              strongest_margin(scores, name) - margin_cutoff)
                    if gap > 0:
                        selected.add(name)
                        confidence.append(gap)
            decisive = len(selected) == len(eligible)
        else:
            decisive = label == 'silence'
        rows.append({'truth': truth, 'scores': result['separated'], 'predicted': tuple(sorted(selected)),
                     'decisive': decisive, 'confidence': min(confidence, default=0.),
                     'scenario': label, 'joint_pitch_agreement': agreement})
    return rows


def rows_metrics(rows, candidates=SPLIT):
    return metrics([(row['truth'], row['scores'], row['predicted'], row['decisive']) for row in rows], candidates)


def assess(cases, scores, thresholds, candidates):
    assessment = evaluate(cases, scores, thresholds, .025)
    if candidates is not SPLIT:
        assessment['metrics'] = {'all': rows_metrics(assessment['rows'], candidates)}
    return assessment


def equal_coverage(assessments, candidates, ranked=None):
    ranked = {} if ranked is None else dict(ranked)
    for name, assessment in assessments.items():
        ranked[name] = sorted((row for row in assessment['rows'] if row['decisive'] and row['scenario'] != 'silence'),
                              key=lambda row: min(abs(value - assessment['thresholds'][singer])
                                                  for singer, value in row['scores'].items()), reverse=True)
    common = min(map(len, ranked.values()))
    return {'common_decisive_vocal_cases': common,
            'variants': {name: rows_metrics(items[:common], candidates) for name, items in ranked.items()}}


def calibrated_unmixx(calibration, evaluation, results_cal, results_test, mixture, separated, candidates=SPLIT):
    calibration_ratios = [(energy_ratio(result), len(case[0])) for case, result in zip(calibration, results_cal, strict=True)
                          if len(case[0]) in (1, 2)]
    energy_cutoff = best_energy_cutoff(calibration_ratios)
    thresholds = separated['thresholds']
    sweep = []
    for offset in (-.10, -.075, -.05, -.025, 0., .025, .05, .075, .10):
        for margin in (0., .02, .04, .06, .08, .10):
            fitted = rows_metrics(gated_rows(calibration, results_cal, thresholds, energy_cutoff, offset, margin), candidates)
            evaluated = rows_metrics(gated_rows(evaluation, results_test, thresholds, energy_cutoff, offset, margin), candidates)
            sweep.append({'cosine_offset': offset, 'identity_margin': margin,
                          'calibration': fitted, 'held_out': evaluated})
    best = max(sweep, key=lambda row: (row['calibration']['solo_exact_match'] + row['calibration']['duet_exact_match'],
                                      -row['calibration']['false_positive_rate'], row['calibration']['f1']))
    rows = gated_rows(evaluation, results_test, thresholds, energy_cutoff, best['cosine_offset'], best['identity_margin'])
    gated = sorted((row for row in rows if row['decisive'] and row['scenario'] != 'silence'),
                   key=lambda row: row['confidence'], reverse=True)
    return {'energy_cutoff': energy_cutoff, 'selected_from_calibration': best, 'sweep': sweep,
            'matched_coverage': equal_coverage({'mixture': mixture, 'separated': separated}, candidates, {'gated': gated}),
            'verified_unison': rows_metrics([row for row in rows if (row['joint_pitch_agreement'] or 0.) >= .75], candidates),
            'decisions': rows}


def summarize_acoustics(method, cases, results):
    groups = defaultdict(list)
    for (truth, _, label, _, agreement), result in zip(cases, results, strict=True):
        if not truth:
            continue
        group = f'{len(truth)} singers'
        labels = [group, 'unison'] if agreement is not None and agreement >= .75 else [group, label]
        if method == 'unmixx':
            data = result['acoustics']
            for name in labels:
                groups[name].append(data)
        else:
            for singer, data in result['acoustics'].items():
                membership = 'present' if singer in truth else 'absent'
                for name in labels:
                    groups[f'{name}, {membership}'].append(data)
                groups[membership].append(data)
    summary = {}
    for key, rows in groups.items():
        fields = ('si_sdri_db', 'relative_output_energy', 'residual_gain', 'reconstruction_error')
        summary[key] = {'count': len(rows)}
        for field in fields:
            values = [row[field] for row in rows if row.get(field) is not None]
            if values:
                summary[key][f'median_{field}'] = np.median(values, axis=0).tolist()
        if method == 'singer-informed':
            summary[key]['extractions_above_minus_40_db'] = sum(row['relative_output_energy'] > 1e-4 for row in rows)
    return summary


def escape_regions(audio, gold):
    for row in gold['rows']:
        if row['category'] not in ('solo', 'duet') or not row.get('scores'):
            continue
        start, end = row['start_ms'], min(row['end_ms'], audio.duration_ms)
        if end - start > WINDOW_MS:
            start += (end - start - WINDOW_MS) // 2
            end = start + WINDOW_MS
        mixture = audio.samples[start * SAMPLE_RATE // 1000:end * SAMPLE_RATE // 1000]
        yield row, start, end, mixture


def escape_blind(args):
    """Measure secondary output on known solo and duet regions without singer enrollments."""
    gold = json.loads(args.escape_gold.read_text(encoding='utf-8-sig'))
    analyzer = VoiceAnalyzer(ROOT / 'data/voice_cache', models_dir=args.models_dir, device=args.device,
                             singer_model='spectral')
    started = perf_counter()
    audio = analyzer.prepare(args.escape_audio)
    preparation_seconds = perf_counter() - started
    separator = UnmixxSeparator(args.models_dir, device=args.device)
    rows = []
    started = perf_counter()
    for row, start, end, mixture in escape_regions(audio, gold):
        stems = separator.separate(mixture)
        secondary, primary = sorted(power(stem) for stem in stems)
        rows.append({'line': row['line'], 'category': row['category'], 'start_ms': start, 'end_ms': end,
                     'secondary_over_primary': secondary / max(primary, 1e-12),
                     'reconstruction_error': power(mixture - stems.sum(axis=0)) / max(power(mixture), 1e-12)})
    seconds = perf_counter() - started
    counts = {}
    for category in ('solo', 'duet'):
        ratios = [row['secondary_over_primary'] for row in rows if row['category'] == category]
        counts[category] = {'count': len(ratios), 'median_secondary_over_primary': float(np.median(ratios)),
                            'secondary_above_minus_20_db': sum(ratio >= .01 for ratio in ratios)}
    posthoc = energy_sweep([(row['secondary_over_primary'], 1 if row['category'] == 'solo' else 2) for row in rows])
    return {'source': str(args.escape_audio), 'source_hash': audio.source_hash, 'duration_ms': audio.duration_ms,
            'preparation': audio.preparation_id, 'preparation_seconds': preparation_seconds,
            'model': separator.version, 'device': separator.device, 'inference_seconds': seconds,
            'measurement': 'blind output energy and reconstruction diagnostics only', 'summary': counts, 'rows': rows,
            'posthoc_same_recording_energy_sweep': posthoc}


def escape_calibration(analyzer, gold, stems):
    if set(gold['references']) != set(ESCAPE_CANDIDATES):
        raise ValueError('Escape references must include both gold singers')
    profiles, solo_clips, film_references = {}, {}, {}
    for name, refs in gold['references'].items():
        position = refs['enroll']
        start, end = (1000 * t for t in gold['sources'][str(position)]['clip'])
        profiles[name] = enrollment(analyzer, stems[position], [(start, end)])
        windows = [stems[position].samples[t * SAMPLE_RATE // 1000:(t + 3000) * SAMPLE_RATE // 1000]
                   for t in range(start, end - 2999, 2000)]
        film_references[name] = max(windows, key=lambda clip: (_quality(clip)['active_fraction'], power(clip)))
        position = refs['calibrate']
        start, end = (1000 * t for t in gold['sources'][str(position)]['clip'])
        solo_clips[name] = [clip for t in range(start, end - WINDOW_MS + 1, WINDOW_MS)
                            if (clip := excerpt(stems[position], t)) is not None]
    if not all(profiles.values()) or not all(solo_clips.values()):
        raise ValueError('Each Escape singer needs quality-passing independent enrollment and calibration windows')
    return profiles, film_references, solo_clips


def escape_offcast(args, analyzer, profiles, film_references, solo_clips, known_hashes):
    paths, facts = load_sources(args.manifest, args.audio_dir)
    names = ('idol:天海春香', 'idol:如月千早')
    positions = [position for name in names for position in (*SPLIT[name][0], SPLIT[name][1])]
    sources = {position: facts[position] for position in positions}
    if any(source['sha256'] in known_hashes for source in sources.values()):
        raise ValueError('Off-cast references must come from different recordings')
    stems = prepare(args, paths, facts)
    for name in names:
        enrolled, calibration, _ = SPLIT[name]
        profiles[name] = [sample for position in enrolled for sample in enrollment(analyzer, stems[position], None)]
        windows = [stems[position].samples[start:start + 3 * SAMPLE_RATE] for position in enrolled
                   for start in range(0, len(stems[position].samples) - 3 * SAMPLE_RATE + 1, 2 * SAMPLE_RATE)]
        film_references[name] = max(windows, key=lambda clip: (_quality(clip)['active_fraction'], power(clip)))
        solo_clips[name] = [clip for t in range(0, stems[calibration].duration_ms - WINDOW_MS + 1, WINDOW_MS)
                            if (clip := excerpt(stems[calibration], t)) is not None]
        if not profiles[name] or not solo_clips[name]:
            raise ValueError(f'Missing quality-passing off-cast windows for {name}')
    return names, sources


def calibration_mixtures(solo_clips):
    cases = [((name,), clip, 'independent_solo', index * WINDOW_MS, None)
             for name, clips in solo_clips.items() for index, clip in enumerate(clips)]
    for a, b in combinations(solo_clips, 2):
        cases.extend(((a, b), (left + right) / np.sqrt(np.float32(2)),
                      'synthetic_independent_duet', index * WINDOW_MS, None)
                     for index, (left, right) in enumerate(zip(solo_clips[a], solo_clips[b])))
    return cases


def named_decisions(assessment, regions):
    return [{**decision, 'line': gold['line'], 'start_ms': start, 'end_ms': end}
            for decision, (gold, start, end, _) in zip(assessment['rows'], regions, strict=True)]


def negative_probes(rows, candidates):
    return {name: {'absent_probes': sum(name not in row['truth'] for row in rows),
                   'false_positives': sum(name not in row['truth'] and name in row['predicted'] for row in rows),
                   'positive_probes': sum(name in row['truth'] for row in rows),
                   'false_negatives': sum(name in row['truth'] and name not in row['predicted'] for row in rows)}
            for name in candidates}


def escape_named(args):
    gold = json.loads(args.escape_gold.read_text(encoding='utf-8-sig'))
    source = verify_audio(args.escape_audio, gold['sources']['387'], album=gold['album'])
    cache_dir = ROOT / 'data/voice_cache'
    decoder = VoiceAnalyzer(cache_dir, models_dir=args.models_dir, device=args.device, singer_model='spectral')
    references = SimpleNamespace(audio=args.escape_audio, references_dir=args.escape_references_dir,
                                 reference=(), profile_json=None)
    started = perf_counter()
    stems, reference_info, missing = load_escape_stems(references, gold, decoder, source['sha256'])
    result = {'state': 'missing_independent_references' if missing else 'scored', 'audio': source,
              'reference_audio': reference_info, 'missing_reference_positions': missing, 'variants': {},
              'measurement': 'Independent enrollment/calibration recordings and central regions of the 49 Escape gold rows.'}
    if missing:
        return result
    audio = decoder.prepare(args.escape_audio, source_hash=source['sha256'])
    result['preparation'] = audio.preparation_id
    result['stem_and_references_s'] = perf_counter() - started
    analyzer = VoiceAnalyzer(cache_dir, models_dir=args.models_dir, device=args.device, singer_model=args.identity,
                             aggregation='topk')
    profiles, film_references, solo_clips = escape_calibration(analyzer, gold, stems)
    offcast = ()
    if args.escape_offcast_probes:
        known_hashes = {source['sha256'], *(row['sha256'] for row in reference_info.values())}
        offcast, offcast_sources = escape_offcast(args, analyzer, profiles, film_references, solo_clips, known_hashes)
        result['offcast_candidates'] = offcast
        result['offcast_reference_audio'] = offcast_sources
    candidates = tuple(profiles)
    calibration = calibration_mixtures(solo_clips)
    regions = list(escape_regions(audio, gold))
    evaluation = [(tuple(row['expected']), mixture, row['category'], start, None) for row, start, _, mixture in regions]
    result.update({'identity_model': analyzer.model_id('singing'), 'profile_examples': {n: len(p) for n, p in profiles.items()},
                   'calibration_cases': {category: sum(row[2] == category for row in calibration)
                                         for category in ('independent_solo', 'synthetic_independent_duet')},
                   'evaluation_rows': len(evaluation)})
    for method in args.backend or ['unmixx', 'singer-informed']:
        started = perf_counter()
        if method == 'unmixx':
            separator = UnmixxSeparator(args.models_dir, device=args.device)
            film_vectors = None
        else:
            separator = SingerInformedSeparator(args.models_dir, device=args.device)
            film_vectors = {name: separator.enroll(samples) for name, samples in film_references.items()}
        load_s = perf_counter() - started
        print(f'{method}: scoring {len(calibration)} independent calibration and {len(evaluation)} Escape cases', flush=True)
        started = perf_counter()
        calibrated, evaluated = ([optional_features(method, separator, analyzer, profiles, film_vectors, case, {})
                                  for case in cases] for cases in (calibration, evaluation))
        inference_s = perf_counter() - started
        assessments = {}
        for key in ('mixture', 'separated'):
            thresholds = calibrate(calibration, [row[key] for row in calibrated], candidates)
            assessments[key] = {'thresholds': thresholds,
                                **assess(evaluation, [row[key] for row in evaluated], thresholds, candidates)}
        variant = {'model': separator.version, 'device': separator.device, 'load_seconds': load_s,
                   'inference_seconds': inference_s, 'source_quality': summarize_acoustics(method, evaluation, evaluated)}
        if method == 'unmixx':
            gate = calibrated_unmixx(calibration, evaluation, calibrated, evaluated,
                                     assessments['mixture'], assessments['separated'], candidates)
            gate['decisions'] = named_decisions({'rows': gate['decisions']}, regions)
            gate['negative_probes'] = negative_probes(gate['decisions'], candidates)
            variant['gated'] = gate
        else:
            fitted = calibrated_film(calibration, evaluation, calibrated, evaluated, candidates)
            if fitted:
                thresholds, assessment = fitted
                assessments['conditional_evidence'] = {'thresholds': thresholds, **assessment}
            if offcast:
                variant['offcast_extraction_energy'] = {}
                for name in offcast:
                    energy = [row['acoustics'][name]['relative_output_energy'] for row in evaluated]
                    variant['offcast_extraction_energy'][name] = {
                        'probes': len(energy), 'median_relative_energy': float(np.median(energy)),
                        'extractions_above_minus_40_db': sum(value > 1e-4 for value in energy)}
        variant['matched_coverage'] = equal_coverage(assessments, candidates)
        for key, assessment in assessments.items():
            rows = named_decisions(assessment, regions)
            variant[key] = {'thresholds': assessment['thresholds'], 'metrics': rows_metrics(rows, candidates),
                            'negative_probes': negative_probes(rows, candidates), 'rows': rows}
        result['variants'][method] = variant
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        print(f'{method}: {inference_s:.2f} seconds ({separator.device})', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'output/playlist-youtube/idolmaster-solo-links.json')
    parser.add_argument('--audio-dir', type=Path, default=ROOT / 'output/mp3')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models/character-voice')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', action='append', choices=('unmixx', 'singer-informed'))
    parser.add_argument('--identity', default='sony', choices=('sony', 'sony-contrastive', 'ecapa'))
    parser.add_argument('--device', default='auto', choices=('cpu', 'cuda', 'auto'))
    parser.add_argument('--escape-audio', type=Path, help='Run Escape diagnostics or independent singer identification')
    parser.add_argument('--escape-gold', type=Path, default=ROOT / 'output/voice-models/escape-gold-parts-test.json')
    parser.add_argument('--escape-references-dir', type=Path, help='Enable independent identification with four reference tracks')
    parser.add_argument('--escape-offcast-probes', action='store_true', help='Add two verified off-cast singers as absent candidates')
    args = parser.parse_args()
    if args.escape_offcast_probes and not (args.escape_audio and args.escape_references_dir):
        parser.error('--escape-offcast-probes requires an independent Escape evaluation')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.escape_audio:
        result = escape_named(args) if args.escape_references_dir else escape_blind(args)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        return
    paths, facts = load_sources(args.manifest, args.audio_dir)
    stems = prepare(args, paths, facts)
    analyzer = VoiceAnalyzer(args.output.parent / 'singing-benchmark-cache', models_dir=args.models_dir,
                             device=args.device, singer_model=args.identity, aggregation='topk')
    profiles = {name: [exemplar for position in positions for exemplar in analyzer.enroll(stems[position])]
                for name, (positions, _, _) in SPLIT.items()}
    calibration = probes(stems, test=False)
    evaluation = probes(stems, test=True)
    result = {'splits': SPLIT, 'identity_model': analyzer.model_id('singing'), 'audio_sources': facts,
              'calibration_cases': len(calibration), 'held_out_cases': len(evaluation), 'variants': {}}
    for method in args.backend or ['unmixx', 'singer-informed']:
        started = perf_counter()
        if method == 'unmixx':
            separator = UnmixxSeparator(args.models_dir, device=args.device)
            enrollment = None
        else:
            separator = SingerInformedSeparator(args.models_dir, device=args.device)
            enrollment = solo_enrollment(stems, separator)
        load_seconds = perf_counter() - started
        started = perf_counter()
        scores = []
        for cases, test in ((calibration, False), (evaluation, True)):
            print(f'{method}: scoring {len(cases)} {"evaluation" if test else "calibration"} cases', flush=True)
            scores.append([optional_features(method, separator, analyzer, profiles, enrollment, case,
                                             reference_stems(stems, case, test=test)) for case in cases])
        inference_seconds = perf_counter() - started
        calibrated, evaluated = scores
        variant = {'model': separator.version, 'device': separator.device, 'load_seconds': load_seconds,
                   'inference_seconds': inference_seconds,
                   'source_quality': summarize_acoustics(method, evaluation, evaluated)}
        assessments = {}
        for key in ('mixture', 'separated'):
            thresholds = calibrate(calibration, [row[key] for row in calibrated])
            assessments[key] = {'thresholds': thresholds,
                                **evaluate(evaluation, [row[key] for row in evaluated], thresholds, .025)}
        if method == 'singer-informed':
            fit = calibrated_film(calibration, evaluation, calibrated, evaluated)
            if fit is not None:
                thresholds, assessment = fit
                assessments['conditional_evidence'] = {'thresholds': thresholds, **assessment}
        variant.update({key: assessment['metrics'] for key, assessment in assessments.items()})
        variant['matched_coverage'] = matched_coverage(assessments)
        if method == 'unmixx':
            variant['gated'] = calibrated_unmixx(calibration, evaluation, calibrated, evaluated,
                                                assessments['mixture'], assessments['separated'])
        result['variants'][method] = variant
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'{method}: {inference_seconds:.2f} seconds ({separator.device})', flush=True)


if __name__ == '__main__':
    main()
