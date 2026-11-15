"""Sweep singer participation thresholds, then freeze calibrated policies for evaluation."""
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from benchmark_singing_voice import ROOT, combine, load_sources, prepare, probes


BUDGETS = (0, .05, .10, .20)
GAPS = (None, .025, .05, .075, .10, .15, .20)
BANDS = (0., .01, .025, .05)
ESCAPE_POINTS = (.40, .45, .47, .48, .49, .50, .51, .52, .53, .54, .55, .56, .58, .60, .65, .97)


@dataclass(frozen=True)
class Policy:
    mode: str
    center: float
    gap: float | None = None
    band: float = 0.


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def measurements(truths, rows):
    candidates = tuple(rows[0])
    if len(truths) != len(rows) or any(set(row) != set(candidates) for row in rows):
        raise ValueError('Ground-truth and raw score rows do not align')
    truth = np.asarray([[name in expected for name in candidates] for expected in truths], dtype=bool)
    scores = np.asarray([[row[name] for name in candidates] for row in rows], dtype=np.float64)
    return candidates, truth, scores


def decisions(scores, candidates, policies, centers):
    thresholds = np.array([[policy.center + (centers[name] if policy.mode == 'offset' else 0.)
                            for name in candidates] for policy in policies])
    bands = np.array([policy.band for policy in policies])[:, None, None]
    gaps = np.array([policy.gap if policy.gap is not None else np.inf for policy in policies])[:, None, None]
    present = np.isfinite(scores)
    best = np.max(np.where(present, scores, -np.inf), axis=1)
    spread = best[:, None] - scores
    evidence = np.minimum(scores[None] - thresholds[:, None, :], gaps - spread)
    predicted = present & (evidence >= bands)
    rejected = present & (evidence < -bands)
    decisive = (predicted | rejected).all(axis=-1) & predicted.any(axis=-1)
    confidence = np.where(decisive, np.min(np.abs(evidence), axis=-1), -np.inf)
    return predicted, decisive, confidence


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(truth, predicted, decisive):
    actual_count = truth.sum(axis=1)
    exact = decisive & np.all(truth[None] == predicted, axis=-1)
    selected_count = predicted.sum(axis=-1)
    positives = (predicted & truth).sum(axis=(1, 2))
    false_positive = (predicted & ~truth).sum(axis=(1, 2))
    false_negative = (~predicted & truth).sum(axis=(1, 2))
    solo = actual_count == 1
    duet = actual_count == 2
    counts = {
        'solo_exact': exact[:, solo].sum(axis=1),
        'solo_false_multiple': (selected_count[:, solo] > 1).sum(axis=1),
        'solo_false_any': (predicted[:, solo] & ~truth[solo]).any(axis=-1).sum(axis=1),
        'duet_exact': exact[:, duet].sum(axis=1),
        'duet_false_extra': (predicted[:, duet] & ~truth[duet]).any(axis=-1).sum(axis=1),
        'three_exact': exact[:, actual_count == 3].sum(axis=1),
        'four_exact': exact[:, actual_count == 4].sum(axis=1),
        'decisive': decisive.sum(axis=1), 'exact': exact.sum(axis=1),
        'tp': positives, 'fp': false_positive, 'fn': false_negative,
    }
    total = len(truth)
    n_negative = int((~truth).sum())
    denominators = {'total': total, 'solo': int(solo.sum()), 'duet': int(duet.sum()),
                    'three': int((actual_count == 3).sum()), 'four': int((actual_count == 4).sum()),
                    'negative_candidate_decisions': n_negative}
    result = []
    for index in range(len(predicted)):
        row = {key: int(values[index]) for key, values in counts.items()}
        tp, fp, fn = (row[name] for name in ('tp', 'fp', 'fn'))
        result.append({**row, **denominators, 'coverage': ratio(row['decisive'], total),
                       'false_positive_rate': ratio(fp, n_negative), 'false_negative_rate': ratio(fn, tp + fn),
                       'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn),
                       'f1': ratio(2 * tp, 2 * tp + fp + fn)})
    return result


def grid(centers=None):
    levels = {'absolute': [index / 100 for index in range(-100, 101)]}
    if centers:
        levels['offset'] = [index / 100 for index in range(-20, 21)]
    return [Policy(mode, center, gap, band) for mode, values in levels.items()
            for center in values for gap in GAPS for band in BANDS]


def evaluate(data, policies, centers=None):
    names, truth, scores = data
    predicted, decisive, confidence = decisions(scores, names, policies, centers or {})
    return summarize(truth, predicted, decisive), (predicted, decisive, confidence)


def budget_passes(row, budget):
    return ((row['false_positive_rate'] or 0.) <= budget
            and (ratio(row['solo_false_multiple'], row['solo']) or 0.) <= budget)


def best_policy(rows, policies, budget):
    eligible = [i for i, row in enumerate(rows) if budget_passes(row, budget)]
    if not eligible:
        return None
    return max(eligible, key=lambda i: (rows[i]['duet_exact'], rows[i]['solo_exact'], rows[i]['exact'],
                                       -rows[i]['fp'], rows[i]['f1'] or 0., rows[i]['decisive'],
                                       policies[i].gap is None, -policies[i].band, policies[i].center))


def plateau(chosen, rows, policies):
    policy = policies[chosen]
    row = rows[chosen]
    relevant = ('duet_exact', 'solo_exact', 'solo_false_multiple', 'exact', 'fp', 'tp', 'decisive')
    values = {round(100 * candidate.center) for candidate, metrics in zip(policies, rows, strict=True)
              if (candidate.mode, candidate.gap, candidate.band) == (policy.mode, policy.gap, policy.band)
              and all(metrics[key] == row[key] for key in relevant)}
    left = right = round(100 * policy.center)
    while left - 1 in values:
        left -= 1
    while right + 1 in values:
        right += 1
    return [left / 100, right / 100]


def encoded_grid(policies, rows):
    policy_columns = ('mode', 'center', 'gap', 'band')
    count_columns = ('solo_exact', 'solo_false_multiple', 'solo_false_any', 'duet_exact', 'duet_false_extra',
                     'three_exact', 'four_exact', 'decisive', 'exact', 'tp', 'fp', 'fn')
    denominators = {name: rows[0][name] for name in ('total', 'solo', 'duet', 'three', 'four', 'negative_candidate_decisions')}
    return {'columns': (*policy_columns, *count_columns), 'denominators': denominators,
            'rows': [[*(getattr(policy, name) for name in policy_columns), *(row[name] for name in count_columns)]
                     for policy, row in zip(policies, rows, strict=True)]}


def freeze_policies(truth, scores, centers, *, include_grid=False):
    names, expected, raw = measurements(truth, scores)
    measured = np.isfinite(raw)
    if not ((measured & expected).any(axis=0) & (measured & ~expected).any(axis=0)).all():
        return {}, None
    policies = grid(centers if all(name in centers for name in names) else None)
    calibration, _ = evaluate((names, expected, raw), policies, centers)
    selected = {}
    for budget in BUDGETS:
        index = best_policy(calibration, policies, budget)
        if index is None:
            continue
        policy = policies[index]
        selected[str(budget)] = {'policy': asdict(policy), 'calibration': calibration[index],
                                 'calibration_plateau': plateau(index, calibration, policies),
                                 'effective_centers': {name: policy.center + (centers[name] if policy.mode == 'offset' else 0.)
                                                       for name in names}}
    return selected, encoded_grid(policies, calibration) if include_grid else None


def evaluate_frozen(selected, truth, scores):
    names, expected, raw = measurements(truth, scores)
    results, cases = {}, {}
    for budget, chosen in selected.items():
        centers = chosen['effective_centers']
        if set(centers) != set(names):
            raise ValueError('Calibration and test candidates differ')
        policy = Policy('offset', 0., chosen['policy']['gap'], chosen['policy']['band'])
        evaluated, masks = evaluate((names, expected, raw), [policy], centers)
        results[budget] = {**chosen, 'holdout': evaluated[0],
                           'holdout_meets_budget': budget_passes(evaluated[0], float(budget))}
        cases[budget] = (expected, *(mask[0] for mask in masks))
    return results, cases


def match_coverage(decisions_by_budget):
    matched = {}
    for budget, models in decisions_by_budget.items():
        common = min((int(raw[2].sum()) for raw in models.values()), default=0)
        matched[budget] = {'common_decisive_cases': common, 'models': {}}
        if common:
            for backend, (truth, predicted, decisive, confidence) in models.items():
                indices = np.argsort(-confidence, kind='stable')[:common]
                matched[budget]['models'][backend] = summarize(truth[indices], predicted[None, indices],
                                                               decisive[None, indices])[0]
    return matched


def unchanged_predictions(scores, threshold):
    scores = scores[np.isfinite(scores)]
    below = scores[scores < threshold]
    above = scores[scores >= threshold]
    return [float(below.max()) if len(below) else None, float(above.min()) if len(above) else None]


def exploratory(data):
    policies = grid()
    rows, _ = evaluate(data, policies)
    plain, _ = evaluate(data, [Policy('absolute', point) for point in ESCAPE_POINTS])
    # Solo false-multiple only, for a transparent picture of the historic duet tradeoff.
    best = {}
    for maximum in (0, 1, 2, 5):
        eligible = [i for i, row in enumerate(rows) if row['solo_false_multiple'] <= maximum]
        index = max(eligible, key=lambda i: (rows[i]['duet_exact'], rows[i]['solo_exact'], -rows[i]['fp'],
                                              rows[i]['decisive'], policies[i].gap is None, -policies[i].band))
        best[str(maximum)] = {'policy': asdict(policies[index]), 'evaluation': rows[index]}
    return {'plain_thresholds': [{'threshold': point, 'unchanged_predictions_interval': unchanged_predictions(data[2], point),
                                  **row} for point, row in zip(ESCAPE_POINTS, plain, strict=True)],
            'posthoc_best_by_solo_false_multiple_count': best,
            'grid': encoded_grid(policies, rows)}


def independent(args, heldout):
    # Current reports store scores in deterministic probe order. Reconstruct the missing
    # calibration labels from their checksum-verified sources, without recomputing identity.
    paths, facts = load_sources(args.manifest, args.audio_dir)
    for position, info in facts.items():
        if info['sha256'] != heldout['audio_sources'][str(position)]['sha256']:
            raise ValueError(f'Calibration source changed: #{position}')
    shared = SimpleNamespace(output=args.heldout, device=args.device, models_dir=args.models_dir)
    calibration_truth = [case[0] for case in probes(prepare(shared, paths, facts), test=False)]
    if len(calibration_truth) != heldout['calibration_cases']:
        raise ValueError('Calibration probe order or count changed')
    variants = heldout['variants']
    result = {}
    decisions_by_budget = {str(budget): {} for budget in BUDGETS}
    for backend, variant in variants.items():
        if 'rows' not in variant:
            continue
        scores = variant.get('calibration_scores')
        if scores is None and backend == 'sony-topk+ecapa-max':
            left = variants['sony:topk']['calibration_scores']
            right = variants['ecapa:max']['calibration_scores']
            scores = combine(left, right, left, right)
        if scores is None:
            continue
        centers = variant.get('thresholds', {})
        frozen, calibration_grid = freeze_policies(calibration_truth, scores, centers, include_grid=True)
        selected, cases = evaluate_frozen(frozen, [row['truth'] for row in variant['rows']],
                                          [row['scores'] for row in variant['rows']])
        for budget, masks in cases.items():
            decisions_by_budget[budget][backend] = masks
        result[backend] = {'initial_candidate_centers': centers, 'selected': selected,
                           'calibration_grid': calibration_grid}
    return {'calibration_cases': len(calibration_truth), 'calibration_truth_in_score_order': calibration_truth,
            'holdout_cases': heldout['test_cases'],
            'variants': result, 'matched_holdout_coverage': match_coverage(decisions_by_budget)}


def fmt_rate(value):
    return '—' if value is None else f'{value:.1%}'


def fmt_policy(policy):
    center = policy['center']
    suffix = f", gap ≤ {policy['gap']:.3f}" if policy['gap'] is not None else ''
    suffix += f", band ±{policy['band']:.3f}" if policy['band'] else ''
    return (f'T {center:.2f}' if policy['mode'] == 'absolute' else f'offset {center:+.2f}') + suffix


def compact(row):
    return [f"{row['duet_exact']}/{row['duet']}", f"{row['solo_false_multiple']}/{row['solo']}",
            f"{row['solo_exact']}/{row['solo']}", f"{row['decisive']}/{row['total']}",
            fmt_rate(row['false_positive_rate'])]


def report(results):
    lines = ['# 가창자 판정 기준값 비교', '',
             '후보마다 코사인 점수 기준값을 적용했다. 선택적으로 가장 높은 점수와의 허용 격차를 적용했다. '
             '보류 폭 안에 있거나 점수가 없는 후보가 하나라도 있으면 줄 전체를 확정으로 집계하지 않았다. '
             '표의 FPR은 실제로 참여하지 않은 후보 전체 중 잘못 선택한 비율이다.', '',
             '## エスケープ 보관 ECAPA 점수', '',
             '솔로 25줄과 듀엣 24줄의 사후 비교다. 두 후보만 포함되어 듀엣에 제3·제4의 인물이 잘못 선택되는지는 측정할 수 없다.', '',
             '| 기준값 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | 확정 | FPR |',
             '|---:|---:|---:|---:|---:|---:|']
    for row in results['escape_archive_posthoc']['plain_thresholds']:
        lines.append('| ' + ' | '.join([f"{row['threshold']:.2f}", *compact(row)]) + ' |')
    compromise = next(row for row in results['escape_archive_posthoc']['plain_thresholds'] if row['threshold'] == .52)
    lower, upper = compromise['unchanged_predictions_interval']
    lines += ['', f'보관 점수에서는 `{lower:.9f} < T ≤ {upper:.9f}`가 0.52와 동일한 후보를 선택했다. '
                   '0.53에서는 솔로 복수 오검출이 0건으로 줄고 듀엣 완전 일치가 3건으로 감소했다.', '',
              '## エスケープ 동일 녹음에서 추출한 진단 점수', '',
              '원본의 30초 구간과 10초 완충 시간을 사용한 앞선 교차 적합의 점수를 다시 집계했다. '
              '아래 기준값은 이 점수에서 사후에 골랐으며, 같은 녹음의 음색과 믹싱이 공유된다.', '',
              '| 모델 | 솔로 복수 오검출 1건 이하에서 고른 기준 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | 확정 | FPR |',
              '|---|---|---:|---:|---:|---:|---:|']
    for name, model in results['escape_same_recording_posthoc'].items():
        chosen = model['posthoc_best_by_solo_false_multiple_count']['1']
        lines.append('| ' + ' | '.join([name, fmt_policy(chosen['policy']), *compact(chosen['evaluation'])]) + ' |')
    suite = results.get('independent_four_candidates')
    if suite:
        lines += ['', '## 녹음을 분리한 4후보 평가', '',
                  f"보정 {suite['calibration_cases']}개, 평가 {suite['holdout_cases']}개. 다른 곡에서 만든 보정 혼합물에서만 기준을 선택했다. "
                  '각 허용치에서 보정 자료의 FPR과 솔로 복수 오검출률을 둘 다 제한한 뒤 듀엣 완전 일치를 우선했다. '
                  'offset은 모델별 후보 기준값에 같은 양을 더하는 방식이다. JSON에 0%, 5%, 10%, 20%의 기준과 세부 지표를 기록했다.', '',
                  '| 모델 | 보정 10% 정책 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | 확정 | 평가 FPR |',
                  '|---|---|---:|---:|---:|---:|---:|']
        for name, variant in suite['variants'].items():
            chosen = variant['selected'].get('0.1')
            if chosen:
                lines.append('| ' + ' | '.join([name, fmt_policy(chosen['policy']), *compact(chosen['holdout'])]) + ' |')
        lines += ['', '### ECAPA 기본 집계의 허용치별 결과', '',
                  '| 보정 허용치 | 정책 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | 확정 | 평가 FPR |',
                  '|---:|---|---:|---:|---:|---:|---:|']
        for budget, chosen in suite['variants']['ecapa:max']['selected'].items():
            lines.append('| ' + ' | '.join([f'{float(budget):.0%}', fmt_policy(chosen['policy']), *compact(chosen['holdout'])]) + ' |')
        lines += ['', 'ECAPA의 5% 정책은 독립 평가에서도 두 오검출 제한을 충족했다. 듀엣 일치는 1/49였다. '
                      '20% 정책의 듀엣 일치는 2/49였고 솔로 완전 일치는 0/20이었다. '
                      '4후보 평가에서는 기본값으로 채택할 정도로 듀엣 일치율이 높은 기준 범위를 확인할 수 없었다.']
        lines += ['', '### 동일 확정 수 비교', '',
                  '각 모델에서 참여 판정과 보류 경계 사이의 거리가 가장 큰 줄부터 추려 공통 확정 수를 맞췄다. '
                  '각 모델이 추린 줄은 다를 수 있다. 공통 확정이 0이면 비교할 수 없다.', '',
                  '| 보정 허용치 | 공통 확정 줄 수 |', '|---:|---:|']
        for budget, matched in suite['matched_holdout_coverage'].items():
            lines.append(f"| {float(budget):.0%} | {matched['common_decisive_cases']} |")
        comparison = suite['matched_holdout_coverage']['0.1']
        if comparison['models']:
            lines += ['', '| 모델 | 10% 보정 정책의 공통 확정 표본 | 듀엣 완전 일치 | 솔로 복수 오검출 | 솔로 완전 일치 | FPR |',
                      '|---|---:|---:|---:|---:|---:|']
            for name, row in comparison['models'].items():
                duet, multiple, solo, _, fpr = compact(row)
                lines.append('| ' + ' | '.join([name, str(row['total']), duet, multiple, solo, fpr]) + ' |')
    return '\n'.join(lines) + '\n'


def main():
    base = ROOT / 'output/voice-models'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--escape-archive', type=Path, default=base / 'escape-gold-parts-test.json')
    parser.add_argument('--escape-diagnostic', type=Path, default=base / 'escape-real-analysis.json')
    parser.add_argument('--heldout', type=Path, default=base / 'singing-benchmark-cuda.json')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models/character-voice')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'output/playlist-youtube/idolmaster-solo-links.json')
    parser.add_argument('--audio-dir', type=Path, default=ROOT / 'output/mp3')
    parser.add_argument('--device', default='cpu', help='Vocal-stem preparation device if the cache is absent')
    parser.add_argument('--output', type=Path, default=base / 'singing-threshold-sweep.json')
    parser.add_argument('--markdown', type=Path, default=base / 'singing-threshold-sweep.md')
    args = parser.parse_args()
    archive = load_json(args.escape_archive)
    gold = [row for row in archive['rows'] if row['category'] in ('solo', 'duet') and row.get('scores')]
    escape = measurements([row['expected'] for row in gold], [row['scores'] for row in gold])
    diagnostic = load_json(args.escape_diagnostic)['diagnostic']
    results = {'escape_archive_posthoc': exploratory(escape), 'escape_same_recording_posthoc': {}}
    for name, model in diagnostic['variants'].items():
        rows = model['rows']
        results['escape_same_recording_posthoc'][name] = exploratory(
            measurements([row['truth'] for row in rows], [row['scores'] for row in rows]))
    results['independent_four_candidates'] = independent(args, load_json(args.heldout)['held_out_local'])
    for path in (args.output, args.markdown):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    args.markdown.write_text(report(results), encoding='utf-8')
    print(args.output)
    print(args.markdown)


if __name__ == '__main__':
    main()
