"""Run the 49 aligned Escape gold rows with independent profiles or a blocked diagnostic."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from time import perf_counter

import numpy as np

from benchmark_singing_voice import ROOT, WINDOW_MS, archived_escape, calibrate, combine, excerpt, metrics
from character_voice import SAMPLE_RATE, VoiceAnalyzer, VoiceAudio, _sha256
from sweep_singing_thresholds import BUDGETS, compact, evaluate_frozen, fmt_policy, fmt_rate, freeze_policies, match_coverage


CANDIDATES = {'줄리아': 'idol:ジュリア', '토코로 메구미': 'idol:所恵美'}
VARIANTS = {'ecapa': ('max',), 'sony': ('max', 'topk', 'mean'), 'sony-contrastive': ('topk',)}
BLOCK_MS = 30_000
GUARD_MS = 10_000
MARGIN = .025


def verify_audio(path, expected, *, album=None):
    import av

    with av.open(str(path)) as container:
        metadata = container.metadata
        duration = container.duration / av.time_base
    if (metadata.get('title') not in (None, expected['title'])
            or album and metadata.get('album') not in (None, album)
            or abs(duration - expected['duration']) > 2):
        raise ValueError(f'Audio tags/duration do not match the gold recording: {path}')
    return {'path': str(path.resolve()), 'sha256': _sha256(path), 'metadata': metadata, 'duration_s': duration}


def renamed(mapping):
    return {name: mapping[character_id] for name, character_id in CANDIDATES.items() if character_id in mapping}


def supplied_profiles(path, backend, model_id, target_hash):
    config = json.loads(path.read_text(encoding='utf-8-sig'))['models'].get(backend, {})
    profiles = renamed(config.get('profiles', {}))
    for name, samples in profiles.items():
        for sample in samples:
            source_hash = sample.get('audio_sha256', '')
            if (sample.get('model_id') != model_id or sample.get('kind') != 'singing'
                    or not re.fullmatch('[a-f0-9]{64}', source_hash) or source_hash == target_hash):
                raise ValueError(f'Invalid or target-leaking independent profile for {name}/{backend}')
    return {name: profiles.get(name, []) for name in CANDIDATES}, renamed(config.get('calibration', {}))


def reference_stems(args, gold, decoder, target_hash):
    matches = {}
    missing = []
    hashes = {target_hash}
    reference_dir = args.references_dir or args.audio.parent
    explicit = dict(args.reference)
    expected_positions = {position for values in gold['references'].values() for position in values.values()}
    if len(explicit) != len(args.reference) or not set(explicit) <= expected_positions:
        raise ValueError('Repeated or unknown explicit reference position')
    for values in gold['references'].values():
        for position in values.values():
            # Accept local FLAC, WAV or MP3 with the archived playlist position.
            paths = ([explicit[position]] if position in explicit else
                     sorted(path for path in reference_dir.glob(f'{position:04d} *')
                            if path.suffix.lower() in ('.mp3', '.flac', '.wav')))
            if len(paths) != 1:
                missing.append(position)
                continue
            path, = paths
            source = gold['sources'][str(position)]
            info = verify_audio(path, source, album=source['album'])
            if info['sha256'] in hashes:
                raise ValueError(f'Duplicate reference audio #{position}')
            hashes.add(info['sha256'])
            matches[position] = info
    if missing or args.profile_json:
        return {}, matches, missing
    stems = {}
    for position, info in matches.items():
        stems[position] = decoder.prepare(info['path'], source_hash=info['sha256'])
    return stems, matches, missing


def enrollment(analyzer, audio, ranges):
    return [{**example, 'audio_sha256': audio.source_hash} for example in analyzer.enroll(audio, ranges=ranges)]


def score_rows(analyzer, audio, rows, profiles):
    results = analyzer.score(audio, ranges=[(row['start_ms'], row['end_ms']) for row in rows], profiles=profiles)
    return [{**result, 'line': row['line'], 'truth': row['expected'], 'category': row['category']}
            for row, result in zip(rows, results, strict=True)]


def metrics_for(rows):
    return metrics([(row['truth'], row['scores'], row['singers'], row['status'] == 'estimated') for row in rows], CANDIDATES)


def bounds_for(cases, scored):
    thresholds = calibrate(cases, scored, CANDIDATES)
    return {name: (max(-1., center - MARGIN), min(1., center + MARGIN)) for name, center in thresholds.items()}


def from_reference_stems(analyzer, stems, gold):
    profiles = {}
    excerpts = {}
    for name, refs in gold['references'].items():
        start, end = (1000 * t for t in gold['sources'][str(refs['enroll'])]['clip'])
        profiles[name] = enrollment(analyzer, stems[refs['enroll']], [(start, end)])
        start, end = (1000 * t for t in gold['sources'][str(refs['calibrate'])]['clip'])
        clips = [(t, excerpt(stems[refs['calibrate']], t)) for t in range(start, end - WINDOW_MS + 1, WINDOW_MS)]
        excerpts[name] = [(clip, {'position': refs['calibrate'], 'start_ms': t, 'end_ms': t + WINDOW_MS})
                          for t, clip in clips if clip is not None]
    cases = [((name,), clip, [provenance]) for name, clips in excerpts.items() for clip, provenance in clips]
    a, b = CANDIDATES
    cases.extend(((a, b), (left + right) / np.sqrt(np.float32(2)), [left_source, right_source])
                 for (left, left_source), (right, right_source) in zip(excerpts[a], excerpts[b]))
    cases.append(((), np.zeros(WINDOW_MS * SAMPLE_RATE // 1000, dtype=np.float32), []))
    scores = [analyzer.score(VoiceAudio(audio, 'singing', 'independent-calibration-mixture'),
                             ranges=[(0, WINDOW_MS)], profiles=profiles)[0]['scores'] for _, audio, _ in cases]
    calibration = [{'truth': truth, 'reference_windows': provenance, 'scores': values}
                   for (truth, _, provenance), values in zip(cases, scores, strict=True)]
    centers = calibrate(cases, scores, CANDIDATES)
    return profiles, centers, calibration


def frozen_for(calibration, centers):
    if not calibration:
        return {}
    frozen, _ = freeze_policies([row['truth'] for row in calibration], [row['scores'] for row in calibration], centers)
    return frozen


def sweep_report(frozen, rows):
    selected, _ = evaluate_frozen(frozen, [row['truth'] for row in rows], [row['scores'] for row in rows])
    return selected


def fused_variant(left, right):
    left_rows, right_rows = left['calibration_rows'], right['calibration_rows']
    if not left_rows or not right_rows:
        return None
    labels = [(row['truth'], row['reference_windows']) for row in left_rows]
    if labels != [(row['truth'], row['reference_windows']) for row in right_rows]:
        raise ValueError('Fusion calibration probes differ')
    left_cal, right_cal = ([row['scores'] for row in records] for records in (left_rows, right_rows))
    combined = combine(left_cal, right_cal, left_cal, right_cal)
    calibration = [{**row, 'scores': score} for row, score in zip(left_rows, combined, strict=True)]
    centers = calibrate([(row['truth'],) for row in calibration], combined, CANDIDATES)
    frozen = frozen_for(calibration, centers)
    left_test, right_test = left['rows'], right['rows']
    if [(row['line'], row['truth']) for row in left_test] != [(row['line'], row['truth']) for row in right_test]:
        raise ValueError('Fusion target rows differ')
    scores = combine([row['scores'] for row in left_test], [row['scores'] for row in right_test], left_cal, right_cal)
    rows = [{**{key: row[key] for key in ('line', 'truth', 'category', 'start_ms', 'end_ms')}, 'scores': values}
            for row, values in zip(left_test, scores, strict=True)]
    return {'model_ids': [left['model_id'], right['model_id']], 'state': 'scored' if frozen else 'calibration_unavailable',
            'score_type': 'mean_of_calibration_normalized_cosines', 'calibration_rows': calibration,
            'candidate_centers': centers, 'threshold_sweep': sweep_report(frozen, rows), 'rows': rows}


def finish_independent(independent):
    decisions = {str(budget): {} for budget in BUDGETS}
    for name, variant in independent['variants'].items():
        if variant.get('state') != 'scored':
            continue
        rows = variant['rows']
        rank = metrics([(row['truth'], row['scores'], (), False) for row in rows], CANDIDATES)
        variant['solo_ranking'] = {'top1': rank['by_singer_count']['1']['top1'], 'cases': rank['by_singer_count']['1']['cases']}
        _, masks = evaluate_frozen(variant['threshold_sweep'], [row['truth'] for row in rows], [row['scores'] for row in rows])
        for budget, decisions_for_model in masks.items():
            decisions[budget][name] = decisions_for_model
    independent['matched_holdout_coverage'] = match_coverage(decisions)


def independent_report(independent):
    scored = {name: variant for name, variant in independent['variants'].items() if variant.get('state') == 'scored'}
    lines = ['# エスケープ 독립 가창자 평가', '',
             '캐릭터마다 서로 다른 솔로곡을 등록과 보정에 사용했다. 두 솔로곡의 유효 구간과 RMS를 맞춘 2인 합성 구간, 무음으로 '
             '기준값, 최고점과의 허용 격차, 보류 폭을 정했다. 그 기준을 실제 곡의 솔로 25줄과 듀엣 24줄에 적용했다. '
             '이 곡의 후보는 두 명이다. 네 후보 중 실제 듀엣만 고르는 성능은 별도 4후보 평가에서 측정했다.', '',
             '| 모델 | 솔로 Top-1 | 솔로 완전 일치 | 듀엣 완전 일치 | 솔로 복수 오검출 | 확정 | FPR |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for name, variant in scored.items():
        top = variant['solo_ranking']
        row = [f"{top['top1']}/{top['cases']}"]
        if 'metrics' in variant:
            result = variant['metrics']
            solo, duet = (result['by_singer_count'][key] for key in ('1', '2'))
            row += [f"{solo['exact']}/{solo['cases']}", f"{duet['exact']}/{duet['cases']}",
                    f"{solo['selected_multiple']}/{solo['cases']}", f"{solo['decisive'] + duet['decisive']}/49",
                    fmt_rate(result['false_positive_rate'])]
        else:
            row += ['—'] * 5
        lines.append('| ' + ' | '.join([name, *row]) + ' |')
    lines += ['', '표의 단일 모델에는 보정 점수로 얻은 후보별 기준과 ±0.025 보류 폭을 적용했다. '
                  '결합 점수는 아래의 보정 정책으로 평가했다.', '',
              '## 보정 자료에서 고정한 정책', '',
              '보정 자료의 FPR과 솔로 복수 오검출률에 같은 허용치를 적용했다. '
              '각 허용치에서 아래 정책을 정한 뒤 실제 곡에서 지표를 계산했다.', '',
              '| 모델 | 보정 허용치 | 기준·격차·보류 폭 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | 확정 | FPR | 실제 허용치 충족 |',
              '|---|---:|---|---:|---:|---:|---:|---:|---:|']
    for name, variant in scored.items():
        for budget, choice in variant['threshold_sweep'].items():
            lines.append('| ' + ' | '.join([name, f'{float(budget):.0%}', fmt_policy(choice['policy']),
                                           *compact(choice['holdout']), '예' if choice['holdout_meets_budget'] else '아니오']) + ' |')
    choices = [choice for variant in scored.values() for choice in variant['threshold_sweep'].values()]
    if choices and not any(choice['holdout_meets_budget'] and choice['holdout']['decisive'] for choice in choices):
        lines += ['', '실제 곡에서 한 줄 이상 확정하면서 보정 때 사용한 두 오검출 허용치를 모두 충족한 정책은 없었다.']
    lines += ['', '## 동일 확정 수', '',
              '각 모델에서 결정 경계까지의 거리가 가장 큰 줄을 먼저 골랐다. 모델마다 선정된 줄은 다를 수 있다.', '',
              '| 보정 허용치 | 공통 확정 줄 수 |', '|---:|---:|']
    for budget, common in independent['matched_holdout_coverage'].items():
        lines.append(f"| {float(budget):.0%} | {common['common_decisive_cases']} |")
    return '\n'.join(lines) + '\n'


def reference_argument(value):
    position, separator, path = value.partition('=')
    if not separator or not position.isdecimal() or not path:
        raise argparse.ArgumentTypeError('reference must be POSITION=PATH')
    return int(position), Path(path)


def diagnostic_blocks(rows):
    blocks = {}
    for row in rows:
        blocks.setdefault(row['start_ms'] // BLOCK_MS, []).append(row)
    for block, test in blocks.items():
        guarded_start = min(row['start_ms'] for row in test) - GUARD_MS
        guarded_end = max(row['end_ms'] for row in test) + GUARD_MS
        fit = {key: [row for row in data if row['end_ms'] <= guarded_start or row['start_ms'] >= guarded_end]
               for key, data in blocks.items() if key != block}
        choices = []
        for key, calibration in fit.items():
            enrollment_rows = [row for other, data in fit.items() if other != key for row in data if row['category'] == 'solo']
            enrolled = Counter(row['expected'][0] for row in enrollment_rows)
            negatives = Counter(name for row in calibration for name in CANDIDATES if name not in row['expected'])
            quality = min(enrolled[name] for name in CANDIDATES), min(negatives[name] for name in CANDIDATES)
            if min(quality):
                choices.append((quality, len(calibration), key, enrollment_rows, calibration))
        if choices:
            _, _, _, enrollment_rows, calibration = max(choices, key=lambda choice: choice[:3])
        else:
            enrollment_rows = [row for data in fit.values() for row in data if row['category'] == 'solo']
            calibration = []
        yield block, test, enrollment_rows, calibration


def blocked_diagnostic(analyzer, audio, rows, examples):
    folds = []
    predictions = []
    for block, test, training, calibration in diagnostic_blocks(rows):
        profiles = {name: [sample for row in training if row['expected'] == [name]
                           for sample in examples[row['line']]] for name in CANDIDATES}
        analyzer.calibration = {}
        calibration_scores = score_rows(analyzer, audio, calibration, profiles)
        analyzer.calibration = bounds_for([(row['expected'],) for row in calibration],
                                          [row['scores'] for row in calibration_scores])
        result = score_rows(analyzer, audio, test, profiles)
        predictions.extend(result)
        folds.append({'block': block, 'training_lines': [row['line'] for row in training],
                      'calibration_lines': [row['line'] for row in calibration],
                      'test_lines': [row['line'] for row in test],
                      'exemplars': {name: len(exemplars) for name, exemplars in profiles.items()},
                      'bounds': analyzer.calibration})
    return {'rows': predictions, 'folds': folds, 'metrics': metrics_for(predictions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True, help='Local full 275-second Escape recording')
    parser.add_argument('--gold', type=Path, default=ROOT / 'output/voice-models/escape-gold-parts-test.json')
    parser.add_argument('--cache-dir', type=Path, default=ROOT / 'data/voice_cache')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models/character-voice')
    parser.add_argument('--references-dir', type=Path, help='Directory with four independent recordings named by gold playlist position')
    parser.add_argument('--reference', type=reference_argument, action='append', default=[], help='Override one local reference as POSITION=PATH')
    parser.add_argument('--profile-json', type=Path, help='Export with models.{backend}.profiles and optional .calibration, keyed by canonical character ID')
    parser.add_argument('--diagnostic', action='store_true', help='Also run an explicitly same-recording blocked diagnostic')
    parser.add_argument('--backend', action='append', choices=VARIANTS)
    parser.add_argument('--device', default='auto')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--markdown', type=Path, help='Write a readable independent evaluation table')
    args = parser.parse_args()
    gold = json.loads(args.gold.read_text(encoding='utf-8-sig'))
    rows = [row for row in gold['rows'] if row['category'] in ('solo', 'duet') and row.get('scores')]
    source = verify_audio(args.audio, gold['sources']['387'], album=gold['album'])
    decoder = VoiceAnalyzer(args.cache_dir, models_dir=args.models_dir, singer_model='spectral', device=args.device)
    started = perf_counter()
    audio = decoder.prepare(args.audio, source_hash=source['sha256'])
    stems, reference_info, missing = reference_stems(args, gold, decoder, source['sha256'])
    results = {'audio': source, 'device_requested': args.device, 'stem_and_references_s': perf_counter() - started,
               'stem_preparation_id': audio.preparation_id, 'archive': archived_escape(args.gold),
               'independent': {'reference_audio': reference_info, 'missing_reference_positions': missing,
                               'threshold_selection': 'Enrollment songs and calibration songs are disjoint. Threshold, score gap and abstention are fitted on calibration solo windows and their controlled mixtures before evaluating the target.',
                               'variants': {}},
               'diagnostic': {'limitation': 'Same-recording temporal cross-fit. Recording and production effects are shared across splits.',
                              'block_ms': BLOCK_MS, 'guard_ms': GUARD_MS, 'variants': {}}}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')

    for backend in args.backend or ('sony', 'sony-contrastive', 'ecapa'):
        analyzer = VoiceAnalyzer(args.cache_dir, models_dir=args.models_dir, singer_model=backend, device=args.device)
        try:
            model_id = analyzer.model_id('singing')
            examples = {row['line']: enrollment(analyzer, audio, [(row['start_ms'], row['end_ms'])])
                        for row in rows if row['category'] == 'solo'} if args.diagnostic else {}
            for aggregation in VARIANTS[backend]:
                analyzer.aggregation = aggregation
                analyzer.calibration = {}
                started = perf_counter()
                calibration_rows = []
                centers = {}
                if args.profile_json:
                    profiles, analyzer.calibration = supplied_profiles(args.profile_json, backend, model_id, source['sha256'])
                elif stems:
                    profiles, centers, calibration_rows = from_reference_stems(analyzer, stems, gold)
                    analyzer.calibration = {name: (max(-1., center - MARGIN), min(1., center + MARGIN))
                                            for name, center in centers.items()}
                else:
                    profiles = {name: [] for name in CANDIDATES}
                key = f'{backend}:{aggregation}'
                frozen = frozen_for(calibration_rows, centers)
                output_rows = score_rows(analyzer, audio, rows, profiles)
                results['independent']['variants'][key] = {
                    'model_id': model_id, 'actual_device': analyzer.actual_device, 'calibration': analyzer.calibration,
                    'candidate_centers': centers, 'calibration_examples': len(calibration_rows),
                    'calibration_rows': calibration_rows, 'threshold_sweep': sweep_report(frozen, output_rows),
                    'exemplars': {name: len(p) for name, p in profiles.items()},
                    'seconds': perf_counter() - started, 'metrics': metrics_for(output_rows), 'rows': output_rows,
                    'state': 'scored' if all(profiles.values()) else 'missing_independent_profiles',
                }
                if args.diagnostic:
                    started = perf_counter()
                    diagnostic = blocked_diagnostic(analyzer, audio, rows, examples)
                    diagnostic.update(model_id=model_id, actual_device=analyzer.actual_device, seconds=perf_counter() - started)
                    results['diagnostic']['variants'][key] = diagnostic
                print(key, results['independent']['variants'][key]['state'], flush=True)
                save()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            results['independent']['variants'][backend] = {'error': f'{type(error).__name__}: {error}'}
            save()
    variants = results['independent']['variants']
    if 'sony:topk' in variants and 'ecapa:max' in variants:
        fusion = fused_variant(variants['sony:topk'], variants['ecapa:max'])
        if fusion is not None:
            variants['sony-topk+ecapa-max'] = fusion
            print('sony-topk+ecapa-max', fusion['state'], flush=True)
    finish_independent(results['independent'])
    save()
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(independent_report(results['independent']), encoding='utf-8')


if __name__ == '__main__':
    main()
