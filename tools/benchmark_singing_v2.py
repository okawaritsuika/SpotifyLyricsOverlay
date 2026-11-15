"""Compare independent singing overlap and stem identity on the frozen local splits."""
import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from itertools import permutations
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from types import SimpleNamespace

import numpy as np

from benchmark_singing_voice import (ROOT, SAMPLE_RATE, SPLIT, VoiceAnalyzer, VoiceAudio, WINDOW_MS,
                                     _sha256, calibrate, evaluate, excerpt, fraction, load_sources,
                                     metrics, prepare, probes)
from benchmark_escape_voice import enrollment, reference_stems as escape_stems, verify_audio
from benchmark_singing_separation import calibration_mixtures, power, reference_stems, si_sdr
from singing_overlap import fit_overlap_model, overlap_features, predict_overlap
from singing_voice_separation import SingerInformedSeparator, UnmixxSeparator


BACKENDS = {'sony': 'topk', 'sony-contrastive': 'topk', 'ecapa': 'max'}


@dataclass
class Dataset:
    label: str
    calibration: list
    evaluation: list
    sources: dict
    enrollment_audio: dict
    oracle_references: tuple = ((), ())
    line_ids: tuple = ()
    novel_duration_count: int | None = None


def synthetic(args, paths, facts):
    stems = prepare(args, paths, facts)
    cal, test = probes(stems, test=False), probes(stems, test=True, harmony=True)
    enroll = {name: [(stems[pos], None) for pos in split[0]] for name, split in SPLIT.items()}
    for positions, calibration, evaluation in SPLIT.values():
        for position in positions:
            facts[position]['role'] = 'enrollment'
        facts[calibration]['role'] = 'calibration'
        facts[evaluation]['role'] = 'evaluation'
    references = tuple([reference_stems(stems, case, test=held_out) if case[2] != 'synthetic_major_third' else {}
                        for case in cases] for held_out, cases in ((False, cal), (True, test)))
    return Dataset('four_synthetic', cal, test, facts, enroll, references)


def escape(args, paths, facts, width_ms):
    gold = json.loads(args.escape_gold.read_text(encoding='utf-8-sig'))
    source = verify_audio(args.escape_audio, gold['sources']['387'], album=gold['album'])
    decoder = VoiceAnalyzer(ROOT / 'data/voice_cache', models_dir=args.models_dir,
                            device=args.device, singer_model='spectral')
    config = SimpleNamespace(audio=args.escape_audio, references_dir=args.audio_dir,
                             reference=(), profile_json=None)
    stems, reference_info, missing = escape_stems(config, gold, decoder, source['sha256'])
    if missing:
        raise ValueError(f'Missing independent Escape recordings: {missing}')
    enroll, solo_clips = {}, {}
    for name, refs in gold['references'].items():
        pos = refs['enroll']
        reference_info[pos]['role'] = 'enrollment'
        bounds = [tuple(1000 * value for value in gold['sources'][str(pos)]['clip'])]
        enroll[name] = [(stems[pos], bounds)]
        pos = refs['calibrate']
        reference_info[pos]['role'] = 'calibration'
        start, end = (1000 * value for value in gold['sources'][str(pos)]['clip'])
        solo_clips[name] = [clip for t in range(start, end - WINDOW_MS + 1, WINDOW_MS)
                            if (clip := excerpt(stems[pos], t)) is not None]
    if args.offcast:
        # Reuse the previous report's verified off-cast artists and recording splits.
        offcast_names = ('idol:天海春香', 'idol:如月千早')
        cached = prepare(args, paths, facts)
        known = {source['sha256'], *(row['sha256'] for row in reference_info.values())}
        for name in offcast_names:
            positions, pos, _ = SPLIT[name]
            for index in (*positions, pos):
                if facts[index]['sha256'] in known:
                    raise ValueError('Repeated recording in off-cast reference split')
                known.add(facts[index]['sha256'])
                reference_info[index] = {**facts[index], 'role': 'calibration' if index == pos else 'enrollment'}
            enroll[name] = [(cached[index], None) for index in positions]
            solo_clips[name] = [clip for t in range(0, cached[pos].duration_ms - WINDOW_MS + 1, WINDOW_MS)
                                if (clip := excerpt(cached[pos], t)) is not None]
    if not all(solo_clips.values()):
        raise ValueError('Missing quality-passing independent Escape calibration windows')
    cal = calibration_mixtures(solo_clips)
    cal_sources = [{name: solo_clips[name][start // WINDOW_MS] / np.sqrt(np.float32(len(truth)))
                    for name in truth} for truth, _, _, start, _ in cal]
    cal.append(((), np.zeros(WINDOW_MS * SAMPLE_RATE // 1000, dtype=np.float32), 'silence', 0, None))
    cal_sources.append({})
    audio = decoder.prepare(args.escape_audio, source_hash=source['sha256'])
    rows = [row for row in gold['rows'] if row['category'] in ('solo', 'duet') and row.get('scores')]
    test = []
    for row in rows:
        start, end = row['start_ms'], min(row['end_ms'], audio.duration_ms)
        # Gold bounds are used only to prevent intentionally mixing a labeled
        # singer with an unknown neighbor in this window-size diagnostic.
        length = min(width_ms, end - start)
        start += (end - start - length) // 2
        samples = audio.samples[start * SAMPLE_RATE // 1000:(start + length) * SAMPLE_RATE // 1000]
        test.append((tuple(row['expected']), samples, row['category'], start, None))
    return Dataset('escape_four' if args.offcast else 'escape_pair', cal, test,
                   {'target': source, 'references': reference_info}, enroll, (cal_sources, ()),
                   line_ids=tuple(row['line'] for row in rows),
                   novel_duration_count=sum(len(case[1]) > WINDOW_MS * SAMPLE_RATE // 1000 for case in test))


class SeparationCache:
    def __init__(self, args):
        self.root = args.cache_dir / 'unmixx-v2'
        self.root.mkdir(parents=True, exist_ok=True)
        self.separator = UnmixxSeparator(args.models_dir, device=args.device)
        self.hits = 0
        self.misses = 0
        self.seconds = 0.0

    def separate(self, samples):
        if not np.any(samples):
            return np.zeros((2, len(samples)), dtype=np.float32)
        key = sha256(self.separator.version.encode() + samples.tobytes()).hexdigest()
        path = self.root / f'{key}.npy'
        if path.is_file():
            try:
                stems = np.load(path, allow_pickle=False)
            except (OSError, ValueError):
                stems = None
            if stems is not None and stems.shape == (2, len(samples)) and np.isfinite(stems).all():
                self.hits += 1
                return stems
        started = perf_counter()
        stems = self.separator.separate(samples)
        self.seconds += perf_counter() - started
        self.misses += 1
        with NamedTemporaryFile(dir=self.root, suffix='.npy', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            np.save(temporary, stems, allow_pickle=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return stems


def count_metrics(examples, counts):
    confusion = {str(size): Counter() for size in range(5)}
    for (truth, *_), estimated in zip(examples, counts, strict=True):
        confusion[str(len(truth))][str(estimated) if estimated is not None else 'uncertain'] += 1
    decided = sum(count is not None for count in counts)
    correct = sum(count == min(2, len(case[0])) for case, count in zip(examples, counts, strict=True))
    return {'case_count': len(counts), 'decided': decided, 'correct': correct,
            'accuracy': fraction(correct, len(counts)), 'accuracy_when_decided': fraction(correct, decided),
            'coverage': fraction(decided, len(counts)), 'by_true_count': {k: dict(v) for k, v in confusion.items() if v}}


def count_bucket(result):
    return 2 if result['status'] == 'overlap' else result['count']


def energy_cutoff(examples, features):
    solos, overlaps = [], []
    for (truth, *_), row in zip(examples, features, strict=True):
        if row.get('has_stems') and truth:
            (solos if len(truth) == 1 else overlaps).append(row['log_energy_ratio'])
    edges = sorted(set(solos + overlaps))
    return max(((a + b) / 2 for a, b in zip(edges, edges[1:])), key=lambda cut: (
        sum(r < cut for r in solos) / len(solos) + sum(r >= cut for r in overlaps) / len(overlaps), cut))


def energy_class(row, cut):
    if row['peak'] <= 1e-6:
        return 0
    return (1 if row['log_energy_ratio'] < cut else 2) if row.get('has_stems') else None


def stem_oracle(estimates, references):
    if not 1 <= len(references) <= 2:
        return None
    choices = []
    for order in permutations(range(2), len(references)):
        scores = [si_sdr(estimates[index], clean) for index, clean in zip(order, references.values(), strict=True)]
        choices.append((sum(scores), order, min(scores)))
    choices.sort(reverse=True)
    best, order, individual = choices[0]
    if individual < 0 or best - choices[1][0] < 3.0:
        return None
    names = [None, None]
    for index, name in zip(order, references, strict=True):
        names[index] = name
    return names


def identity_metrics(rows, candidates):
    scored = [(row['truth'], row['scores'], row['predicted'], row['decisive']) for row in rows]
    result = metrics(scored, candidates)
    result['macro_f1'] = 0.
    result['by_candidate'] = {}
    for name in candidates:
        tp = sum(name in row['truth'] and name in row['predicted'] for row in rows)
        fp = sum(name not in row['truth'] and name in row['predicted'] for row in rows)
        fn = sum(name in row['truth'] and name not in row['predicted'] for row in rows)
        absent = sum(name not in row['truth'] for row in rows)
        f1 = fraction(2 * tp, 2 * tp + fp + fn)
        result['by_candidate'][name] = {'absent': absent, 'false_positives': fp, 'false_negatives': fn,
                                        'false_positive_rate': fraction(fp, absent), 'f1': f1}
        result['macro_f1'] += (f1 or 0.) / len(candidates)
    result['uncertain_rate'] = 1 - result['coverage'] if result['coverage'] is not None else None
    return result


def identity_summary(rows, candidates):
    groups = {'all': rows,
              'frozen_primary': [r for r in rows if r['scenario'] != 'synthetic_major_third'],
              'unison': [r for r in rows if r['scenario'] == 'same_melody' and len(r['truth']) == 2
                         and (r['joint_pitch_agreement'] or 0) >= .75],
              'harmony': [r for r in rows if r['scenario'] == 'synthetic_major_third']}
    return {name: identity_metrics(group, candidates) for name, group in groups.items() if group}


def load_identity_models(args, dataset):
    models, profiles, info = {}, {}, {}
    for backend, aggregation in BACKENDS.items():
        model = VoiceAnalyzer(args.cache_dir, device=args.device, singer_model=backend,
                              models_dir=args.models_dir, aggregation=aggregation)
        models[backend] = model
        profiles[backend] = {name: [sample for audio, ranges in recordings for sample in enrollment(model, audio, ranges)]
                             for name, recordings in dataset.enrollment_audio.items()}
        info[backend] = {'model_id': model.model_id('singing'), 'aggregation': aggregation,
                         'exemplars': {name: len(samples) for name, samples in profiles[backend].items()},
                         'recording_count': {name: len(recordings) for name, recordings in dataset.enrollment_audio.items()}}
    return models, profiles, info


def profile_quality(sample):
    quality = sample.get('quality', {})
    return (quality.get('tonal_fraction', 0), quality.get('active_fraction', 0),
            -quality.get('clipped_fraction', 0), quality.get('rms_dbfs', -120))


def limited_profiles(profiles, limit):
    if limit is None:
        return profiles
    result = {}
    for name, samples in profiles.items():
        recordings = {}
        for sample in samples:
            recordings.setdefault(sample.get('audio_sha256'), []).append(sample)
        result[name] = [sample for row in recordings.values()
                        for sample in sorted(row, key=profile_quality, reverse=True)[:limit]]
    return result


def sweep_registration(args, dataset):
    """Select each encoder's representative windows using calibration recordings alone."""
    models, profiles, info = load_identity_models(args, dataset)
    candidates = tuple(dataset.enrollment_audio)
    result = {}
    for backend, model in models.items():
        started = perf_counter()
        embeddings = []
        for cases in (dataset.calibration, dataset.evaluation):
            examples = [VoiceAudio(case[1], 'singing', 'benchmark-registration-sweep') for case in cases]
            embeddings.append([model._window(audio, 0, audio.duration_ms)[1] for audio in examples])
        choices = {}
        for limit in (None, 1, 2, 3):
            selected = limited_profiles(profiles[backend], limit)
            compare = model.profile_comparator('singing', selected)
            scores_cal, scores_test = ([compare(value) for value in subset] for subset in embeddings)
            bounds = calibrate(dataset.calibration, scores_cal, candidates)
            cal_rows = evaluate(dataset.calibration, scores_cal, bounds, .025)['rows']
            test_rows = evaluate(dataset.evaluation, scores_test, bounds, .025)['rows']
            choices[str(limit or 'all')] = {'calibration': identity_summary(cal_rows, candidates),
                                            'evaluation': identity_summary(test_rows, candidates),
                                            'exemplars': {name: len(row) for name, row in selected.items()}}
        best = max(choices, key=lambda key: (choices[key]['calibration']['all']['f1'] or 0,
                                            -(choices[key]['calibration']['all']['false_positive_rate'] or 0),
                                            choices[key]['calibration']['all']['coverage'] or 0))
        result[backend] = {'source_recordings': info[backend]['recording_count'], 'variants': choices,
                           'best_on_calibration': best, 'seconds': perf_counter() - started}
        print(backend, 'best enrollment limit on calibration', best, flush=True)
    return {'dataset': dataset.label, 'selection': 'per-model quality caps within the same verified recording pool',
            'result': result}


def scores_for(samples, models, comparators):
    audio = VoiceAudio(samples, 'singing', 'benchmark-unmixx-v2')
    scores = {}
    for backend, model in models.items():
        _, embedding = model._window(audio, 0, audio.duration_ms)
        scores[backend] = comparators[backend](embedding)
    quality, _ = VoiceAnalyzer._signal(audio, 0, audio.duration_ms)
    return scores, quality


def film_context(args, dataset):
    separator = SingerInformedSeparator(args.models_dir, device=args.device)
    from character_voice import _quality
    refs = {}
    for name, recordings in dataset.enrollment_audio.items():
        clips = []
        for audio, ranges in recordings:
            for left, right in ranges or [(0, audio.duration_ms)]:
                clips.extend(audio.samples[t * SAMPLE_RATE // 1000:(t + 3000) * SAMPLE_RATE // 1000]
                             for t in range(left, right - 2999, 2000))
        refs[name] = separator.enroll(max(clips, key=lambda clip: (_quality(clip)['active_fraction'], power(clip))))
    return separator, refs


def film_scores(mixture, film, models, comparators):
    if film is None or not np.any(mixture):
        return {}
    separator, refs = film
    evidence = {}
    for singer, vector in refs.items():
        target, residual = separator.extract(mixture, vector)
        scored, quality = scores_for(target, {'sony': models['sony']}, comparators)
        residual_scores, _ = scores_for(residual, {'sony': models['sony']}, comparators)
        raw = scored['sony'][singer]
        rival = max((value for name, value in scored['sony'].items() if name != singer and value is not None), default=None)
        energy = max(power(mixture), 1e-12)
        evidence[singer] = {'quality': quality, 'target_similarity': raw,
                            'residual_similarity': residual_scores['sony'][singer],
                            'target_margin': raw - rival if raw is not None and rival is not None else None,
                            'energy_fraction': power(target) / energy, 'residual_gain': 1 - power(residual) / energy}
    return evidence


def score_cases(cases, references, separation, models, comparators, film=None):
    rows = []
    for index, case in enumerate(cases):
        samples = case[1]
        stems = separation.separate(samples)
        features = overlap_features(samples, stems)
        mixture, _ = scores_for(samples, models, comparators)
        pairs = [scores_for(stem, models, comparators) for stem in stems]
        qualities = [{**(pair[1] or {}), 'energy_fraction': power(stem) / max(power(samples), 1e-12)}
                     for pair, stem in zip(pairs, stems, strict=True)]
        rows.append({'overlap': features, 'mixture': mixture, 'stems': [pair[0] for pair in pairs],
                     'qualities': qualities,
                     'oracle': stem_oracle(stems, references[index]) if references else None,
                     'film': film_scores(samples, film, models, comparators)})
    return rows


def assignment_rows(dataset, scored, overlap_model, *, training):
    from singing_assignment import assignment_features

    inputs = dataset.calibration if training else dataset.evaluation
    rows = []
    for (truth, _, scenario, start, pitch), raw in zip(inputs, scored, strict=True):
        overlap = predict_overlap(raw['overlap'], overlap_model)
        overlap['features'] = raw['overlap']
        evidence = assignment_features(raw['mixture'], raw['stems'], overlap,
                                       qualities=raw['qualities'], targets=raw['film'] or None)
        rows.append({'truth': truth, 'scenario': scenario, 'start_ms': start, 'joint_pitch_agreement': pitch,
                     'overlap': overlap, 'evidence': evidence, 'oracle': raw['oracle']})
    return rows


def baselines(calibration, evaluation, cal_raw, eval_raw, candidates):
    result = {}
    for backend in BACKENDS:
        for source in ('mixture', 'stems'):
            if source == 'mixture':
                train, test = ([row['mixture'][backend] for row in rows] for rows in (cal_raw, eval_raw))
            else:
                train, test = ([{name: max((s[backend][name] for s in row['stems']
                                            if s[backend][name] is not None), default=None) for name in candidates}
                                for row in rows] for rows in (cal_raw, eval_raw))
            thresholds = calibrate(calibration, train, candidates)
            rows = evaluate(evaluation, test, thresholds, .025)['rows']
            for row in rows:
                row['confidence'] = min((abs(s - thresholds[name]) for name, s in row['scores'].items()
                                         if s is not None and name in thresholds), default=None)
            result[f'{backend}:{source}'] = {'metrics': identity_summary(rows, candidates), 'thresholds': thresholds,
                                             'rows': rows}
    return result


def oracle_summary(rows):
    total = decided = correct = confirmed = confirmed_correct = 0
    for row in rows:
        if row['oracle'] is None:
            continue
        for index, truth in enumerate(row['oracle']):
            if truth is None:
                continue
            total += 1
            joint = row['assignment'].get('joint', {})
            assignment = (joint or row['assignment']).get('stems', [])
            predicted = assignment[index] if len(assignment) > index else None
            decided += predicted is not None
            correct += predicted == truth
            accepted = row['decisive'] and predicted in row['predicted']
            confirmed += accepted
            confirmed_correct += accepted and predicted == truth
    return {'oracle_assignable_stems': total, 'assigned': decided, 'correct': correct,
            'accuracy': fraction(correct, total), 'accuracy_when_assigned': fraction(correct, decided),
            'confirmed': confirmed, 'confirmed_correct': confirmed_correct,
            'accuracy_when_confirmed': fraction(confirmed_correct, confirmed)}


def classify_assignment(rows, model):
    from singing_assignment import assign_singers

    classified = []
    for row in rows:
        prediction = assign_singers(row['evidence'], row['overlap'], model)
        scores = prediction['scores']
        classified.append({**row, 'scores': scores, 'predicted': prediction['singers'],
                           'decisive': prediction['status'] in ('estimated', 'no_vocal'),
                           'decisions': prediction['decisions'], 'assignment': prediction.get('assignment') or {},
                           'confidence': min((abs(score) for score in scores.values() if score is not None), default=None)})
    return classified


def assignment_summary(rows, candidates):
    result = identity_summary(rows, candidates)
    # Candidate participation can remain independent evidence even when the
    # final compatible singer set is withheld by the count or pair gate.
    presence_rows = [{**row, 'predicted': [name for name, state in row['decisions'].items() if state == 'present'],
                      'decisive': 'uncertain' not in row['decisions'].values()} for row in rows]
    return {'metrics': result, 'candidate_presence': identity_summary(presence_rows, candidates),
            'rows': rows, 'stem_oracle': oracle_summary(rows)}


def equal_coverage(variants, candidates):
    ordered = {name: sorted([r for r in variant['rows'] if r['decisive'] and r['truth']
                             and r['scenario'] != 'synthetic_major_third'],
                            key=lambda r: r['confidence'] if r['confidence'] is not None else 0., reverse=True)
               for name, variant in variants.items()}
    count = min(map(len, ordered.values()))
    shared = set.intersection(*(set((r['start_ms'], r['scenario'], tuple(r['truth'])) for r in rows)
                               for rows in ordered.values()))
    return {'decisive_vocal_per_method': count, 'same_row_intersection': len(shared),
            'at_common_coverage': {name: identity_metrics(rows[:count], candidates) for name, rows in ordered.items()},
            'same_rows': {name: identity_metrics([r for r in rows if (r['start_ms'], r['scenario'], tuple(r['truth']))
                                                 in shared], candidates) for name, rows in ordered.items()}}


def benchmark(args, dataset, separation, width_ms, fitted=None):
    import torch
    from singing_assignment import fit_assignment_model

    gpu = separation.separator.device == 'cuda'
    if gpu:
        torch.cuda.reset_peak_memory_stats()
    started = perf_counter()
    models, profiles, profile_info = load_identity_models(args, dataset)
    comparators = {backend: model.profile_comparator('singing', profiles[backend]) for backend, model in models.items()}
    film = film_context(args, dataset) if args.film else None
    initialization = perf_counter() - started
    cal_ref, eval_ref = dataset.oracle_references
    started = perf_counter()
    cal_raw = score_cases(dataset.calibration, cal_ref, separation, models, comparators, film)
    calibration_seconds = perf_counter() - started
    print(dataset.label, width_ms, 'scored calibration', len(cal_raw), round(calibration_seconds, 2), flush=True)
    cal_overlap = [r['overlap'] for r in cal_raw]
    overlap_model = fitted['overlap'] if fitted else fit_overlap_model(cal_overlap, [len(case[0]) for case in dataset.calibration])
    cal_rows = assignment_rows(dataset, cal_raw, overlap_model, training=True)
    if args.calibration_only:
        return {'overlap': overlap_model, 'calibration_cases': len(cal_raw), 'rows': cal_rows,
                'sources': dataset.sources}
    assignment_model = fitted['assignment'] if fitted else fit_assignment_model(
        [(r['evidence'], r['truth'], r['oracle']) for r in cal_rows])
    calibration = {'version': 1, 'window_ms': WINDOW_MS, 'overlap': overlap_model, 'assignment': assignment_model}
    if args.calibration_output and width_ms == WINDOW_MS:
        args.calibration_output.parent.mkdir(parents=True, exist_ok=True)
        args.calibration_output.write_text(json.dumps(calibration, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    started = perf_counter()
    eval_raw = score_cases(dataset.evaluation, eval_ref, separation, models, comparators, film)
    evaluation_seconds = perf_counter() - started
    eval_rows = assignment_rows(dataset, eval_raw, overlap_model, training=False)
    for row, line in zip(eval_rows, dataset.line_ids):
        row['line'] = line
    cut = energy_cutoff(dataset.calibration, cal_overlap)
    count_outputs = [count_bucket(row['overlap']) for row in eval_rows]
    count = {'multi_feature': count_metrics(dataset.evaluation, count_outputs),
             'energy_only': count_metrics(dataset.evaluation, [energy_class(r['overlap'], cut) for r in eval_raw]),
             'energy_cutoff': 10 ** cut,
             'primary_multi_feature': count_metrics([case for case in dataset.evaluation if case[2] != 'synthetic_major_third'],
                                                    [prediction for case, prediction in zip(dataset.evaluation, count_outputs, strict=True)
                                                     if case[2] != 'synthetic_major_third']),
             'calibration': overlap_model}
    candidates = tuple(dataset.enrollment_audio)
    variants = baselines(dataset.calibration, dataset.evaluation, cal_raw, eval_raw, candidates)
    for name, model in (('strict', {**assignment_model, 'pair': None}), ('new', assignment_model)):
        variants[name] = assignment_summary(classify_assignment(eval_rows, model), candidates)
    return {'window_cap_ms': width_ms, 'windows_longer_than_2400': dataset.novel_duration_count,
            'effective_length_ms': [round(len(case[1]) / SAMPLE_RATE * 1000, 2) for case in dataset.evaluation],
            'calibration_cases': len(cal_raw), 'evaluation_cases': len(eval_raw), 'sources': dataset.sources,
            'profile_info': profile_info, 'fitted': calibration, 'count': count, 'variants': variants,
            'matched_coverage': equal_coverage(variants, candidates),
            'matched_coverage_strict_vs_joint': equal_coverage({k: variants[k] for k in ('strict', 'new')}, candidates),
            'timing': {'initialization_seconds': initialization, 'calibration_seconds': calibration_seconds,
                       'evaluation_seconds': evaluation_seconds, 'separator_device': separation.separator.device,
                       'separator_cumulative_seconds': separation.seconds, 'separator_cache_hits': separation.hits,
                       'separator_cache_misses': separation.misses,
                       'encoder_devices': {k: model.actual_device for k, model in models.items()},
                       'peak_cuda_allocated_mib': torch.cuda.max_memory_allocated() / 2**20 if gpu else None,
                       'peak_cuda_reserved_mib': torch.cuda.max_memory_reserved() / 2**20 if gpu else None}}


def refit_assignment(previous, calibration_data, *, count_config=None):
    """Refit and compare policies on preserved evidence without audio inference."""
    from copy import deepcopy
    from singing_assignment import fit_assignment_model

    if previous['dataset'] != calibration_data['dataset']:
        raise ValueError('Calibration and evaluation cohorts do not match')
    fitted = {}
    calibration = calibration_data['windows']['2400']

    def update_count(rows, model):
        rows = deepcopy(rows)
        for row in rows:
            features = row['overlap']['features']
            row['overlap'] = {**predict_overlap(features, model), 'features': features}
            row['evidence']['overlap'] = row['overlap']
            for evidence in row['evidence']['presence'].values():
                evidence.update(overlap_score=row['overlap']['score'], overlap_minimum=row['overlap']['min_count'])
        return rows

    count_model = count_config['overlap'] if count_config else calibration['overlap']
    training = update_count(calibration['rows'], count_model)
    assignment_model = fit_assignment_model([(row['evidence'], row['truth'], row['oracle']) for row in training])
    for width, prior in previous['windows'].items():
        if prior['calibration_cases'] != len(training):
            raise ValueError('Stored calibration row count changed')
        baseline = {name: entry for name, entry in prior['variants'].items() if name != 'new'}
        rows = update_count(prior['variants']['new']['rows'], count_model)
        candidates = tuple(prior['profile_info']['sony']['exemplars'])
        variants = {}
        for name, model in (('strict', {**assignment_model, 'pair': None}), ('joint', assignment_model)):
            variants[name] = assignment_summary(classify_assignment(rows, model), candidates)
        count_cases = [(row['truth'],) for row in rows]
        fitted[width] = {'variants': variants, 'count': count_metrics(count_cases, [count_bucket(r['overlap']) for r in rows]),
                         'matched_coverage': equal_coverage({**baseline, **variants}, candidates),
                         'matched_coverage_strict_vs_joint': equal_coverage(variants, candidates),
                         'matched_coverage_joint_vs_ecapa': equal_coverage({'joint': variants['joint'], 'ecapa': baseline['ecapa:mixture']}, candidates)}
    return {'dataset': previous['dataset'], 'fitted': {'version': 1, 'window_ms': 2400,
                                                      'overlap': count_model, 'assignment': assignment_model},
            'windows': fitted, 'code_sha256': {name: sha256((ROOT / name).read_bytes()).hexdigest()
                                               for name in ('singing_overlap.py', 'singing_assignment.py')}}


def song_reference_diagnostic(args, report):
    """Measure label-free within-song prototype discovery as a separate diagnostic."""
    data = report['windows']['2400']
    rows = data['variants']['new']['rows']
    spans = [(row['start_ms'], row['start_ms'] + width)
             for row, width in zip(rows, data['effective_length_ms'], strict=True)]
    candidates = tuple(data['profile_info']['sony']['exemplars'])
    anchors = {name: [] for name in candidates}

    def top(scores):
        ordered = sorted(((value, name) for name, value in scores.items() if value is not None), reverse=True)
        return ordered[0][1] if ordered and (len(ordered) == 1 or ordered[0][0] != ordered[1][0]) else None

    for index, row in enumerate(rows):
        if not row['decisive'] or row['overlap']['status'] != 'solo' or len(row['predicted']) != 1:
            continue
        name, = row['predicted']
        if all(top(scores) == name for scores in row['evidence']['mixture_scores'].values()):
            anchors[name].append(index)
    started = perf_counter()
    vectors = {backend: {} for backend in BACKENDS}
    source_hash = data['sources']['target']['sha256']
    if _sha256(args.escape_audio) != source_hash:
        raise ValueError('Song-reference diagnostic requires the same recording')
    for backend in BACKENDS:
        analyzer = VoiceAnalyzer(ROOT / 'data/voice_cache', models_dir=args.models_dir,
                                 device=args.device, singer_model=backend)
        audio = analyzer.prepare(args.escape_audio, source_hash=source_hash)
        for index, (start, end) in enumerate(spans):
            _, embedding = analyzer._window(audio, int(start), int(round(end)))
            vectors[backend][index] = embedding
    results = []
    guard_ms = 10_000
    for index, row in enumerate(rows):
        start, end = spans[index]
        accepted = {name: [j for j in indices if spans[j][1] + guard_ms <= start or end + guard_ms <= spans[j][0]]
                    for name, indices in anchors.items()}
        baseline = top(row['scores'])
        anchor_vote = None
        if all(accepted.values()):
            votes = []
            for backend, embeddings in vectors.items():
                query = embeddings[index]
                scores = {}
                if query is not None:
                    for name, choices in accepted.items():
                        dots = [float(embeddings[j] @ query) for j in choices if embeddings[j] is not None]
                        scores[name] = max(dots, default=None)
                votes.append(top(scores) if len(scores) == len(candidates) and all(v is not None for v in scores.values()) else None)
            if len(set(votes)) == 1:
                anchor_vote = votes[0]
        results.append({'line': row['line'], 'truth': row['truth'], 'baseline': baseline,
                        'vote': anchor_vote, 'reranked': anchor_vote or baseline})
    solos = [row for row in results if len(row['truth']) == 1]
    return {'guard_ms': guard_ms, 'candidates': candidates, 'anchor_lines': {
                name: [rows[i]['line'] for i in indices] for name, indices in anchors.items()},
            'anchor_errors': {name: sum(rows[i]['truth'] != [name] for i in indices) for name, indices in anchors.items()},
            'solo_cases': len(solos), 'solo_votes': sum(row['vote'] is not None for row in solos),
            'solo_top1_baseline': sum(row['baseline'] in row['truth'] for row in solos),
            'solo_top1_within_song': sum(row['reranked'] in row['truth'] for row in solos),
            'inference_seconds': perf_counter() - started, 'rows': results,
            'measurement': 'Same-recording diagnostic using consensus-ranked inferred solo anchors and 10 s guard. '
                           'Only top-one reranking is compared.'}


def make_dataset(args, name, width_ms):
    paths, facts = load_sources(args.manifest, args.audio_dir) if name == 'synthetic' or args.offcast else ({}, {})
    return synthetic(args, paths, facts) if name == 'synthetic' else escape(args, paths, facts, width_ms)


def main():
    workspace = ROOT / 'output/voice-models'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=('escape', 'synthetic'), default='escape')
    parser.add_argument('--offcast', action='store_true', help='Add two independent absent artists to Escape')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'output/playlist-youtube/idolmaster-solo-links.json')
    parser.add_argument('--audio-dir', type=Path, default=ROOT / 'output/mp3')
    parser.add_argument('--escape-audio', type=Path, default=ROOT / 'output/mp3/0387 エスケープ.mp3')
    parser.add_argument('--escape-gold', type=Path, default=workspace / 'escape-gold-parts-test.json')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models/character-voice')
    parser.add_argument('--cache-dir', type=Path, default=workspace / 'singing-benchmark-v2-cache')
    parser.add_argument('--device', choices=('auto', 'cuda', 'cpu'), default='auto')
    parser.add_argument('--window', type=int, choices=(2400, 4000, 6000, 8000), action='append')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache-only', action='store_true')
    parser.add_argument('--film', action='store_true', help='Measure optional conditioned target and residual features')
    parser.add_argument('--profile-sweep', action='store_true', help='Compare quality-selected enrollment examples')
    parser.add_argument('--calibration-output', type=Path, help='Write the fitted 2400 ms application model')
    parser.add_argument('--calibration-only', action='store_true', help='Preserve raw evidence for the calibration recordings')
    parser.add_argument('--rescore', type=Path, help='Re-evaluate stored rows offline with the current assignment code')
    parser.add_argument('--training-evidence', type=Path, help='Previously extracted independent calibration evidence')
    parser.add_argument('--count-config', type=Path, help='Optional overlap configuration trained on an independent cohort')
    parser.add_argument('--song-reference-report', type=Path, help='Test temporary song-local profiles from a scored Escape report')
    args = parser.parse_args()
    windows = args.window or [2400]
    if args.dataset == 'synthetic' and windows != [2400]:
        parser.error('the frozen synthetic examples use 2400 ms windows')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.song_reference_report:
        report = json.loads(args.song_reference_report.read_text(encoding='utf-8'))
        result = song_reference_diagnostic(args, report)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        return
    if args.rescore:
        if not args.training_evidence:
            parser.error('--rescore requires --training-evidence')
        previous = json.loads(args.rescore.read_text(encoding='utf-8'))
        training = json.loads(args.training_evidence.read_text(encoding='utf-8'))
        count_config = json.loads(args.count_config.read_text(encoding='utf-8')) if args.count_config else None
        result = refit_assignment(previous, training, count_config=count_config)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        return
    if args.profile_sweep:
        result = sweep_registration(args, make_dataset(args, args.dataset, windows[0]))
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        return
    separator = SeparationCache(args)
    result, fitted = {}, None
    for width_ms in windows:
        dataset = make_dataset(args, args.dataset, width_ms)
        if args.cache_only:
            for case in (*dataset.calibration, *dataset.evaluation):
                separator.separate(case[1])
            print(dataset.label, width_ms, len(dataset.calibration), len(dataset.evaluation),
                  separator.misses, separator.hits, round(separator.seconds, 2), flush=True)
        else:
            result[str(width_ms)] = benchmark(args, dataset, separator, width_ms, fitted)
            if fitted is None and not args.calibration_only:
                fitted = result[str(width_ms)]['fitted']
            args.output.write_text(json.dumps({'dataset': dataset.label, 'windows': result}, ensure_ascii=False,
                                              indent=2, allow_nan=False) + '\n', encoding='utf-8')
            if not args.calibration_only:
                print(dataset.label, width_ms, result[str(width_ms)]['count']['multi_feature'],
                      result[str(width_ms)]['variants']['new']['metrics']['frozen_primary'], flush=True)


if __name__ == '__main__':
    main()
