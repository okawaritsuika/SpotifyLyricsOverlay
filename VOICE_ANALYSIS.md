# 로컬 음성 프로필과 가창자 분석

`character_voice.py`는 사용자가 등록한 로컬 파일을 44.1 kHz로 읽는다. 혼합 음악은 TorchAudio Hybrid Demucs로 보컬 스템을 분리한다. 분리된 보컬과 대사는 바로 임베딩을 추출한다. 스템 캐시 키는 오디오 SHA-256과 보컬 분리 모델 파일 SHA-256에서 만든다. 디코더는 외부 파일이나 네트워크를 참조하는 재생 목록을 거부한다. [Hybrid Demucs 문서](https://docs.pytorch.org/audio/2.8/tutorials/hybrid_demucs_tutorial.html)

Sony는 공식 평가 길이인 4초, ECAPA는 3초 단위로 등록한다. 샘플마다 시간 범위, 유효 발성 비율, 음량, 클리핑 비율, 원본과 모델 버전을 저장한다. 샘플별 임베딩을 보관해 같은 캐릭터의 여러 곡에서 음색을 비교할 수 있다. Sony의 기본 점수 집계는 서로 다른 녹음에서 얻은 유사도를 우선하는 Top-K다. ECAPA의 기존 최대 코사인 방식과 평균 프로필 비교도 지원한다. 대사와 가창은 동일한 모델을 선택해도 서로 다른 프로필 키를 사용한다. 긴 가사 줄을 약 2초씩 나누고, 줄과 하위 구간의 추정 결과를 따로 저장한다. 너무 짧은 줄의 인접 구간 점수는 검토용으로 표시한다.

유사도는 확률과 다른 수치다. 판정 기준이 설정된 경우에만 각 캐릭터를 독립적으로 판정한다. 기준값 ±0.025 이내, 프로필 누락, 부족한 음성 증거는 미확정으로 남긴다. 이번 벤치마크에서 맞춘 기준값은 테스트에 사용한 캐릭터 전용이다. 앱이 그 값을 일반 그룹곡에 적용하지 않는다. 추정한 구간은 검증된 솔로·대사 등록을 대체하지 않는다.

## 모델 파일과 의존성

앱의 선택적 오디오 런타임에는 NumPy, PyAV와 같은 버전 계열의 Torch 및 TorchAudio가 필요하다. 아래에서 `models_dir`은 음성 모델 디렉터리다. 작업 중 모델과 오디오를 온라인으로 보내는 호출은 없다.

| 분석 옵션 | `models_dir` 내부 경로 | 추가 패키지 |
|---|---|---|
| `ecapa` | `ecapa/embedding_model.ckpt` | SpeechBrain 1.1.0 |
| `sony` | `sony-byol/model.pt`, `sony-byol/hyperparams.yaml` | nnAudio 0.3.3, SciPy 1.15.3, torchvision 0.23.0, PyYAML |
| `sony-contrastive` | `sony-contrastive/model.pt`, `sony-contrastive/hyperparams.yaml` | BYOL과 동일 |
| `spectral` | 없음 | NumPy |
| 혼합 음악 보컬 분리 | `hdemucs_high_trained.pt` | TorchAudio 2.8 |

앱의 선택적 오디오 설치는 CPU용 Torch 2.8과 호환되는 TorchAudio 및 torchvision, 가창자 분석 패키지를 설치한다. NVIDIA CUDA용 Torch를 설치하려면 소스 실행 환경에서 `setup-audio.ps1 -VoiceCuda`를 실행한다. EXE는 PowerShell에서 `Start-Process -Wait -FilePath .\SpotifyLyricsOverlay.exe -ArgumentList '--worker','optional_runtime','audio-cuda'`로 CUDA용 런타임을 설치할 수 있다. CUDA 휠은 약 2.9GB를 다운로드한다. 설치된 EXE의 모델 기본 위치는 `%LOCALAPPDATA%\SpotifyLyricsOverlay\runtime\models\character-voice`이다. 소스 실행 환경은 프로젝트의 `models\character-voice`를 사용한다. 가창 모델 파일은 해당 폴더에 별도로 배치한다. [공식 PyTorch 2.8 설치 안내](https://pytorch.org/get-started/previous-versions/#v280)

Sony 어댑터는 공식 멜 스펙트로그램과 EfficientNet-B0 가중치를 사용해 44.1 kHz 모노에서 1,000차원 특징을 계산한다. 앱은 비교 전에 임베딩을 L2 정규화한다. 모델과 구성 파일의 SHA-256에서 만든 키를 프로필 버전으로 사용한다. 분리 모델은 `HDEMUCS_HIGH_MUSDB_PLUS`의 공개 `hdemucs_high` 구조와 로컬 체크포인트를 사용한다. Sony 코드는 MIT 라이선스이며 어댑터에 저작권 고지를 포함한다. [Sony 모델 소스](https://github.com/SonyCSLParis/ssl-singer-identity), [Sony BYOL 모델 파일](https://huggingface.co/BernardoTorres/singer-identity/tree/main/byol), [Sony Contrastive 모델 파일](https://huggingface.co/BernardoTorres/singer-identity/tree/main/contrastive), [SpeechBrain ECAPA 모델 파일](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/tree/main), [TorchAudio 모델 가중치](https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt)

## 실제 녹음 검증

독립된 곡을 각각 등록, 기준값 보정, 평가에 사용했다. 두 후보 실험에는 JSUT-song과 OJaMa-Song의 일본어 가창을 사용했다. 네 후보 실험에는 NUS-48E의 영어 가창자 두 명을 추가했다. 평가의 솔로는 실제 녹음이다. 복수 가창은 각 녹음에서 유효한 2초 발성 구간을 골라 RMS를 맞춘 뒤 합성한 것이다. 실제 합창이나 상용 음원으로 검증한 결과는 없다. [JSUT-song](https://sites.google.com/site/shinnosuketakamichi/publication/jsut-song), [OJaMa-Song](https://sython.org/Corpus/OJaMa-Song/), [NUS-48E 연구용 사본 및 라이선스](https://huggingface.co/datasets/guey-khala-mari/NUS-48E)

| 후보 | 모델 | 솔로 1순위 | 솔로 판정 전체 일치 | 2인 전체 일치 | 3인 전체 일치 | 4인 전체 일치 |
|---|---|---:|---:|---:|---:|---:|
| 2명 | ECAPA | 23/23 | 19/23 | 0/11 | — | — |
| 2명 | Sony BYOL | 12/23 | 10/23 | 0/11 | — | — |
| 2명 | 스펙트럼 | 20/23 | 16/23 | 1/11 | — | — |
| 4명 | ECAPA | 31/31 | 24/31 | 0/31 | 0/16 | 0/4 |
| 4명 | Sony BYOL | 15/31 | 9/31 | 0/31 | 0/16 | 0/4 |
| 4명 | 스펙트럼 | 21/31 | 3/31 | 1/31 | 1/16 | 1/4 |

전체 일치는 가창자 목록이 정확하고 모든 후보의 판정이 완료된 경우로 계산했다. 미확정 건도 분모에 포함했다. 이 자료의 솔로 순위와 전체 일치에서 가장 높았던 ECAPA를 기본 가창 프로필 모델로 선택했다. 세 모델 모두 복수 가창의 일반 적용을 뒷받침할 정확도에 미치지 못했다. 보컬 분리기는 반주와 전체 보컬을 분리하므로 같은 음정으로 부르는 여러 목소리의 분리와 개별 기여 판정에는 별도 검증이 필요하다.

네 후보 기준 모델 로드와 28개 샘플 등록은 ECAPA 3.36초, Sony BYOL 6.11초, 스펙트럼 0.21초였다. 82개 평가 구간의 점수 계산은 각각 5.70초, 3.58초, 0.33초였다. 대사 파일에서 ECAPA 샘플 3개를 생성하는 데 0.38초가 걸렸고, 가창 프로필을 대사 판정에 제공한 경우 유사도는 계산되지 않았다. CPU Torch 2.8과 4개 OpenMP 스레드 환경에서 측정했다. 장착된 RTX 3060 Laptop GPU는 이 CPU 전용 Torch 런타임에서 사용되지 않았다.

공식 튜토리얼의 Creative Commons 음악 예제에서 10초 구간을 분리하는 데 모델 로드 포함 6.83초가 걸렸다. 캐시 재사용은 0.013초였으며 두 보컬 스템의 샘플이 일치했다. 이 실행에서 스템 정확도를 수치로 평가하지 않았다. [A Classic Education – NightOwl 예제 출처](https://docs.pytorch.org/audio/2.8/tutorials/hybrid_demucs_tutorial.html#run-model)

## 재현

작업 디렉터리의 `output/voice-models/benchmark.json`과 `output/voice-models/benchmark-four.json`에 모델 ID, 녹음 파일명, 구간 수, 기준값과 시간을 기록했다. 테스트 오디오는 출처별 이용 조건을 따른다. 가창 모델과 검증 오디오는 별도 선택적 자산이다.

```powershell
$env:OMP_NUM_THREADS = '4'
$env:OPENBLAS_NUM_THREADS = '2'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_character_voice.py -v
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_character_voice.py `
  --jsut-zip output/japanese-alignment/local-corpus/jsut-song_ver1.zip `
  --ojama-zip output/japanese-alignment/local-corpus/ojama_take1.zip `
  --nus-dir output/voice-models/nus48e `
  --music-wav output/voice-models/hdemucs_mix.wav `
  --models-dir output/voice-models --output output/voice-models/benchmark-four.json
```

두 후보 검증에는 `--nus-dir`과 `--music-wav`를 생략한다. NUS 디렉터리에는 `ADIZ`의 01, 09, 13, 18번과 `JLEE`의 05, 08, 11, 15번 가창 파일을 `sing-NN.wav`로 배치한다. 대사 검증은 `ADIZ/read-01.wav`를 사용한다. 주파수 대역이 서로 다른 순수 합성 신호로는 1~4인 판정, 가사 줄 내부의 순차 가창, 무음과 잡음 기각, 모델 및 대사 프로필 격리, 분리 스템 캐시 재사용을 별도로 시험했다.

## 선택적 복수 보컬 분리

`singing_voice_separation.py`는 로컬 모델을 읽고 입력 샘플 수에 맞춘 보컬 추정치를 반환한다. UNMIXX의 공개 가중치는 24 kHz의 단일 채널 입력으로부터 항상 두 스템을 만든다. 두 출력의 합은 모델에서 혼합 신호에 맞추므로 재합성 잔여량은 가창자 수를 판별하는 근거가 될 수 없다. FiLM은 검증된 3초 솔로 구간을 NetSV 입력인 16 kHz로 변환하고, 44.1 kHz 혼합에서 후보별 추정 보컬과 잔여 신호를 반환한다. 실제 분석은 가사 시간축에서 최대 8초씩 처리한다. [UNMIXX 추론과 구성](https://github.com/jihoojung0106/unmixx/tree/8e750521b5942f4717656cac86a23cf0bd90dea5) [Singer-Informed FiLM 소스와 공개 가중치](https://github.com/jocelynxu01/singer-separation-paper/tree/3f6ce09d53750b55f52801a24efff369dbd04846)

| 선택 모델 | `models/character-voice` 아래 로컬 파일 | 가중치 SHA-256 |
|---|---|---|
| UNMIXX | `unmixx/best.ckpt`, `unmixx/conf.yml` | `edd8bd0782f85ebb8a6b26bec47808b3130d15c2ccd49c741d2ff5983cb6c359` |
| Singer-Informed FiLM | `singer-informed/vocals.pth`, `singer-informed/vocals.json`, `singer-informed/separator.json` | `7f90116504450d01ed39d7515345b37e5e49bfed7df68e88887fe7c9aee8a012` |
| FiLM용 Clean NetSV | `singer-informed/embedding.pth` | `5f52e199edc0a753378bfbf335408dd3044828c9989684f602765e7169fbc1fc` |

UNMIXX에는 저장소 루트에서 확인 가능한 라이선스가 없다. Singer-Informed의 `open-unmix-pytorch` 하위 소스는 MIT이며 NetSV 및 체크포인트의 재배포 조건은 확인되지 않았다. Windows 배포본에는 이 프로젝트의 로더만 포함한다. 각 모델과 외부 소스는 로컬 선택 자산이다. [UNMIXX 저장소](https://github.com/jihoojung0106/unmixx) [Singer-Informed Open-Unmix 라이선스](https://github.com/jocelynxu01/singer-separation-paper/blob/main/open-unmix-pytorch/LICENSE)

이 작업 환경에서는 아래 명령으로 별도 설치한 공식 소스 및 의존성을 사용한다. Torch와 TorchAudio 2.8 CPU와 CUDA 12.6에서 각각 확인했다. UNMIXX의 소스 패키지는 `look2hear`를 가져오는 과정에서 librosa, Asteroid, PyTorch Lightning, torch-complex, typeguard 2.x, Rich, pandas, Matplotlib, praat-parselmouth도 불러온다. FiLM은 SciPy, TorchAudio 및 공식 Open-Unmix와 NetSV 소스를 사용한다. 조건부 검증 벤치마크는 scikit-learn이 필요하다.

```powershell
$voiceDir = 'output\voice-models'
$unmixx = "$voiceDir\unmixx-8e750521b5942f4717656cac86a23cf0bd90dea5"
$film = "$voiceDir\singer-separation-paper-3f6ce09d53750b55f52801a24efff369dbd04846"
$env:PYTHONPATH = "$voiceDir\cuda-runtime;$unmixx;$film;$film\open-unmix-pytorch;$voiceDir\deps;$voiceDir\unmixx-deps;$voiceDir\ssl-singer-identity-master"
$env:OMP_NUM_THREADS = '3'
$env:OPENBLAS_NUM_THREADS = '1'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_singing_voice_separation.py -v
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_separation.py --device auto `
  --output output\voice-models\singing-separation-cuda.json
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_separation.py --device auto `
  --escape-audio 'output\mp3\0387 エスケープ.mp3' --output output\voice-models\escape-unmixx-blind.json
```

벤치마크는 `Brand New Theater!`의 네 솔로 버전에서 HDemucs 보컬을 분리하고 반주로 맞춘 시간 오프셋으로 1~4인 혼합을 만든다. 같은 멜로디에서 동시 발성 프레임의 75% 이상이 65센트 이내인 구간을 유니즌으로 집계한다. 보정용 곡과 프로필 등록용 곡은 평가곡과 다르다. 무음은 신호 없음으로 처리한다. 조건부 분리 검증은 입력·추출·잔여 신호의 가창자 점수, 경쟁 후보와의 점수 차이, 추출 에너지와 잔여 에너지 감소량을 보정에 사용한다. 추출된 에너지만으로는 후보를 확정하지 않는다.

RTX 3060 Laptop GPU에서 51개 구간으로 기준을 보정하고 107개 별도 구간으로 평가했다. 신호 없음 1개도 전체 지표에 포함했다. 결과 파일은 `output/voice-models/singing-separation-cuda.json`이다.

| 입력 및 판정 방식 | 솔로 1순위 | 솔로 전체 일치 | 듀엣 전체 일치 | 3인 전체 일치 | 4인 전체 일치 | 솔로 복수 오검출 | F1 | 비참여 후보 오검출률 | 확정률 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 전체 보컬 + Sony BYOL | 9/20 | 1/20 | 0/49 | 2/30 | 0/7 | 8/20 | 0.395 | 20.8% | 13.1% |
| UNMIXX 스템 + Sony BYOL | 6/20 | 0/20 | 1/49 | 0/30 | 0/7 | 15/20 | 0.513 | 52.1% | 24.3% |
| FiLM 추출 + Sony BYOL | 5/20 | 1/20 | 1/49 | 0/30 | 0/7 | 3/20 | 0.366 | 21.4% | 28.0% |
| FiLM 혼합·추출·잔여 보정 | 6/20 | 0/20 | 1/49 | 1/30 | 1/7 | 7/20 | 0.483 | 32.3% | 32.7% |

방법마다 가장 확신한 유효 발성 13개로 확정 건수를 맞췄을 때 듀엣 전체 일치는 모두 0건이었다. 확인된 유니즌 5개에서도 가창자 두 명을 모두 확정한 결과는 없었다. UNMIXX의 듀엣 분리 SI-SDR 개선 중앙값은 전체 49개에서 +4.91 dB, 유니즌 5개에서 +3.77 dB였다. FiLM의 듀엣 목표 신호 SI-SDR 개선 중앙값은 −3.48 dB였다. 부재 후보를 조건으로 준 188회 모두 혼합 대비 −40 dB를 넘는 출력이 생겼다. 듀엣 부재 후보의 출력 에너지 중앙값은 혼합 에너지의 46.0%였고 실제 참가 후보는 45.7%였다.

보정과 평가 158개 구간의 분리 및 점수 계산은 GPU에서 UNMIXX 133.51초, FiLM 112.83초였다. FiLM CPU 실행은 153.28초였다. UNMIXX 입력 2초에 대한 첫 추론은 CPU 9.45초, GPU 1.66초였으며 초기화 후 GPU 5회 평균은 0.73초였다. UNMIXX와 FiLM은 진단 옵션으로 제공한다.

`エスケープ` 실음원에서는 정답 가사 49줄의 중심을 최대 2.4초로 잘라 UNMIXX를 실행했다. 전체 275.574초의 HDemucs 보컬 준비에 GPU 28.35초가 들었고 캐시 재사용은 0.57초였다. UNMIXX 구간 추론은 GPU 38.60초였다. 두 출력 중 약한 쪽과 강한 쪽의 에너지 비율 중앙값은 솔로 25줄에서 2.77%, 듀엣 24줄에서 60.76%였다. 약한 출력이 강한 출력 대비 −20 dB를 넘은 경우는 솔로 24/25줄과 듀엣 24/24줄이었다. `output/voice-models/escape-unmixx-blind.json`은 가창자 이름을 붙이기 전 음향 진단을 담고 있다.

## 로컬 솔로 등록과 검토

`prepare_solo_profiles.py`는 링크 목록의 공식 채널·곡 일치 근거와 단독 캐릭터 크레딧을 확인하고, 예상 파일명, MP3의 제목·가수·앨범 태그, 길이를 대조한다. 일치한 파일과 출처만 기존 분석 큐로 전달한다. 추가 다운로드는 수행하지 않는다. 체크섬이 같고 등록 설정이 동일한 작업은 재실행 시 저장 결과를 재사용한다.

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\prepare_solo_profiles.py `
  --links output\playlist-youtube\idolmaster-solo-links.json --output output\voice-models\solo-enrollment.json
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --manifest output\voice-models\solo-enrollment.json
```

로컬 음원 분석 창에서 가창 모델과 선택적 분리기를 지정한다. 기존 녹음의 싱크를 우선 사용하고, 새 그룹 작업에 싱크와 엔진 선택이 없으면 RomajiASR을 사용한다. 후보별 유사도와 하위 구간 근거는 행을 선택하면 표시된다. `미확정 다시 분석`은 모델·프로필·오디오 준비 버전이 같을 때 기존 추정 행을 재사용한다. 사용자가 확정한 가창자는 녹음별로 따로 저장되며 기존 수동 파트 표를 우선 표시한다.

보정 자료가 확보된 모델에 `character_voice.calibration`으로 캐릭터 ID별 `[미참여 경계, 참여 경계]`를 지정할 수 있다. `*`는 해당 모델·집계 방식의 공통 기준이다. 점수가 미참여 경계 미만이면 미참여로 추정하고 참여 경계 이상이면 참여로 추정한다. `max_score_gap`을 지정하면 최고점과의 차이에도 같은 보류 폭을 적용한다. 모델, 집계 방식, 프로필 구성에 따라 원시 코사인 분포가 달라지므로 별도 검증에서 얻은 기준을 사용한다. 두 분리 모델의 출력은 진단 근거로 기록한다.

## 인원수와 스템 할당 실험

로컬 음원 분석의 `인원수·스템 할당 실험 (UNMIXX)`은 별도 작업으로 실행한다. 2.4초 문맥과 최대 1.2초 간격의 분석 구간을 가사 시간축 안에 배치한다. 구간별 후보가 바뀌면 각각의 구간을 보존한다. 이름을 확정한 솔로의 임시 참조는 8초 이상 떨어진 구간에서 비교용 점수로 기록한다. 임시 참조로 참여 판정을 바꾸거나 등록 프로필을 수정하지 않는다.

인원수는 가수 프로필 없이 UNMIXX 스템의 에너지, 동시 활성, 상관관계, 음정·스펙트럼 차이와 잔여 신호를 사용한다. 두 스템으로 검증한 중첩은 `2명 이상`으로 표시한다. 가창 신원은 BYOL·Contrastive·ECAPA의 녹음별 프로토타입을 사용해 후보의 참여와 부재를 별도로 판정한다. 두 스템에 같은 가수를 중복 할당하지 않고, 반대 할당 및 다른 후보 조합과 비교한다. 스템·후보 근거와 인원수가 충돌하면 미확정으로 표시한다.

기본 프로필 비교에는 세 모델에 공통으로 등록된 녹음 해시만 사용한다. `character_voice.matched_profiles`를 `false`로 지정하면 모델별 사용 가능한 자료를 모두 비교한다. 보정 파일은 선택적 런타임의 `models/character-voice/singing-v2.json`에 두거나 `character_voice.overlap_calibration`에 경로를 지정한다. 파일을 찾지 못하면 근거만 저장한다. `Singer-Informed`를 함께 선택하면 대상 추출과 잔여 근거도 계산한다. 해당 특징으로 보정한 검증기를 지정한 경우에만 판정에 반영한다.

사용자가 검토 창에서 확정한 행은 별도 데이터로 내보낼 수 있다. 내보내기에는 모델의 추정이 들어가지 않는다.

```powershell
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --export-labels JOB_ID output\reviewed-voice-labels.json
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_v2.py --dataset synthetic --device cuda `
  --calibration-output output\voice-models\singing-v2-calibration.json `
  --output output\voice-models\singing-v2-synthetic.json
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_v2.py --dataset escape --offcast --device cuda `
  --window 2400 --window 4000 --window 6000 --window 8000 `
  --output output\voice-models\singing-v2-escape-four.json
```

같은 곡에 에너지 비율 기준을 사후 적용한 결과, 12.34%에서는 듀엣 24/24줄을 검출하고 솔로 3/25줄을 듀엣으로 판정했다. 기준 23.47%에서는 듀엣 23/24줄을 검출하고 솔로 2/25줄을 듀엣으로 판정했다. 이 값은 동일 녹음에서 기준을 고른 가창자 수 진단이다.

별도 곡에서 두 번째 스템을 채택하는 에너지 비율과 가창자 점수 기준 54개 조합을 보정했다. 선택된 에너지 비율은 37.72%였고 후보별 코사인 기준을 0.10 낮춘 뒤 경쟁 후보보다 점수가 높은 경우에만 해당 스템을 채택했다. 평가에서는 솔로 전체 일치 6/20, 듀엣 전체 일치 1/49, 솔로 복수 오검출 2/20, 비참여 후보 오검출률 33.9%, 확정률 51.4%였다. 전체 54개 조합 중 듀엣 전체 일치 최댓값은 1/49였다. 13개 확정 구간으로 맞추면 듀엣 전체 일치는 0건이다. 이 에너지 기준만 `エスケープ`에 적용하면 듀엣 20/24줄을 검출하고 솔로 2/25줄을 듀엣으로 판정했다. 후보 이름까지 판정하는 기준은 진단 옵션으로 유지했다. 전체 조합의 보정과 평가 점수는 `output/voice-models/unmixx-gate-sweep-cuda.json`에 있다.

### `エスケープ` 독립 프로필로 분리 후 가창자 식별

사용자가 제공한 `0246 流星群`과 `0233 アフタースクールパーリータイム`으로 두 가창자를 등록했다. `0307 プラリネ`와 `0323 フローズン・ワード`에서 품질 기준을 통과한 솔로 18개와 두 곡을 혼합한 듀엣 8개로 기준을 보정했다. `0387 エスケープ` 정답 49줄의 중앙 최대 2.4초에서 동일한 기준으로 평가했다.

| 분석 방식 | 솔로 1순위 | 솔로 전체 일치 | 듀엣 전체 일치 | 솔로 복수 오검출 | 비참여 후보 오검출률 | 확정률 |
|---|---:|---:|---:|---:|---:|---:|
| 전체 보컬 + Sony BYOL | 12/25 | 0/25 | 19/24 | 23/25 | 96% | 87.8% |
| UNMIXX 스템 + Sony BYOL | 12/25 | 0/25 | 22/24 | 23/25 | 92% | 91.8% |
| UNMIXX 에너지·음색 기준 + Sony BYOL | 12/25 | 11/25 | 0/24 | 0/25 | 52% | 63.3% |
| FiLM 추출 + Sony BYOL | 12/25 | 10/25 | 5/24 | 9/25 | 60% | 73.5% |
| FiLM 혼합·추출·잔여 보정 | 11/25 | 9/25 | 0/24 | 8/25 | 52% | 73.5% |
| 전체 보컬 + ECAPA | 15/25 | 1/25 | 21/24 | 18/25 | 80% | 81.6% |
| UNMIXX 스템 + ECAPA | 14/25 | 1/25 | 22/24 | 15/25 | 68% | 77.6% |
| UNMIXX 에너지·음색 기준 + ECAPA | 14/25 | 14/25 | 3/24 | 0/25 | 40% | 67.3% |

FiLM은 실제 부재 후보를 조건으로 넣은 25회 모두 혼합 대비 −40 dB보다 큰 신호를 추출했고, 출력 에너지 중앙값은 혼합의 68.8%였다. RTX 3060의 보정·평가 75개 구간 처리 시간은 FiLM과 BYOL 21.40초, UNMIXX와 BYOL 56.04초였다. ECAPA와 UNMIXX는 다른 평가 작업과 동시에 실행해 79.01초가 걸렸다. ECAPA에서 각 방식의 확정 건수를 33개로 맞추면 전체 보컬과 스템 방식 모두 F1 0.891과 비참여 후보 오검출률 100%였고, 에너지·음색 기준을 추가한 방식은 F1 0.684와 비참여 후보 오검출률 39.1%였다. `output/voice-models/escape-named-separation-cuda.json`과 `output/voice-models/escape-ecapa-unmixx-cuda.json`에 줄별 결정과 보정 기준을 기록했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_separation.py --device auto `
  --escape-audio 'output\mp3\0387 エスケープ.mp3' --escape-references-dir output\mp3 `
  --output output\voice-models\escape-named-separation-cuda.json
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_separation.py --device auto `
  --escape-audio 'output\mp3\0387 エスケープ.mp3' --escape-references-dir output\mp3 `
  --escape-offcast-probes --output output\voice-models\escape-four-candidate-separation-cuda.json
.\.venv\Scripts\python.exe -X utf8 tools\benchmark_singing_separation.py --device auto `
  --escape-audio 'output\mp3\0387 エスケープ.mp3' --escape-references-dir output\mp3 `
  --identity ecapa --backend unmixx --output output\voice-models\escape-ecapa-unmixx-cuda.json
```

아마미 하루카와 키사라기 치하야를 부재 후보로 추가한 네 후보 실험에서는 등록곡과 보정곡도 후보별로 나눴다. 독립 솔로 28개와 합성 듀엣 33개로 기준을 보정했다. `エスケープ`의 듀엣 정답은 줄리아와 토코로 메구미 두 사람만 포함한다.

| 분석 방식 | 솔로 전체 일치 | 듀엣 전체 일치 | 솔로 복수 오검출 | 하루카 오검출 | 치하야 오검출 | 확정률 |
|---|---:|---:|---:|---:|---:|---:|
| 전체 보컬 + Sony BYOL | 0/25 | 3/24 | 25/25 | 34/49 | 24/49 | 61.2% |
| UNMIXX 스템 + Sony BYOL | 0/25 | 0/24 | 25/25 | 47/49 | 10/49 | 69.4% |
| UNMIXX 에너지·음색 기준 + Sony BYOL | 11/25 | 0/24 | 0/25 | 0/49 | 0/49 | 61.2% |
| FiLM 추출 + Sony BYOL | 0/25 | 0/24 | 25/25 | 49/49 | 34/49 | 69.4% |
| FiLM 혼합·추출·잔여 보정 | 4/25 | 0/24 | 0/25 | 0/49 | 1/49 | 63.3% |

FiLM의 두 부재 후보 조건은 각각 49/49구간에서 혼합 대비 −40 dB보다 큰 신호를 반환했다. 출력 에너지 중앙값은 하루카 조건 12.9%, 치하야 조건 92.8%였다. 보정과 평가 110개 구간을 실행하는 데 GPU에서 FiLM 59.17초, UNMIXX 106.43초가 걸렸다. `output/voice-models/escape-four-candidate-separation-cuda.json`에서 후보별 오검출과 49줄의 상세 판정을 볼 수 있다.
