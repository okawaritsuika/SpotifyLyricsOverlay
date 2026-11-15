"""Local vocal stems, voice exemplars and segment-level similarity evidence."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from sony_singer_identity import REFERENCE_WINDOW_MS, SONY_VARIANTS

if TYPE_CHECKING:
    import numpy as np


SAMPLE_RATE = 44100
MIN_WINDOW_MS = 750
SEPARATION_WEIGHTS = 'hdemucs_high_trained.pt'
ECAPA_WEIGHTS = 'ecapa/embedding_model.ckpt'


@dataclass(frozen=True)
class VoiceAudio:
    samples: 'np.ndarray'
    kind: str
    preparation_id: str
    source_hash: str | None = None

    @property
    def duration_ms(self):
        return math.ceil(len(self.samples) * 1000 / SAMPLE_RATE)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _reject_external_io(url, flags, options):
    raise OSError('다른 파일이나 네트워크를 참조하는 재생 목록은 지원하지 않습니다.')


def _decode(path, stereo):
    import av
    import numpy as np

    resampler = av.AudioResampler(format='fltp', layout='stereo' if stereo else 'mono', rate=SAMPLE_RATE)
    chunks = []
    total = 0
    with path.open('rb') as handle:
        with av.open(handle, io_open=_reject_external_io,
                     container_options={'protocol_whitelist': 'file'}) as audio:
            if not audio.streams.audio:
                raise ValueError('오디오 스트림이 없습니다.')
            for frame in audio.decode(audio=0):
                for output in resampler.resample(frame):
                    total += output.samples
                    if total > 3600 * SAMPLE_RATE:
                        raise ValueError('한 작업에는 최대 1시간 길이의 음원을 등록할 수 있습니다.')
                    chunks.append(output.to_ndarray())
            chunks.extend(output.to_ndarray() for output in resampler.resample(None))
    if not chunks:
        raise ValueError('디코딩된 오디오가 없습니다.')
    samples = np.concatenate(chunks, axis=1)
    if not np.isfinite(samples).all():
        raise ValueError('오디오에 유효하지 않은 샘플이 있습니다.')
    return samples


def _windows(start, end, length):
    boundaries = list(range(start, end, length))
    if len(boundaries) > 1 and end - boundaries[-1] < MIN_WINDOW_MS:
        boundaries.pop()
    return list(zip(boundaries, boundaries[1:] + [end]))


def _quality(samples):
    import numpy as np

    frame_length = SAMPLE_RATE // 50
    frames = samples[:len(samples) // frame_length * frame_length].reshape(-1, frame_length)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    peak_rms = float(np.quantile(rms, .9))
    active = rms >= max(.002, peak_rms * .12)
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(frame_length), axis=1)) ** 2 + 1e-10
    flatness = np.exp(np.mean(np.log(spectrum), axis=1)) / np.mean(spectrum, axis=1)
    activity = float(np.mean(active))
    tonal_activity = float(np.mean(active & (flatness < .35)))
    clipped = float(np.mean(np.abs(samples) >= .999))
    return {'active_fraction': round(activity, 5), 'rms_dbfs': round(20 * math.log10(max(peak_rms, 1e-8)), 2),
            'tonal_fraction': round(tonal_activity, 5), 'clipped_fraction': round(clipped, 5)}


@lru_cache(maxsize=1)
def _spectral_bands():
    import numpy as np

    frequencies = np.fft.rfftfreq(4096, 1 / SAMPLE_RATE)
    edges = np.geomspace(75, 9500, 66)
    lower, center, upper = edges[:-2], edges[1:-1], edges[2:]
    rising = (frequencies[:, None] - lower) / (center - lower)
    falling = (upper - frequencies[:, None]) / (upper - center)
    return np.maximum(0., np.minimum(rising, falling)), np.hanning(4096)


def _spectral_embedding(samples):
    import numpy as np

    bands, window = _spectral_bands()
    frames = np.lib.stride_tricks.sliding_window_view(samples, len(window))[::2205]
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    return np.sqrt(np.mean(power, axis=0) @ bands)


def _unit(vector):
    import numpy as np

    vector = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(vector)
    if vector.ndim != 1 or not np.isfinite(norm) or norm <= 0:
        raise ValueError('유효한 음성 임베딩을 얻지 못했습니다.')
    return vector / norm


class VoiceAnalyzer:
    def __init__(self, cache_dir, *, device='cpu', singer_model='ecapa', speech_model=None,
                 models_dir=None, thresholds=None, default_threshold=None, aggregation=None,
                 calibration=None, max_score_gap=None, separator=None):
        if singer_model not in (*SONY_VARIANTS, 'ecapa', 'spectral'):
            raise ValueError(f'지원하지 않는 가창 음성 모델: {singer_model}')
        if speech_model not in (None, 'ecapa', 'spectral'):
            raise ValueError(f'지원하지 않는 대사 음성 모델: {speech_model}')
        self.cache_dir = Path(cache_dir)
        self.models_dir = Path(models_dir) if models_dir is not None else self.cache_dir / 'models'
        self.device = device
        self.actual_device = None
        self.fallback_reason = None
        self.backends = {'singing': singer_model,
                         'speech': speech_model or ('spectral' if singer_model == 'spectral' else 'ecapa')}
        self.aggregation = aggregation or ('topk' if singer_model in SONY_VARIANTS else 'max')
        if self.aggregation not in ('max', 'mean', 'topk'):
            raise ValueError('가창 프로필 집계 방식은 max, mean, topk 중 하나여야 합니다.')
        self.calibration = calibration or {}
        for bounds in self.calibration.values():
            if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
                    or not all(type(bound) in (float, int) for bound in bounds)
                    or not -1 <= bounds[0] <= bounds[1] <= 1):
                raise ValueError('가창자 보정에는 음성 부재 및 참여 기준 두 개가 필요합니다.')
        if max_score_gap is not None and (type(max_score_gap) not in (float, int) or not 0 <= max_score_gap <= 2):
            raise ValueError('가창 후보 간 허용 점수 차이는 0과 2 사이여야 합니다.')
        self.max_score_gap = max_score_gap
        if separator not in (None, 'unmixx', 'singer-informed'):
            raise ValueError('지원하지 않는 복수 보컬 분리 모델입니다.')
        self.separator_backend = separator
        self._singer_separator = None
        self.thresholds = dict(thresholds or {})
        self.default_threshold = default_threshold
        for value in [*self.thresholds.values(), default_threshold]:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not -1 <= value <= 1):
                raise ValueError('음성 유사도 기준은 -1과 1 사이여야 합니다.')
        self._identities = {}
        self._encoders = {}
        self._separator = None

    def _model_path(self, relative):
        path = self.models_dir / relative
        if not path.is_file():
            raise FileNotFoundError(f'로컬 음성 모델 파일이 필요합니다: {path}')
        return path

    def _model_hash(self, *relative_paths):
        hashes = ''.join(_sha256(self._model_path(path)) for path in relative_paths)
        return hashlib.sha256(hashes.encode('ascii')).hexdigest()

    def model_id(self, kind):
        backend = self.backends[kind]
        if kind not in self._identities:
            if backend in SONY_VARIANTS:
                folder = SONY_VARIANTS[backend]
                digest = self._model_hash(f'{folder}/model.pt', f'{folder}/hyperparams.yaml')
                model = f'sony-csl-{folder.removeprefix("sony-")}-v2:{digest}'
            elif backend == 'ecapa':
                digest = self._model_hash(ECAPA_WEIGHTS)
                model = f'speechbrain-ecapa-voxceleb-v1:{digest}'
            else:
                model = 'spectral-bands-v1'
            self._identities[kind] = f'{kind}:{model}'
        return self._identities[kind]

    def preparation_id(self, *, isolated=False, kind='singing'):
        if kind not in self.backends:
            raise ValueError(f'지원하지 않는 음성 종류: {kind}')
        if isolated or kind == 'speech':
            return 'provided-voice-mono-44100-v1'
        if 'separator' not in self._identities:
            self._identities['separator'] = f'torchaudio-hdemucs-high-v1:{self._model_hash(SEPARATION_WEIGHTS)}'
        return self._identities['separator']

    def prepare(self, audio_path, *, isolated=False, kind='singing', source_hash=None):
        import numpy as np

        path = Path(audio_path)
        preparation = self.preparation_id(isolated=isolated, kind=kind)
        source_hash = source_hash or _sha256(path)
        if isolated or kind == 'speech':
            return VoiceAudio(_decode(path, stereo=False)[0], kind, preparation, source_hash)
        key = hashlib.sha256(f'{source_hash}:{preparation}'.encode('utf-8')).hexdigest()
        stem_path = self.cache_dir / 'stems' / f'{key}.npy'
        try:
            stem = np.load(stem_path, allow_pickle=False)
        except (OSError, ValueError, EOFError):
            stem = None
        if stem is None or stem.ndim != 1 or not len(stem) or not np.isfinite(stem).all():
            stem = self._run_on_device(self._separate, _decode(path, stereo=True))
            stem_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with NamedTemporaryFile(dir=stem_path.parent, suffix='.tmp', delete=False) as temporary:
                    temp_path = Path(temporary.name)
                    np.save(temporary, stem, allow_pickle=False)
                os.replace(temp_path, stem_path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        return VoiceAudio(stem, kind, preparation, source_hash)

    def _run_on_device(self, function, *args):
        import torch

        if self.actual_device is None:
            self.actual_device = self.device
            if self.device in ('auto', 'cuda'):
                self.actual_device = 'cuda' if torch.cuda.is_available() else 'cpu'
                if self.device == 'cuda' and self.actual_device == 'cpu':
                    self.fallback_reason = 'CUDA unavailable'
        try:
            return function(*args)
        except RuntimeError as error:
            if self.actual_device != 'cuda' or not any(
                    name in str(error).lower() for name in ('cuda', 'cudnn', 'cublas', 'out of memory')):
                raise
            self.fallback_reason = str(error)
        self._encoders.clear()
        torch.cuda.empty_cache()
        self.actual_device = 'cpu'
        return function(*args)

    def _separate(self, stereo):
        import numpy as np
        import torch
        from torchaudio.models import hdemucs_high

        if self._separator is None:
            separator = hdemucs_high(sources=['drums', 'bass', 'other', 'vocals'])
            separator.load_state_dict(torch.load(self._model_path(SEPARATION_WEIGHTS), map_location='cpu',
                                                 weights_only=True))
            self._separator = separator.eval()
        overlap = SAMPLE_RATE
        chunk_size = 8 * SAMPLE_RATE
        fade = np.linspace(0, 1, overlap, dtype=np.float32)
        vocals = np.zeros(stereo.shape[1], dtype=np.float32)
        scale = max(float(stereo.std()), 1e-6)
        offset = float(stereo.mean())
        try:
            model = self._separator.to(self.actual_device)
            with torch.inference_mode():
                for start in range(0, len(vocals), chunk_size - overlap):
                    end = min(start + chunk_size, len(vocals))
                    chunk = torch.from_numpy((stereo[:, start:end] - offset) / scale).to(self.actual_device)
                    # Very short final inputs still need enough samples for the model's STFT.
                    padded = torch.nn.functional.pad(chunk, (0, max(0, 8192 - chunk.shape[-1])))
                    separated = model(padded[None])[0, model.sources.index('vocals')]
                    part = separated[:, :end - start].mean(dim=0).cpu().numpy() * scale
                    if start:
                        part[:overlap] *= fade[:min(len(part), overlap)]
                    if end < len(vocals):
                        part[-overlap:] *= 1 - fade
                    vocals[start:end] += part
                    if end == len(vocals):
                        break
        finally:
            self._separator.to('cpu')
        if not np.isfinite(vocals).all():
            raise ValueError('보컬 분리 결과에 유효하지 않은 샘플이 있습니다.')
        return vocals

    def _encoder(self, backend):
        import torch

        if backend in SONY_VARIANTS:
            from sony_singer_identity import load_sony_encoder

            folder = SONY_VARIANTS[backend]
            return load_sony_encoder(self._model_path(f'{folder}/model.pt'),
                                     self._model_path(f'{folder}/hyperparams.yaml'), self.actual_device)

        # Initialize optional torchvision before SpeechBrain's lazy modules can
        # interfere with torch's operator registration during a later import.
        try:
            __import__('torchvision')
        except (ImportError, OSError, RuntimeError):
            pass
        from speechbrain.lobes.features import Fbank
        from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
        from speechbrain.processing.features import InputNormalization
        from torchaudio.transforms import Resample

        features = Fbank(n_mels=80).eval().to(self.actual_device)
        normalize = InputNormalization(norm_type='sentence', std_norm=False).eval().to(self.actual_device)
        resample = Resample(SAMPLE_RATE, 16000).to(self.actual_device)
        model = ECAPA_TDNN(input_size=80, channels=[1024, 1024, 1024, 1024, 3072],
                          kernel_sizes=[5, 3, 3, 3, 1], dilations=[1, 2, 3, 4, 1],
                          attention_channels=128, lin_neurons=192)
        model.load_state_dict(torch.load(self._model_path(ECAPA_WEIGHTS), map_location='cpu', weights_only=True))
        model.eval().to(self.actual_device)

        def encode(waveform):
            lengths = torch.ones(waveform.shape[0], device=waveform.device)
            return model(normalize(features(resample(waveform)), lengths), lengths)

        return encode

    def _embedding(self, samples, kind):
        backend = self.backends[kind]
        if backend == 'spectral':
            return _unit(_spectral_embedding(samples))
        return self._run_on_device(self._model_embedding, samples, backend)

    def _model_embedding(self, samples, backend):
        import torch

        if backend not in self._encoders:
            self._encoders.clear()
            self._encoders[backend] = self._encoder(backend)
        with torch.inference_mode():
            waveform = torch.from_numpy(samples[None]).to(self.actual_device)
            embedding = self._encoders[backend](waveform).detach().cpu().numpy().reshape(-1)
        return _unit(embedding)

    @staticmethod
    def _signal(audio, start_ms, end_ms):
        start = max(0, start_ms * SAMPLE_RATE // 1000)
        end = end_ms * SAMPLE_RATE // 1000
        samples = audio.samples[start:end]
        if len(samples) < SAMPLE_RATE * MIN_WINDOW_MS // 1000:
            return None, None
        quality = _quality(samples)
        if (quality['active_fraction'] < .35 or quality['tonal_fraction'] < .25
                or quality['clipped_fraction'] > .03):
            return quality, None
        return quality, samples

    def _window(self, audio, start_ms, end_ms):
        quality, samples = self._signal(audio, start_ms, end_ms)
        if samples is None:
            return quality, None
        if not audio.source_hash or self.backends[audio.kind] == 'spectral':
            return quality, self._embedding(samples, audio.kind)
        start = max(0, start_ms * SAMPLE_RATE // 1000)
        end = end_ms * SAMPLE_RATE // 1000
        return quality, self._cached_embedding(audio, start, end, samples)

    def _cached_embedding(self, audio, start, end, samples):
        import numpy as np

        key = f'{audio.source_hash}:{audio.preparation_id}:{self.model_id(audio.kind)}:{start}:{end}'
        path = self.cache_dir / 'embeddings' / (hashlib.sha256(key.encode()).hexdigest() + '.npy')
        try:
            return _unit(np.load(path, allow_pickle=False))
        except (OSError, ValueError, EOFError):
            pass
        vector = self._embedding(samples, audio.kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as temporary:
            temp_path = Path(temporary.name)
        try:
            with temp_path.open('wb') as destination:
                np.save(destination, vector, allow_pickle=False)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return vector

    def enroll(self, audio, *, ranges=None):
        """Extract exemplars only from caller-supplied verified solo or dialogue ranges."""
        model = self.model_id(audio.kind)
        exemplars = []
        window_ms = REFERENCE_WINDOW_MS if self.backends[audio.kind] in SONY_VARIANTS else 3000
        for start_ms, end_ms in ranges if ranges is not None else [(0, audio.duration_ms)]:
            for start, end in _windows(max(0, int(start_ms)), min(int(end_ms), audio.duration_ms), window_ms):
                quality, vector = self._window(audio, start, end)
                if vector is not None:
                    exemplars.append({'start_ms': start, 'end_ms': end, 'quality': quality,
                                      'model_id': model, 'kind': audio.kind, 'embedding': vector.tolist()})
        return exemplars

    def _summarize(self, scores):
        ranked = sorted((candidate for candidate in scores if scores[candidate] is not None),
                        key=scores.get, reverse=True)
        decisions = {}
        for candidate, score in scores.items():
            threshold = self.thresholds.get(candidate, self.default_threshold)
            decision = 'uncertain'
            bounds = self.calibration.get(candidate, self.calibration.get('*'))
            if bounds is None and threshold is not None:
                bounds = (threshold - .025, threshold + .025)
            if score is not None and bounds is not None:
                gap = scores[ranked[0]] - score
                band = (bounds[1] - bounds[0]) / 2
                if score >= bounds[1] and (self.max_score_gap is None or gap <= self.max_score_gap - band):
                    decision = 'estimated'
                elif score < bounds[0] or (self.max_score_gap is not None and gap > self.max_score_gap + band):
                    decision = 'below_threshold'
            decisions[candidate] = decision
        singers = [candidate for candidate, decision in decisions.items() if decision == 'estimated']
        has_scores = bool(ranked)
        complete = singers and 'uncertain' not in decisions.values()
        margin = scores[ranked[0]] - scores[ranked[1]] if len(ranked) > 1 else None
        return {'scores': scores, 'decisions': decisions, 'singers': singers,
                'ranked': ranked, 'top_margin': margin,
                'estimated_count': len(singers) if complete else None,
                'status': 'estimated' if complete else ('uncertain' if has_scores else 'unavailable')}

    def profile_comparator(self, kind, profiles):
        """Build a reusable prototype scorer for one model and profile snapshot."""
        import numpy as np

        model = self.model_id(kind)
        matrices = {}
        for candidate, exemplars in profiles.items():
            groups = {}
            for sample in exemplars:
                if sample.get('kind') == kind and sample.get('model_id') == model:
                    groups.setdefault(sample.get('audio_sha256'), []).append(_unit(sample['embedding']))
            matrices[candidate] = [np.stack(vectors) for vectors in groups.values()]

        def compare(embedding):
            scores = dict.fromkeys(matrices)
            if embedding is None:
                return scores
            for candidate, recordings in matrices.items():
                valid = [matrix for matrix in recordings if matrix.shape[1] == len(embedding)]
                if not valid:
                    continue
                if self.aggregation == 'mean':
                    centroid = np.concatenate(valid).mean(axis=0)
                    norm = np.linalg.norm(centroid)
                    if norm > 0:
                        scores[candidate] = float(np.clip((centroid @ embedding) / norm, -1, 1))
                    continue
                similarities = [matrix @ embedding for matrix in valid]
                if self.aggregation == 'max':
                    value = max(vector.max() for vector in similarities)
                else:
                    # Balance recordings before aggregating representative windows.
                    best = (sorted((vector.max() for vector in similarities), reverse=True)[:3]
                            if len(similarities) > 1 else np.sort(similarities[0])[-3:])
                    value = np.mean(best)
                scores[candidate] = float(np.clip(value, -1, 1))
            return scores

        return compare

    def score(self, audio, *, ranges, profiles):
        """Compare vocal windows to each supplied candidate independently."""
        model = self.model_id(audio.kind)
        compare = self.profile_comparator(audio.kind, profiles)
        has_profiles = any(sample.get('kind') == audio.kind and sample.get('model_id') == model
                           for samples in profiles.values() for sample in samples)
        separation_error = None
        targets = None

        def subsegment(start, end):
            nonlocal separation_error, targets
            if has_profiles:
                quality, embedding = self._window(audio, start, end)
                active = embedding is not None
            else:
                quality, signal = self._signal(audio, start, end)
                active = signal is not None
                embedding = None
            scores = compare(embedding)
            result = {'start_ms': start, 'end_ms': end, 'quality': quality, **self._summarize(scores)}
            if (self.separator_backend and not separation_error and audio.kind == 'singing' and active
                    and result['estimated_count'] != 1):
                try:
                    if targets is None and self.separator_backend == 'singer-informed':
                        targets = self._enroll_targets(profiles)
                    result['separation'] = self._separation_evidence(audio, start, end, compare, targets, has_profiles)
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    separation_error = f'{type(error).__name__}: {error}'
            if separation_error:
                result['separation_error'] = separation_error
            return result

        bounds = [(min(max(0, int(start)), audio.duration_ms), min(max(0, int(end)), audio.duration_ms))
                  for start, end in ranges]
        results = []
        for index, (start, end) in enumerate(bounds):
            end = max(start, end)
            segments = [subsegment(left, right) for left, right in _windows(start, end, 2000)]
            scores = {candidate: max((segment['scores'][candidate] for segment in segments
                                      if segment['scores'][candidate] is not None), default=None)
                      for candidate in profiles}
            summary = self._summarize(scores)
            if len(segments) > 1:
                # A line can contain sequential soloists. A weak score in one
                # window cannot turn another window's estimate into a duet.
                summary['singers'] = list(dict.fromkeys(singer for segment in segments
                                                        for singer in segment['singers']))
                for candidate in profiles:
                    decisions = {segment['decisions'][candidate] for segment in segments}
                    summary['decisions'][candidate] = ('estimated' if 'estimated' in decisions else
                                                        'below_threshold' if decisions == {'below_threshold'} else
                                                        'uncertain')
                same_singers = all(segment['singers'] == segments[0]['singers'] for segment in segments)
                summary['estimated_count'] = (segments[0]['estimated_count'] if same_singers and
                                              all(segment['status'] == 'estimated' for segment in segments) else None)
                if all(segment['status'] == 'estimated' for segment in segments):
                    summary['status'] = 'estimated'
                elif summary['ranked']:
                    summary['status'] = 'uncertain'
            row = {'start_ms': start, 'end_ms': end, 'model_id': model,
                   'score_type': f'{self.aggregation}_cosine_similarity', 'subsegments': segments, **summary}
            if has_profiles and 0 < end - start < MIN_WINDOW_MS:
                left, right = start, end
                if index and 0 <= start - bounds[index - 1][1] <= 100:
                    left = max(bounds[index - 1][0], start - 450)
                if index + 1 < len(bounds) and 0 <= bounds[index + 1][0] - end <= 100:
                    right = min(bounds[index + 1][1], end + 450)
                if right - left >= MIN_WINDOW_MS:
                    _, embedding = self._window(audio, left, right)
                    row['adjacent_evidence'] = {'start_ms': left, 'end_ms': right, 'scores': compare(embedding)}
            results.append(row)
        return results

    def _enroll_targets(self, profiles):
        import numpy as np
        from singing_voice_separation import SingerInformedSeparator

        if self._singer_separator is None:
            self._singer_separator = SingerInformedSeparator(self.models_dir, device=self._separator_device())
        targets = {}
        for candidate, samples in profiles.items():
            refs = [sample for sample in samples if sample.get('audio_sha256') and
                    sample.get('preparation', '').startswith('torchaudio-hdemucs-') and
                    sample['end_ms'] - sample['start_ms'] >= 3000]
            refs.sort(key=lambda sample: sample.get('quality', {}).get('tonal_fraction', 0), reverse=True)
            for sample in refs:
                digest = sample['audio_sha256']
                key = hashlib.sha256(f"{digest}:{sample['preparation']}".encode()).hexdigest()
                path = self.cache_dir / 'stems' / f'{key}.npy'
                try:
                    stem = np.load(path, allow_pickle=False, mmap_mode='r')
                except (OSError, ValueError, EOFError):
                    continue
                start = sample['start_ms'] * SAMPLE_RATE // 1000
                end = min(start + 4 * SAMPLE_RATE, sample['end_ms'] * SAMPLE_RATE // 1000)
                excerpt = np.array(stem[start:end], dtype=np.float32)
                if excerpt.ndim == 1 and len(excerpt) >= 3 * SAMPLE_RATE and np.isfinite(excerpt).all():
                    targets[candidate] = (self._singer_separator.enroll(excerpt, sample_rate=SAMPLE_RATE), digest)
                    break
        return targets

    def _separation_evidence(self, audio, start, end, compare, targets, has_profiles):
        import numpy as np

        mixture = audio.samples[start * SAMPLE_RATE // 1000:end * SAMPLE_RATE // 1000]
        energy = max(float(mixture @ mixture), 1e-12)
        if targets is not None:
            return self._target_evidence(mixture, energy, targets, compare)

        from singing_voice_separation import UnmixxSeparator

        if self._singer_separator is None:
            self._singer_separator = UnmixxSeparator(self.models_dir, device=self._separator_device())
        separator = self._singer_separator
        stems = separator.separate(mixture, sample_rate=SAMPLE_RATE)
        remainder = mixture - stems.sum(axis=0)
        residual = float(remainder @ remainder) / energy
        similarity = float(stems[0] @ stems[1]) / max(
            float(np.linalg.norm(stems[0]) * np.linalg.norm(stems[1])), 1e-12)
        rows = []
        for stem in stems:
            reference = VoiceAudio(stem, audio.kind, separator.version)
            if has_profiles:
                quality, embedding = self._window(reference, 0, reference.duration_ms)
            else:
                quality, _ = self._signal(reference, 0, reference.duration_ms)
                embedding = None
            residual_without_stem = remainder + stem
            gain = float(residual_without_stem @ residual_without_stem) / energy - residual
            scores = self._summarize(compare(embedding))
            rows.append({'quality': quality, 'scores': scores['scores'], 'ranked': scores['ranked'],
                         'top_margin': scores['top_margin'],
                         'energy_fraction': float(stem @ stem) / energy, 'residual_gain': gain})
        return {'model': separator.version, 'device': separator.device,
                'residual_fraction': residual, 'stem_cosine': similarity, 'stems': rows}

    def _target_evidence(self, mixture, energy, targets, compare):
        separator = self._singer_separator
        results = {}
        for candidate, (enrollment, digest) in targets.items():
            target, residual = separator.extract(mixture, enrollment, sample_rate=SAMPLE_RATE)
            target_audio = VoiceAudio(target, 'singing', separator.version)
            residual_audio = VoiceAudio(residual, 'singing', separator.version)
            quality, embedding = self._window(target_audio, 0, target_audio.duration_ms)
            _, residual_embedding = self._window(residual_audio, 0, residual_audio.duration_ms)
            target_scores = compare(embedding)
            target_score = target_scores[candidate]
            competitor = max((score for identity, score in target_scores.items() if identity != candidate
                              and score is not None), default=None)
            residual_score = compare(residual_embedding)[candidate]
            results[candidate] = {'quality': quality, 'reference_sha256': digest,
                                  'target_similarity': target_score, 'residual_similarity': residual_score,
                                  'target_margin': (target_score - competitor if target_score is not None
                                                    and competitor is not None else None),
                                  'energy_fraction': float(target @ target) / energy,
                                  'residual_gain': 1 - float(residual @ residual) / energy}
        return {'model': separator.version, 'device': separator.device, 'targets': results}

    def _separator_device(self):
        return 'cpu' if self.actual_device == 'cpu' or self.device == 'cpu' else 'auto'
