"""Calibrated candidate verification and injective assignment of two vocal stems."""
from itertools import combinations
import math


BACKENDS = ('sony', 'sony-contrastive', 'ecapa')


def _margin(scores, candidate):
    score = scores.get(candidate)
    competitors = [value for name, value in scores.items() if name != candidate and value is not None]
    return score - max(competitors) if score is not None and competitors else None


def assignment_features(mixture_scores, stem_scores, overlap, *, qualities=None, targets=None, temporal=None):
    """Retain each encoder's scale and use missing features explicitly."""
    candidates = list(dict.fromkeys(name for scores in mixture_scores.values() for name in scores))
    qualities = qualities or [{} for _ in stem_scores]
    targets, temporal = targets or {}, temporal or {}
    acoustics = overlap.get('features', {})
    presence = {}
    edges = [{} for _ in stem_scores]
    for name in candidates:
        target = targets.get(name, {})
        features = {'overlap_score': overlap.get('score'),
                    'overlap_minimum': overlap.get('min_count'),
                    'stem_waveform_correlation': acoustics.get('waveform_correlation'),
                    'stem_spectral_divergence': acoustics.get('spectral_divergence'),
                    'stem_residual_ratio': acoustics.get('residual_ratio'),
                    'stem_coactivity': acoustics.get('coactive_fraction'),
                    'target_similarity': target.get('target_similarity'),
                    'target_margin': target.get('target_margin'),
                    'target_residual_similarity': target.get('residual_similarity'),
                    'target_residual_gain': target.get('residual_gain'),
                    'target_tonal_fraction': (target.get('quality') or {}).get('tonal_fraction')}
        history = temporal.get(name, ())
        features['neighbor_support'] = sum(history) / len(history) if history else None
        for backend in BACKENDS:
            mixed = mixture_scores.get(backend, {})
            stem_values = [scores.get(backend, {}).get(name) for scores in stem_scores]
            ranked = sorted((value for value in stem_values if value is not None), reverse=True)
            features[f'{backend}:mixed'] = mixed.get(name)
            features[f'{backend}:mixed_margin'] = _margin(mixed, name)
            features[f'{backend}:best_stem'] = ranked[0] if ranked else None
            features[f'{backend}:stem_spread'] = ranked[0] - ranked[1] if len(ranked) == 2 else None
            for index, scores in enumerate(stem_scores):
                edge = edges[index].setdefault(name, {})
                value = stem_values[index]
                other = stem_values[1 - index] if len(stem_values) == 2 else None
                edge[f'{backend}:stem'] = value
                edge[f'{backend}:stem_margin'] = _margin(scores.get(backend, {}), name)
                edge[f'{backend}:other_stem_gap'] = value - other if value is not None and other is not None else None
                edge[f'{backend}:mixed'] = mixed.get(name)
        for index, quality in enumerate(qualities):
            if index >= len(edges):
                break
            edge = edges[index][name]
            for field in ('active_fraction', 'tonal_fraction', 'rms_dbfs', 'clipped_fraction', 'energy_fraction'):
                edge[field] = (quality or {}).get(field)
            edge['waveform_correlation'] = acoustics.get('waveform_correlation')
            edge['residual_ratio'] = acoustics.get('residual_ratio')
        presence[name] = features
    return {'presence': presence, 'edges': edges, 'mixture_scores': mixture_scores,
            'stem_scores': stem_scores, 'overlap': overlap}


def _design(rows, columns, centers, scales):
    import numpy as np

    values = np.array([[row.get(key) if row.get(key) is not None else np.nan for key in columns]
                       for row in rows], dtype=float)
    missing = ~np.isfinite(values)
    standardized = np.where(missing, 0., (values - centers) / scales)
    return np.concatenate([np.clip(standardized, -8., 8.), missing.astype(float),
                           np.ones((len(rows), 1))], axis=1)


def _fit(rows, labels):
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit

    if min(sum(labels), len(labels) - sum(labels)) < 4:
        return None
    columns = [key for key in rows[0] if sum(row.get(key) is not None and math.isfinite(row[key])
                                           for row in rows) >= 4]
    if not columns:
        return None
    finite = [[row[key] for row in rows if row.get(key) is not None and math.isfinite(row[key])] for key in columns]
    centers = np.array([np.median(values) for values in finite])
    scales = np.array([max(np.std(values), 1e-5) for values in finite])
    matrix = _design(rows, columns, centers, scales)
    labels = np.asarray(labels, dtype=float)
    weights = np.where(labels == 1., .5 / labels.sum(), .5 / (len(labels) - labels.sum()))

    def loss(coefs):
        logits = matrix @ coefs
        penalty = .025 * np.dot(coefs[:-1], coefs[:-1])
        gradient = matrix.T @ ((expit(logits) - labels) * weights)
        gradient[:-1] += .05 * coefs[:-1]
        return np.dot(weights, np.logaddexp(0., logits) - labels * logits) + penalty, gradient

    fit = minimize(loss, np.zeros(matrix.shape[1]), method='L-BFGS-B', jac=True)
    if not fit.success or not np.isfinite(fit.x).all():
        return None
    scores = matrix @ fit.x
    lower = min(float(np.quantile(scores[labels == 1], .1)), 0.)
    upper = max(float(np.quantile(scores[labels == 0], .9)), 0.)
    return {'columns': columns, 'centers': centers.tolist(), 'scales': scales.tolist(),
            'weights': fit.x.tolist(), 'lower': lower, 'upper': upper}


def _predict(rows, model):
    if not rows or model is None:
        return [None] * len(rows)
    design = _design(rows, model['columns'], model['centers'], model['scales'])
    return [float(score) for score in design @ model['weights']]


def fit_assignment_model(examples):
    """Fit presence and stem association from recordings reserved for calibration."""
    presences, present_labels, stems, stem_labels = [], [], [], []
    for evidence, truth, stem_truths in examples:
        truth = set(truth)
        for name, values in evidence['presence'].items():
            if any(values.get(f'{backend}:mixed') is not None for backend in BACKENDS):
                presences.append(values)
                present_labels.append(name in truth)
        for edge, expected in zip(evidence['edges'], stem_truths or ()):
            for name, values in edge.items():
                if (expected is not None or name not in truth) and any(
                        values.get(f'{backend}:stem') is not None for backend in BACKENDS):
                    stems.append(values)
                    stem_labels.append(name == expected)
    presence = _fit(presences, present_labels)
    edge = _fit(stems, stem_labels)
    required = [backend for backend in BACKENDS if presence and f'{backend}:mixed' in presence['columns']]
    model = {'version': 1, 'presence': presence, 'edge': edge, 'required_backends': required,
            'calibration_examples': len(examples), 'calibration_stem_examples': len(stems)}
    pairs, pair_labels = [], []
    if presence and edge:
        for evidence, truth, _ in examples:
            overlap = evidence.get('overlap') or {}
            scores = _presence_scores(evidence, model)
            for _, left, right, _, _, features in _hypotheses(evidence, overlap, scores, edge):
                pairs.append(features)
                pair_labels.append({left, right} == set(truth))
    return {**model, 'pair': _fit(pairs, pair_labels)}


def _decision(score, model):
    if score is not None:
        if score > model['upper']:
            return 'present'
        if score < model['lower']:
            return 'absent'
    return 'uncertain'


def _presence_scores(evidence, model):
    features = evidence['presence']
    required = model.get('required_backends', ())
    supported = [name for name, values in features.items()
                 if any(values.get(f'{backend}:mixed') is not None for backend in BACKENDS)
                 and all(values.get(f'{backend}:mixed') is not None for backend in required)]
    scores = dict.fromkeys(features)
    scores.update(zip(supported, _predict([features[name] for name in supported], model.get('presence'))))
    return scores


def _hypotheses(evidence, overlap, scores, edge_model):
    if not edge_model or len(evidence['edges']) != 2:
        return []
    candidates = [name for name, value in scores.items() if value is not None]
    edges = []
    for edge in evidence['edges']:
        available = [name for name in candidates if any(
            edge[name].get(f'{backend}:stem') is not None for backend in BACKENDS)]
        edges.append(dict(zip(available, _predict([edge[name] for name in available], edge_model))))
    hypotheses = []
    for first, second in combinations(candidates, 2):
        permutations = []
        for left, right in ((first, second), (second, first)):
            a, b = edges[0].get(left), edges[1].get(right)
            if a is not None and b is not None:
                permutations.append((a + b, left, right, a, b))
        if not permutations:
            continue
        permutations.sort(reverse=True)
        total, left, right, a, b = permutations[0]
        rival = max((scores[name] for name in candidates if name not in (left, right)), default=None)
        minimum = min(scores[left], scores[right])
        features = {'weaker_edge': min(a, b), 'stronger_edge': max(a, b), 'total': total,
                    'permutation_margin': total - permutations[1][0] if len(permutations) == 2 else None,
                    'weaker_presence': minimum, 'stronger_presence': max(scores[left], scores[right]),
                    'other_presence': rival, 'presence_gap': minimum - rival if rival is not None else None,
                    'overlap_score': overlap.get('score'),
                    'waveform_correlation': overlap.get('features', {}).get('waveform_correlation')}
        hypotheses.append((total, left, right, a, b, features))
    return sorted(hypotheses, key=lambda row: row[0], reverse=True)


def assign_singers(evidence, overlap, model=None):
    """Require independently compatible count, presence and distinct stem evidence."""
    model = model or {}
    presence_model, edge_model = model.get('presence'), model.get('edge')
    features = evidence['presence']
    available = [name for name, values in features.items() if any(
        values.get(f'{backend}:mixed') is not None for backend in BACKENDS)]
    scores = _presence_scores(evidence, model)
    decisions = {name: _decision(score, presence_model) for name, score in scores.items()}
    present = [name for name, state in decisions.items() if state == 'present']
    complete = bool(present) and 'uncertain' not in decisions.values()
    ranked = sorted((name for name, score in scores.items() if score is not None), key=scores.get, reverse=True)
    if not ranked:
        # Expose diagnostic ranks without treating incomparable model cosines as one score.
        raw = next((scores for scores in evidence['mixture_scores'].values()
                    if any(value is not None for value in scores.values())), {})
        ranked = sorted((name for name, score in raw.items() if score is not None), key=raw.get, reverse=True)
    result = {'singers': [], 'decisions': decisions, 'status': 'uncertain' if available else 'unavailable',
              'scores': scores, 'ranked': ranked, 'assignment': None, 'estimated_count': None}
    if overlap['status'] == 'no_vocal':
        return {**result, 'status': 'no_vocal', 'decisions': dict.fromkeys(features, 'absent')}
    if overlap['status'] == 'solo' and len(present) == 1:
        return {**result, 'singers': present, 'status': 'estimated' if complete else 'uncertain',
                'estimated_count': 1 if complete else None}
    if overlap['status'] != 'overlap' or not edge_model or len(evidence['edges']) != 2:
        return result

    hypotheses = _hypotheses(evidence, overlap, scores, edge_model)
    if not hypotheses:
        return result
    total, left, right, a, b, evidence_pair = hypotheses[0]
    alternatives = [value for value in (evidence_pair['permutation_margin'],
                                         total - hypotheses[1][0] if len(hypotheses) > 1 else None)
                    if value is not None]
    margin = min(alternatives) if alternatives else None
    result['assignment'] = {'stems': [left, right], 'scores': [a, b], 'alternative_margin': margin}
    duplicate = overlap.get('features', {}).get('waveform_correlation', 0.) >= .995
    pair_supported = (not duplicate and set(present) == {left, right} and margin is not None and margin > 0
                      and min(a, b) > edge_model['upper'])
    if pair_supported:
        result.update(singers=[left, right], status='estimated' if complete else 'uncertain',
                      estimated_count=2 if complete else None)
    if model.get('pair') and not duplicate:
        pair_model = model['pair']
        ranked_pairs = sorted(zip(_predict([row[-1] for row in hypotheses], pair_model), hypotheses),
                              key=lambda item: item[0], reverse=True)
        score, (_, first, second, first_edge, second_edge, evidence_pair) = ranked_pairs[0]
        pair = {first, second}
        competitor_gap = score - ranked_pairs[1][0] if len(ranked_pairs) > 1 else None
        permutation_gap = evidence_pair['permutation_margin']
        result['assignment']['joint'] = {'stems': [first, second], 'score': score,
                                         'competitor_margin': competitor_gap, 'permutation_margin': permutation_gap}
        if (score > pair_model['upper'] and (competitor_gap is None or competitor_gap > 0)
                and permutation_gap is not None and permutation_gap > 0
                and min(first_edge, second_edge) >= edge_model['lower']
                and all(decisions[name] != 'absent' for name in pair) and set(present) <= pair):
            if pair_supported and set(result['singers']) != pair:
                result.update(singers=[], status='uncertain', estimated_count=None)
            else:
                combined = {name: 'present' if name in pair else state for name, state in decisions.items()}
                complete = 'uncertain' not in combined.values()
                result.update(singers=[first, second], decisions=combined,
                              status='estimated' if complete else 'uncertain', estimated_count=2 if complete else None)
    return result
