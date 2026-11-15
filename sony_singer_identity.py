"""Local inference for the published Sony CSL singer identity encoders.

Adapted from https://github.com/SonyCSLParis/ssl-singer-identity

MIT License

Copyright (c) 2023 Bernardo Torres

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from collections import OrderedDict

SONY_VARIANTS = {'sony': 'sony-byol', 'sony-contrastive': 'sony-contrastive'}
REFERENCE_WINDOW_MS = 4000


def load_sony_encoder(weights_path, config_path, device):
    """Load an offline encoder that takes mono 44.1 kHz [batch, samples]."""
    import torch
    import yaml
    from nnAudio.features import MelSpectrogram
    from torchvision.models import efficientnet_b0
    from torchvision.transforms import Normalize

    settings = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    features, encoder = settings['feature_extractor'], settings['encoder']
    if features['spec_layer'] != 'melspectogram' or encoder['backbone'] != 'efficientnet_b0':
        raise ValueError('지원하지 않는 Sony Singer Identity 모델 구성입니다.')

    class LogScale(torch.nn.Module):
        def forward(self, mel):
            return torch.log(mel + 1e-8)

    class Grey2Rgb(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.normalize = Normalize(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            )

        def forward(self, mel):
            mel = mel / mel.max()
            return self.normalize(mel.unsqueeze(1).expand(-1, 3, -1, -1))

    # Preserve the published checkpoint's feature_extractor.spec_layer and encoder.net keys.
    mel = MelSpectrogram(
        n_fft=features['n_fft'],
        hop_length=features['hop_length'],
        n_mels=features.get('n_mels') or 128,
        verbose=False,
    )
    network = torch.nn.Sequential(
        LogScale(), Grey2Rgb(),
        efficientnet_b0(weights=None, num_classes=encoder['embedding_dim']),
    )
    model = torch.nn.Sequential(OrderedDict(
        feature_extractor=torch.nn.Sequential(OrderedDict(spec_layer=mel)),
        encoder=torch.nn.Sequential(OrderedDict(net=network)),
    ))
    model.load_state_dict(torch.load(weights_path, map_location='cpu', weights_only=True))
    return model.eval().to(device)
