"""Experimental singer analysis on a recording's original time axis."""
from hashlib import sha256
import json
from pathlib import Path

from character_voice import SAMPLE_RATE, VoiceAnalyzer, VoiceAudio, _windows
from singing_assignment import BACKENDS, assign_singers, assignment_features
from singing_overlap import overlap_features, predict_overlap


STRATEGY = 'overlap-assignment'
WINDOW_MS = 2400
HOP_MS = 1200


def matched_profiles(profiles):
    """Restrict each character to recordings represented in all encoders."""
    common = {}
    for candidate in profiles[BACKENDS[0]]:
        sources = [{sample['audio_sha256'] for sample in profiles[backend][candidate]
                    if sample.get('audio_sha256')} for backend in BACKENDS]
        common[candidate] = set.intersection(*sources)
    return {backend: {name: [sample for sample in exemplars if sample.get('audio_sha256') in common[name]]
                      for name, exemplars in candidates.items()}
            for backend, candidates in profiles.items()}


def analysis_windows(start, end, width=WINDOW_MS):
    """Keep short context inside the lyric interval and retain overlapping evidence."""
    if end <= start:
        return []
    if end - start <= width:
        return [(start, end, start, end)]
    return [(left, right, context, context + width)
            for left, right in _windows(start, end, HOP_MS)
            for context in (max(start, min(end - width, (left + right - width) // 2)),)]


def summarize_windows(windows, candidates):
    if not windows:
        return {'overlap': {'status': 'uncertain', 'count': None, 'min_count': None}, 'singers': [],
                'decisions': dict.fromkeys(candidates, 'uncertain'), 'scores': dict.fromkeys(candidates),
                'ranked': [], 'status': 'unavailable', 'estimated_count': None}
    first = windows[0]
    count = {key: first['overlap'][key] for key in ('status', 'count', 'min_count')}
    if any(any(window['overlap'][key] != value for key, value in count.items()) for window in windows[1:]):
        count = {'status': 'varies', 'count': None, 'min_count': None}
    same_identity = all(set(window['singers']) == set(first['singers']) and window['status'] == 'estimated'
                        for window in windows)
    states = {candidate: {window['decisions'][candidate] for window in windows} for candidate in candidates}
    decisions = {candidate: next(iter(values)) if len(values) == 1 else 'uncertain'
                 for candidate, values in states.items()}
    scores = {candidate: max((window['scores'][candidate] for window in windows
                              if window['scores'][candidate] is not None), default=None)
              for candidate in candidates}
    ranked = sorted((name for name in candidates if scores[name] is not None), key=scores.get, reverse=True)
    status = 'uncertain' if any(window['ranked'] for window in windows) else 'unavailable'
    if count['status'] == 'no_vocal':
        status = 'no_vocal'
    elif same_identity:
        status = 'estimated'
    return {'overlap': count, 'singers': first['singers'] if same_identity else [], 'decisions': decisions,
            'scores': scores, 'ranked': ranked or first['ranked'], 'status': status,
            'estimated_count': first['estimated_count'] if same_identity else None}


class SingingAnalysis:
    def __init__(self, cache_dir, *, models_dir, device='auto', calibration_path=None, verify_targets=False):
        self.device = device
        self.models_dir = Path(models_dir)
        self.analyzers = {backend: VoiceAnalyzer(cache_dir, models_dir=models_dir, device=device,
                                                 singer_model=backend) for backend in BACKENDS}
        self.separator = None
        self.verify_targets = verify_targets
        self.calibration, self.calibration_id = {}, None
        if calibration_path is not None:
            path = Path(calibration_path)
            if path.is_file():
                body = path.read_bytes()
                self.calibration = json.loads(body)
                if self.calibration.get('version') != 1 or self.calibration.get('window_ms') != WINDOW_MS:
                    raise ValueError('가창 인원수·신원 보정 파일의 버전 또는 분석 길이가 일치하지 않습니다.')
                self.calibration_id = sha256(body).hexdigest()

    def model_ids(self):
        models = {backend: analyzer.model_id('singing') for backend, analyzer in self.analyzers.items()}
        expected = self.calibration.get('model_ids')
        if expected and expected != models:
            raise ValueError('가창 신원 보정에 사용한 체크포인트가 현재 모델과 다릅니다.')
        return models

    def prepare(self, path, *, isolated, source_hash):
        return self.analyzers['ecapa'].prepare(path, isolated=isolated, source_hash=source_hash)

    def _separate(self, samples):
        from singing_voice_separation import UnmixxSeparator

        if self.separator is None:
            self.separator = UnmixxSeparator(self.models_dir, device=self.device)
        return self.separator.separate(samples, sample_rate=SAMPLE_RATE)

    def score(self, audio, *, ranges, profiles):
        import numpy as np

        compares = {backend: analyzer.profile_comparator('singing', profiles[backend])
                    for backend, analyzer in self.analyzers.items()}
        candidates = list(profiles[BACKENDS[0]])
        enabled = [backend for backend in BACKENDS if any(profiles[backend].values())]
        primary = self.analyzers['sony']
        target_error = None
        targets = None
        if self.verify_targets and 'sony' in enabled:
            try:
                targets = primary._enroll_targets(profiles['sony'])
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                target_error = f'{type(error).__name__}: {error}'
        analyses = {}
        separation_error = None

        def scores(probe, left, right):
            return {backend: compare(self.analyzers[backend]._window(probe, left, right)[1]
                                     if backend in enabled else None)
                    for backend, compare in compares.items()}

        def probe(left, right):
            nonlocal separation_error, target_error
            begin, finish = left * SAMPLE_RATE // 1000, right * SAMPLE_RATE // 1000
            mixture = audio.samples[begin:finish]
            quality, active = primary._signal(audio, left, right)
            acoustics = overlap_features(mixture)
            stem_scores, qualities, target_evidence = [], [], {}
            if active is not None and not separation_error:
                try:
                    stems = self._separate(mixture)
                    acoustics = overlap_features(mixture, stems)
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    separation_error = f'{type(error).__name__}: {error}'
                else:
                    energy = max(float(np.dot(mixture, mixture)), 1e-12)
                    for index, stem in enumerate(stems):
                        source = (f'{audio.source_hash}:{audio.preparation_id}:{left}:{right}:{index}'
                                  if audio.source_hash else None)
                        clip = VoiceAudio(stem, 'singing', self.separator.version, source)
                        details, _ = primary._signal(clip, 0, clip.duration_ms)
                        qualities.append({**(details or {}), 'energy_fraction': float(np.dot(stem, stem)) / energy})
                        stem_scores.append(scores(clip, 0, clip.duration_ms))
                    if targets and not target_error:
                        try:
                            target_evidence = primary._target_evidence(mixture, energy, targets, compares['sony'])['targets']
                        except (ImportError, OSError, RuntimeError, ValueError) as error:
                            target_error = f'{type(error).__name__}: {error}'
            overlap = {**predict_overlap(acoustics, self.calibration.get('overlap')), 'features': acoustics}
            mixed = scores(audio, left, right) if active is not None else {name: dict.fromkeys(candidates) for name in BACKENDS}
            evidence = assignment_features(mixed, stem_scores, overlap, qualities=qualities, targets=target_evidence)
            assignment = assign_singers(evidence, overlap, self.calibration.get('assignment'))
            result = {'analysis_start_ms': left, 'analysis_end_ms': right, 'quality': quality, 'overlap': overlap,
                      'model_scores': mixed, 'stem_scores': stem_scores, 'target_evidence': target_evidence, **assignment}
            if separation_error:
                result['separation_error'] = separation_error
            if target_error:
                result['target_error'] = target_error
            return result

        rows = []
        for start, end in ranges:
            start = max(0, min(int(start), audio.duration_ms))
            end = max(start, min(int(end), audio.duration_ms))
            segments = []
            for left, right, context_left, context_right in analysis_windows(start, end):
                key = context_left, context_right
                if key not in analyses:
                    analyses[key] = probe(*key)
                segments.append({'start_ms': left, 'end_ms': right, **analyses[key]})
            rows.append({'start_ms': start, 'end_ms': end, 'subsegments': segments,
                         'score_type': 'presence_margin', **summarize_windows(segments, candidates)})
        self._reference_diagnostics(audio, rows)
        return rows

    def _reference_diagnostics(self, audio, rows):
        """Use independently verified solos as temporary, non-promoting comparisons."""
        import numpy as np

        analyzer = self.analyzers['sony']
        anchors = {}
        for row in rows:
            for window in row['subsegments']:
                if window['status'] == 'estimated' and window['overlap']['status'] == 'solo':
                    singer, = window['singers']
                    anchors.setdefault(singer, []).append(window)
        for windows in anchors.values():
            windows.sort(key=lambda item: item['scores'][item['singers'][0]], reverse=True)
        if not anchors:
            return
        vectors = {}

        def vector(window):
            key = window['analysis_start_ms'], window['analysis_end_ms']
            if key not in vectors:
                vectors[key] = analyzer._window(audio, *key)[1]
            return vectors[key]

        for row in rows:
            for window in row['subsegments']:
                refs = {name: next((anchor for anchor in anchors[name]
                                    if anchor['analysis_end_ms'] + 8000 <= window['analysis_start_ms']
                                    or window['analysis_end_ms'] + 8000 <= anchor['analysis_start_ms']), None)
                        for name in anchors}
                if not any(refs.values()):
                    continue
                current = vector(window)
                if current is not None:
                    window['recording_reference_scores'] = {name: float(np.dot(current, reference))
                                                            for name, anchor in refs.items() if anchor
                                                            if (reference := vector(anchor)) is not None}
