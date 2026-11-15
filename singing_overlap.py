"""Singer-independent overlap evidence from a mixture and two aligned stems."""

FEATURE_NAMES = (
    'log_energy_ratio', 'coactive_fraction', 'lower_log_ratio', 'envelope_correlation',
    'waveform_correlation', 'residual_ratio', 'spectral_divergence',
    'pitch_disagreement', 'joint_periodicity', 'mixture_periodicity',
)
MODEL_VERSION = 'singing-overlap-v1'


def _correlation(first, second):
    import numpy as np

    first = first - first.mean()
    second = second - second.mean()
    norm = np.linalg.norm(first) * np.linalg.norm(second)
    return float(np.clip(np.dot(first, second) / norm, -1, 1)) if norm > 1e-12 else 0.


def _spectral_frames(audio, sample_rate):
    import numpy as np

    # Block averaging limits aliasing before the pitch autocorrelation.
    factor = max(1, sample_rate // 11025)
    audio = audio[:len(audio) // factor * factor].reshape(-1, factor).mean(axis=1)
    rate = sample_rate / factor
    length = round(rate * .08)
    hop = round(rate * .04)
    frames = np.lib.stride_tricks.sliding_window_view(audio, length)[::hop]
    frames = frames - frames.mean(axis=1, keepdims=True)
    transform_size = 1 << (2 * length - 1).bit_length()
    power = np.abs(np.fft.rfft(frames * np.hanning(length), n=transform_size, axis=1)) ** 2
    autocorrelation = np.fft.irfft(power, n=transform_size, axis=1)
    autocorrelation /= np.maximum(autocorrelation[:, :1], 1e-12)
    low, high = round(rate / 1000), round(rate / 75)
    candidates = autocorrelation[:, low:high]
    peaks = ((candidates > autocorrelation[:, low - 1:high - 1]) &
             (candidates >= autocorrelation[:, low + 1:high + 1]))
    peak_values = np.where(peaks, candidates, 0.)
    maximum = peak_values.max(axis=1)
    first_peak = (peaks & (peak_values >= maximum[:, None] * .88)).argmax(axis=1) + low
    frequencies = np.fft.rfftfreq(transform_size, 1 / rate)
    spectrum = np.sqrt(power[:, (frequencies >= 100) & (frequencies <= 4300)])
    spectrum /= np.maximum(np.linalg.norm(spectrum, axis=1, keepdims=True), 1e-12)
    return spectrum, rate / first_peak, maximum, np.mean(frames ** 2, axis=1)


def overlap_features(mixture, stems=None, sample_rate=44100):
    """Measure power, temporal independence, and periodic structure without identities."""
    import numpy as np

    mixture = np.asarray(mixture, dtype=np.float32)
    if (mixture.ndim != 1 or not len(mixture) or not np.isfinite(mixture).all()
            or sample_rate < 1000):
        raise ValueError('Expected finite mono audio at a supported sample rate')
    peak = float(np.max(np.abs(mixture)))
    result = {'peak': peak, 'has_stems': False}
    if stems is None:
        return result
    stems = np.asarray(stems, dtype=np.float32)
    if stems.shape != (2, len(mixture)) or not np.isfinite(stems).all():
        raise ValueError('Expected two finite stems with the same sample clock as the mixture')
    if len(mixture) < round(sample_rate * .12) or peak <= 1e-6:
        return result

    stem_power = np.mean(stems ** 2, axis=1)
    if stem_power.max() <= 1e-12:
        return result
    strong, weak = stems[np.argsort(stem_power)[::-1]]
    frame_length = round(sample_rate * .04)
    frames = np.lib.stride_tricks.sliding_window_view(stems, frame_length, axis=1)[:, ::frame_length // 2]
    frame_power = np.mean(frames ** 2, axis=2)
    local_low, local_high = np.sort(frame_power, axis=0)
    floor = max(float(np.quantile(local_high, .9)) * .01, 1e-12)
    active = local_high >= floor
    ratios = local_low[active] / np.maximum(local_high[active], 1e-12)
    residue = mixture - strong - weak

    first, first_pitch, first_period, first_power = _spectral_frames(strong, sample_rate)
    second, second_pitch, second_period, second_power = _spectral_frames(weak, sample_rate)
    _, _, mixture_period, mixture_power = _spectral_frames(mixture, sample_rate)
    coactive = np.minimum(first_power, second_power) >= floor
    both_periodic = coactive & (np.minimum(first_period, second_period) >= .35)
    cents = np.abs(1200 * np.log2(first_pitch[both_periodic] / second_pitch[both_periodic]))
    result.update(
        has_stems=True,
        log_energy_ratio=float(np.log10(max(float(stem_power.min()), 1e-12) /
                                        max(float(stem_power.max()), 1e-12))),
        coactive_fraction=float(np.mean(local_low[active] >= floor)) if len(ratios) else 0.,
        lower_log_ratio=float(np.log10(max(float(np.quantile(ratios, .25)), 1e-8))) if len(ratios) else -8.,
        envelope_correlation=_correlation(np.log(np.maximum(frame_power[0], 1e-12)),
                                          np.log(np.maximum(frame_power[1], 1e-12))),
        waveform_correlation=abs(_correlation(strong, weak)),
        # Some separators enforce mixture consistency. Fit this feature jointly
        # with the others, since a small residual by itself carries little evidence.
        residual_ratio=float(np.log1p(np.mean(residue ** 2) / max(np.mean(mixture ** 2), 1e-12))),
        spectral_divergence=float(np.mean(1 - np.sum(first[coactive] * second[coactive], axis=1)))
        if np.any(coactive) else 0.,
        pitch_disagreement=float(np.mean(cents > 90)) if len(cents) else 0.,
        joint_periodicity=float(np.mean(both_periodic)),
        mixture_periodicity=float(np.mean(mixture_period[mixture_power >= floor]))
        if np.any(mixture_power >= floor) else 0.,
    )
    return result


def _matrix(rows):
    import numpy as np

    return np.asarray([[row[name] for name in FEATURE_NAMES] for row in rows], dtype=np.float64)


def fit_overlap_model(feature_rows, counts, *, max_calibration_error=.10):
    """Fit a regularized binary classifier using calibration recordings only."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    if not 0 <= max_calibration_error <= .5:
        raise ValueError('max_calibration_error must be in [0, .5]')
    pairs = [(features, int(count) >= 2) for features, count in zip(feature_rows, counts, strict=True)
             if count and features.get('has_stems')]
    solo = sum(not target for _, target in pairs)
    overlap = len(pairs) - solo
    if min(solo, overlap) < 2:
        raise ValueError('Overlap calibration requires separate solo and overlapping examples')
    matrix = _matrix(row for row, _ in pairs)
    targets = np.asarray([target for _, target in pairs], dtype=bool)
    center = matrix.mean(axis=0)
    scale = np.maximum(matrix.std(axis=0), 1e-6)
    matrix = (matrix - center) / scale
    classifier = LogisticRegression(C=.5, class_weight='balanced', max_iter=500).fit(matrix, targets)
    score = classifier.decision_function(matrix)
    negatives, positives = score[~targets], score[targets]
    # Margin bounds are raw scores. Use observed order statistics to bound the
    # calibration errors, including when either side has only a few examples.
    allowed_solo = int(overlap * max_calibration_error)
    allowed_overlap = int(solo * max_calibration_error)
    solo_bound = min(0., float(np.sort(positives)[allowed_solo]))
    overlap_bound = max(0., float(np.sort(negatives)[solo - 1 - allowed_overlap]))
    return {'version': MODEL_VERSION, 'features': list(FEATURE_NAMES), 'center': center.tolist(),
            'scale': scale.tolist(), 'weights': classifier.coef_[0].tolist(),
            'intercept': float(classifier.intercept_[0]),
            'bands': {'solo': solo_bound, 'overlap': overlap_bound},
            'support': {'solo': solo, 'overlap': overlap}}


def predict_overlap(features, model=None):
    """Return a count independently of the performer profiles and assignment."""
    import numpy as np

    result = {'status': 'uncertain', 'count': None, 'min_count': None, 'score': None}
    if features['peak'] <= 1e-6:
        return {'status': 'no_vocal', 'count': 0, 'min_count': 0, 'score': None}
    if model is None or not features.get('has_stems'):
        return result
    if model['version'] != MODEL_VERSION or tuple(model['features']) != FEATURE_NAMES:
        raise ValueError('Overlap model has incompatible features')
    normalized = (_matrix([features])[0] - model['center']) / model['scale']
    score = float(np.dot(normalized, model['weights']) + model['intercept'])
    if not np.isfinite(score):
        raise ValueError('Overlap model produced an invalid decision score')
    if score < model['bands']['solo']:
        result.update(status='solo', count=1, min_count=1)
    elif score > model['bands']['overlap']:
        result.update(status='overlap', min_count=2)
    return {**result, 'score': score}
