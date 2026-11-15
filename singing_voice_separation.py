"""Optional vocal separation for short, time-aligned regions."""
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from threading import Lock


def _resolve_device(torch, requested):
    if requested == 'auto':
        return 'cuda' if torch.cuda.is_available() else 'cpu'
    if requested == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable in the active PyTorch runtime')
    return requested


class UnmixxSeparator:
    """Load the upstream two-source model from local, separately installed assets."""

    max_seconds = 8

    def __init__(self, models_dir, device='auto'):
        if device not in {'auto', 'cpu', 'cuda'}:
            raise ValueError(f'Unsupported UNMIXX device: {device}')
        model_dir = Path(models_dir) / 'unmixx'
        config_path = model_dir / 'conf.yml'
        weights_path = model_dir / 'best.ckpt'
        for path in (config_path, weights_path):
            if not path.is_file():
                raise FileNotFoundError(f'UNMIXX model asset missing: {path}')

        import torch
        import yaml
        try:
            from look2hear.models import UNMIXX
        except ImportError as exc:
            raise RuntimeError(f'UNMIXX upstream source or inference dependency is unavailable: {exc}') from exc

        config_bytes = config_path.read_bytes()
        config = yaml.safe_load(config_bytes)
        self.sample_rate = int(config['datamodule']['data_config']['sample_rate'])
        architecture = config['audionet']['audionet_config']
        if (config['audionet']['audionet_name'] != 'UNMIXX' or self.sample_rate != 24000
                or architecture['num_sources'] != 2):
            raise ValueError('UNMIXX expects the official two-source, 24 kHz configuration')

        digest = sha256(config_bytes)
        with weights_path.open('rb') as weights:
            for block in iter(lambda: weights.read(1024 * 1024), b''):
                digest.update(block)
            weights.seek(0)
            checkpoint = torch.load(weights, map_location='cpu', weights_only=True)
        self.version = f'unmixx:{digest.hexdigest()}'
        state = checkpoint.get('state_dict', checkpoint)
        with redirect_stdout(StringIO()):
            model = UNMIXX(sample_rate=self.sample_rate, **architecture)
        model.load_state_dict({key.removeprefix('audio_model.'): value for key, value in state.items()}, strict=True)

        self._model = model.eval()
        self._lock = Lock()
        self._allow_cpu_fallback = device == 'auto'
        self.device = _resolve_device(torch, device)
        self._move_model()

    def _move_model(self):
        try:
            self._model.to(self.device)
        except RuntimeError:
            if self.device != 'cuda' or not self._allow_cpu_fallback:
                raise
            self.device = 'cpu'
            self._model.to(self.device)

    def _infer(self, audio):
        import torch

        with torch.inference_mode():
            stems, *_ = self._model(audio[None, None].to(self.device), istest=True)
            return stems.squeeze(0).cpu()

    def separate(self, samples, sample_rate=44100):
        """Return two float32 mono arrays at the input rate and sample count."""
        import numpy as np
        import torch
        from torch.nn.functional import pad
        from torchaudio.functional import resample

        audio = np.array(samples, dtype=np.float32, copy=True)
        if sample_rate <= 0 or audio.ndim != 1 or not 0 < len(audio) <= sample_rate * self.max_seconds:
            raise ValueError(f'UNMIXX expects a mono vocal region of up to {self.max_seconds} seconds')
        if not np.isfinite(audio).all():
            raise ValueError('UNMIXX input contains nonfinite samples')

        resampled = resample(torch.from_numpy(audio), sample_rate, self.sample_rate)
        length = resampled.shape[-1]
        # The upstream STFT uses reflection padding and needs at least one full window.
        if length < self._model.win:
            resampled = pad(resampled, (0, self._model.win - length))
        with self._lock:
            try:
                stems = self._infer(resampled)
            except RuntimeError:
                if self.device != 'cuda' or not self._allow_cpu_fallback:
                    raise
                self.device = 'cpu'
                self._move_model()
                stems = self._infer(resampled)

        if stems.shape != (2, resampled.shape[-1]) or not torch.isfinite(stems).all():
            raise RuntimeError('UNMIXX did not return two time-aligned finite stems')
        aligned = resample(stems[:, :length], self.sample_rate, sample_rate)[:, :len(audio)]
        return aligned.contiguous().numpy()


class SingerInformedSeparator:
    """The published FiLM model conditioned on its own NetSV enrollment embedding."""

    sample_rate = 44100
    enrollment_rate = 16000
    enrollment_seconds = 3

    def __init__(self, models_dir, device='auto'):
        if device not in {'auto', 'cpu', 'cuda'}:
            raise ValueError(f'Unsupported singer-informed separation device: {device}')
        model_dir = Path(models_dir) / 'singer-informed'
        names = ('separator.json', 'vocals.json', 'embedding.pth', 'vocals.pth')
        for name in names:
            if not (model_dir / name).is_file():
                raise FileNotFoundError(f'Singer-informed model asset missing: {model_dir / name}')

        import torch
        try:
            from openunmix.model import OpenUnmix_FiLM
            from openunmix.transforms import make_filterbanks
            from singer_embedding.Customized_NetSV import NetSV
        except ImportError as exc:
            raise RuntimeError(f'Singer-informed upstream source or inference dependency is unavailable: {exc}') from exc

        config = json.loads((model_dir / 'separator.json').read_bytes())
        settings = json.loads((model_dir / 'vocals.json').read_bytes())['args']
        if config['sample_rate'] != self.sample_rate or config['nb_channels'] != 1:
            raise ValueError('Singer-informed FiLM expects the official mono 44.1 kHz configuration')
        fft_size = self._fft_size = int(config['nfft'])
        self._encoder, self._decoder = make_filterbanks(n_fft=fft_size, n_hop=config['nhop'], center=True)
        max_bin = min(fft_size // 2 + 1, int(settings['bandwidth'] * fft_size / self.sample_rate) + 1)
        self._embedder = NetSV(hidden_size=32, num_layers=2).eval()
        self._separator = OpenUnmix_FiLM(nb_bins=fft_size // 2 + 1, nb_channels=1,
                                        hidden_size=settings['hidden_size'], max_bin=max_bin, emb_dim=32,
                                        unidirectional=settings.get('unidirectional', False)).eval()
        self._embedder.load_state_dict(torch.load(model_dir / 'embedding.pth', map_location='cpu', weights_only=True))
        self._separator.load_state_dict(torch.load(model_dir / 'vocals.pth', map_location='cpu', weights_only=True))

        digest = sha256()
        for name in names:
            with (model_dir / name).open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
        self.version = f'singer-informed-film:{digest.hexdigest()}'
        self._lock = Lock()
        self._allow_cpu_fallback = device == 'auto'
        self.device = _resolve_device(torch, device)
        self._on_device(self._move_models)

    def _move_models(self):
        for model in (self._embedder, self._separator, self._encoder, self._decoder):
            model.to(self.device)

    def _on_device(self, operation, *args):
        import torch

        with self._lock, torch.inference_mode():
            try:
                return operation(*args)
            except RuntimeError:
                if self.device != 'cuda' or not self._allow_cpu_fallback:
                    raise
                self.device = 'cpu'
                self._move_models()
                return operation(*args)

    def _embed(self, reference):
        return self._embedder.embedding(reference[None].to(self.device)).squeeze(0).cpu()

    def enroll(self, samples, sample_rate=44100):
        """Encode a verified solo region of three seconds without assigning a voice label."""
        import numpy as np
        import torch
        from torchaudio.functional import resample

        audio = np.array(samples, dtype=np.float32, copy=True)
        length = sample_rate * self.enrollment_seconds
        if sample_rate <= 0 or audio.ndim != 1 or len(audio) < length or not np.isfinite(audio[:length]).all():
            raise ValueError('Singer-informed enrollment requires three seconds of finite mono audio')
        reference = torch.from_numpy(audio[:length])
        peak = reference.abs().max()
        if peak <= 1e-6:
            raise ValueError('Singer-informed enrollment requires audible solo vocals')
        reference = resample(reference / peak, sample_rate, self.enrollment_rate)
        return self._on_device(self._embed, reference).numpy()

    def _extract(self, mixture, enrollment):
        import torch

        spectrum = torch.view_as_complex(self._encoder(mixture[None, None].to(self.device)))
        magnitude = self._separator(spectrum.abs(), enrollment[None].to(self.device))
        estimate = magnitude * torch.exp(1j * spectrum.angle())
        return self._decoder(torch.view_as_real(estimate), length=mixture.numel()).reshape(-1).cpu()

    def extract(self, samples, enrollment, sample_rate=44100):
        """Return target and mixture-minus-target on the input timeline as float32 arrays."""
        import numpy as np
        import torch
        from torch.nn.functional import pad
        from torchaudio.functional import resample

        audio = np.array(samples, dtype=np.float32, copy=True)
        reference = np.array(enrollment, dtype=np.float32, copy=True)
        if (sample_rate <= 0 or audio.ndim != 1 or not 0 < len(audio) <= sample_rate * 8
                or not np.isfinite(audio).all() or reference.shape != (32,) or not np.isfinite(reference).all()):
            raise ValueError('Singer-informed extraction expects up to eight seconds of mono audio and a NetSV embedding')
        mixture = resample(torch.from_numpy(audio), sample_rate, self.sample_rate)
        length = mixture.numel()
        if length < self._fft_size:
            mixture = pad(mixture, (0, self._fft_size - length))
        target = self._on_device(self._extract, mixture, torch.from_numpy(reference))
        if target.numel() != mixture.numel() or not torch.isfinite(target).all():
            raise RuntimeError('Singer-informed separation did not return a time-aligned finite estimate')
        target = resample(target[:length], self.sample_rate, sample_rate)[:len(audio)].numpy()
        return target, audio - target
