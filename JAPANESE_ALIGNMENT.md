# 일본어 노래 음소 정렬

로컬 음원 분석의 자동 선택은 가나가 포함된 일본어 가창 가사에 RomajiASR CTC를 사용합니다. 다른 언어의 가사에는 음원에서 언어를 감지하는 stable-ts를 사용합니다. 수동 선택은 다음 실행에도 유지됩니다. 수집 분석 및 실시간 미리보기 설정은 각각 유지됩니다.

JSUT-song 단독 가창 26곡·304구절의 기존 ±500ms 기준은 RomajiASR 89.5%, stable-ts 78.6%, pyshiro 결합 43.4%였습니다. CTC의 현재 기본 프로필은 `retry`입니다. 가나 독음과 모음 연장 후보를 사용하고, 이미 정렬된 줄 사이에서 누락을 다시 검색합니다. 낮은 품질 후보는 로컬 분석 창에서 재생과 대조해 빈 구절에 적용할 수 있습니다.

RomajiASR를 불러오거나 실행하지 못하면 stable-ts로 검토용 후보를 생성합니다. 결합 엔진의 pyshiro 준비나 추론만 실패하면 같은 오디오에서 이미 구한 RomajiASR 단독 후보를 저장합니다. 결과에 실제 엔진과 pyshiro 오류가 기록됩니다.

## Windows 설치

소스 폴더에서 기존 AI 선택 설치를 마친 뒤 PowerShell에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install pyopenjtalk-plus==0.4.1.post10 onnx==1.23.2 onnxruntime==1.23.2
```

[RomajiASR v1.0.0 릴리스](https://github.com/Xiantaidu/RomajiASR/releases/tag/v1.0.0)의 `model_onnx.zip`을 받아 SHA-256을 확인하고, `model.onnx`와 `phoneme_vocab.json`이 `models/romaji-asr` 바로 아래에 있도록 압축을 풉니다.

```text
8aa7e5c6f66cbe00018a33717531bd7f14e096fb6a7f0fcaf4e7c21623e62166
```

```powershell
.\.venv\Scripts\python.exe -m romaji_ctc models/romaji-asr
```

변환은 공개 그래프의 ArgMax 직전 출력을 `model-logits.onnx`로 저장합니다. 입력은 모노 16 kHz이고 출력은 20ms마다 161개 토큰의 logits입니다. 엔진은 겹치는 오디오 창에서 추론하고, CTC로 각 줄의 후보를 찾은 뒤 생략과 순서를 허용하는 동적 계획법으로 전곡 후보를 선택합니다.

결합 엔진을 별도로 비교하려면 [pyshiro 0.1.0 소스](https://github.com/wavtechyukky/pyshiro/tree/49851cd4701d300eaf310b274f7c3b3e594d3824)를 `models/pyshiro`에 풀고 별도 Python 환경에 설치합니다. 그 소스의 `checkpoint/pyshiro-jp-v2.hsmm`과 `checkpoint/pyshiro-jp-v2_phonemap.json`을 함께 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m venv models/japanese-alignment-venv
.\models\japanese-alignment-venv\Scripts\python.exe -m pip install .\models\pyshiro
```

EXE 설치에서는 기존 선택 오디오 런타임의 `runtime/python/python.exe`에 `pyopenjtalk-plus`와 ONNX Runtime을 설치합니다. 소스에서 변환한 모델 디렉터리와 별도 pyshiro 파일들을 `runtime/models`에 배치하거나 설정에 절대 경로를 지정합니다. Python 3.10용 Windows x64 휠을 사용합니다. RomajiASR 소스는 Apache 2.0이고 pyshiro 소스는 GPLv3입니다. EXE 패키지에서 모델 가중치와 선택 런타임을 제외하며 pyshiro는 로컬 별도 프로세스로 호출합니다. 가중치를 별도로 재배포할 때는 해당 아티팩트의 고지와 배포 조건을 확인합니다.

## 설정과 후보

`data/settings.json`의 기존 항목에 `audio_alignment`를 추가할 수 있습니다. EXE의 사용자 데이터는 기본적으로 `%LOCALAPPDATA%/SpotifyLyricsOverlay/data`에 있습니다. 상대 경로의 기준은 소스 폴더 또는 EXE 사용자 디렉터리의 `runtime`입니다.

```json
{
  "audio_alignment": {
    "engine": "romaji_ctc",
    "device": "cpu",
    "realtime": false,
    "readings": {
      "星井美希": "ほしいみき",
      "LOVE": "ラブ"
    }
  }
}
```

독음 교정 사전은 단어 또는 한 줄 전체를 가나 독음에 매핑합니다. 인식되지 않는 한자나 라틴 문자 구간은 교정 대상으로 남습니다. `model_dir`, `pyshiro.python`, `pyshiro.checkpoint`, `pyshiro.phonemap`으로 다른 로컬 설치를 지정할 수 있습니다.

CPU 설치는 `onnxruntime`을 사용합니다. CUDA나 DirectML을 사용할 때는 같은 Python 환경에서 해당 ONNX Runtime 패키지 하나를 선택하고 `device`를 `cuda`, `dml` 또는 `auto`로 설정합니다. CUDA에는 패키지 버전에 맞는 CUDA 및 cuDNN이 필요합니다. 실행 결과의 `runtime.provider`에서 실제 장치를 확인할 수 있습니다. [CUDA 요구 사항](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html), [DirectML 요구 사항](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html).

`realtime: true`는 실험용 CPU 음소 추적을 켭니다. 기본 실시간 추적은 기존 Whisper 경로를 사용합니다. 실험용 추적은 근처 구절을 검색하고 연속 창에서 일치 여부를 확인하며 모델 실행 오류가 발생하면 CPU Whisper로 복구합니다.

후보 JSON의 `line_alignment`에는 줄 번호, 곡 기준 시작·끝, 모델, CTC 점수 및 후보 점수 차이, 정밀 보정 구간과 충돌 사유가 들어갑니다. 점수는 확률 보정을 거치지 않은 비교값입니다. 두 엔진의 시작점 차이가 650ms를 넘으면 충돌로 기록합니다. 분석에는 WASAPI의 연속 수집 구간과 해당 Spotify 시작 위치를 사용합니다.

`profile: "baseline"` 설정으로 기존 CTC 동작을 다시 비교할 수 있습니다. `readings`, `global`, `onset`, `retry`는 발음 후보, 순서를 유지하는 빈 구간 재탐색, 첫 음소 경계 보정, 제한된 음향 조건의 재시도를 차례로 추가합니다. 후보의 품질은 CTC 음소 커버리지와 경로 간 점수 차이 등으로 표시합니다.

로컬 음원 분석 창에서 **CTC 누락 복구**를 선택하면 새 분석과 **미확정 싱크 재분석**에 `recovery` 프로필을 사용할 수 있습니다. 이 프로필은 인접 시작점이 정해진 짧은 구간에서 첫 가나의 독음 후보, 마지막 모라 앞까지의 음향 경로, 일부 약한 음소를 허용하는 국소 경로를 확인합니다. 복구 후보는 낮은 품질로 표시되며, 미리 듣고 줄을 선택해 적용할 수 있습니다. 수집 분석에서 같은 프로필을 명시적으로 쓰려면 `audio_alignment.profile`을 `"recovery"`로 지정합니다.

자동 수집이 중단되면 같은 녹음의 다음 재생에서 남은 구간을 수집하고 검토용 후보를 이어 붙입니다. 기존에 적용된 싱크만 정렬 위치를 제한하는 데 사용합니다.

## 독립 정확도 평가

Spotify와 별도로 로컬 파일도 비교할 수 있습니다. manifest의 `tracks` 배열에 `song_id`, `split`, `audio`, `lines`, `reference_times`, `reference_source`를 넣습니다. `audio`는 manifest에 상대적인 파일 경로입니다. `lines`는 가사 원문 배열이고 `reference_times`는 같은 순서의 기준 시작 시각(초)입니다. 검증하지 못한 시각은 `null`로 표시합니다. 분석 프로세스는 오디오와 원문만 읽고, 평가 프로세스에서 출력과 기준을 비교합니다. `--track`으로 평가할 곡을 지정할 수 있습니다.

벤치마크에서는 요청한 엔진의 실행이 다른 엔진으로 전환되면 오류와 실제 엔진을 기록하고 해당 실행을 요청 엔진의 정확도 집계에서 제외합니다.

실제 단독 가창으로 재현하려면 [JSUT-song 공식 페이지](https://sites.google.com/site/shinnosuketakamichi/publication/jsut-song)의 음원 및 HTS 라벨 ZIP을 사용합니다. 라벨 URL의 연결이 실패하면 같은 제공자의 [공식 코퍼스 서버](https://ss-takashi.sakura.ne.jp/corpus/jsut-song_label.zip)에서 라벨을 받을 수 있습니다. 전처리 도구가 `pau`, `sil`, `br`을 구절 경계로 보고 가나 발음과 기준 시각을 분리합니다. 이 평가는 발음이 주어진 일본어 동요 가창에서 구절 시작점을 비교합니다.

```powershell
.\.venv\Scripts\python.exe tools/prepare_jsut_song_benchmark.py jsut-song_ver1.zip jsut-song_label.zip output/jsut-benchmark
.\.venv\Scripts\python.exe tools/benchmark_japanese_alignment.py output/jsut-benchmark/manifest.json --output output/jsut-benchmark/comparison.json --pyshiro-python models/japanese-alignment-venv/Scripts/python.exe --pyshiro-checkpoint models/pyshiro/checkpoint/pyshiro-jp-v2.hsmm --pyshiro-phonemap models/pyshiro/checkpoint/pyshiro-jp-v2_phonemap.json
```

`tools/benchmark_japanese_alignment.py --help`에서 오디오와 기준 싱크가 있는 로컬 manifest 실행 옵션을 확인합니다. 캡처 출처, 실제 곡 시작 위치, 녹음 버전 및 원문 일치를 확인한 곡을 `calibration` 또는 `evaluation`에 각각 한 번만 넣습니다. 분석 프로세스에는 16kHz 오디오, 가사 원문 및 수집 위치만 제공하고, 기준 시각은 결과가 나온 뒤 비교합니다. 원본 WAV와 manifest의 개인 경로는 EXE와 소스 배포에 포함하지 않습니다.

`tools/benchmark_romaji_profiles.py`는 같은 혼합 음원에서 추론한 결과로 CTC 프로필을 비교합니다. `--stems --stem-device cuda`는 설치된 HDemucs 가중치로 보컬 스템과 선택적 결합을 추가 평가합니다. `--ctc-diagnostics`를 지정하면 첫 번째 CTC 경로와 후보 시각을 줄별 JSON에 기록합니다. 기존 실패 줄의 파형 및 오디오 검토 자료는 `tools/report_romaji_failures.py --help`의 입력으로 재현할 수 있습니다.

manifest의 `tracks` 배열에서 각 항목에 `song_id`, `split`, `capture`, `spotify_track_id`를 넣습니다. `capture`는 `capture.wav`, `request.json`, `capture-info.json`이 함께 있는 폴더입니다. `reference`는 기본값 `spotify` 또는 `manual`을 선택합니다. 완전한 녹음에서 실제로 부르지 않은 줄을 확인했다면 0부터 세는 줄 번호를 `absent_indices`에 추가합니다.
