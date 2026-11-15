# 로컬 음원 분석

## 가사창에서 분석

Spotify에서 곡을 재생하고 원문 가사를 연결한 다음 가사창 메뉴에서 **로컬 음원 분석…**을 엽니다. 같은 녹음 버전이며 분석 권한이 있는 파일을 선택합니다. 음악 사전의 **로컬 파일** 탭에서도 목록에 있는 파일을 현재 재생에 연결할 수 있습니다. 파일이 아직 없으면 음악 폴더를 추가해 목록을 만들 수 있습니다.

자동 설정은 가나가 일정 비율 이상 포함된 가창 가사에 RomajiASR CTC를 선택하고, 그 밖의 가사에는 음원 언어를 감지하는 stable-ts를 선택합니다. 마지막 엔진 선택은 다음에도 사용합니다. 창 상단에서 설치된 엔진과 감지된 GPU를 확인할 수 있습니다. 결과 상태에는 요청한 엔진과 실제로 사용한 엔진, 장치와 전환 이유가 표시됩니다. RomajiASR를 실행하지 못하면 stable-ts 검토 후보를 표시합니다. RomajiASR + pyshiro는 직접 선택할 수 있는 비교 옵션입니다. [일본어 정렬 설치 및 평가](JAPANESE_ALIGNMENT.md)

결과 표에서 기존 싱크의 출처와 시각, 초기 후보와의 차이, 품질, 재분석 시각을 비교할 수 있습니다. 품질은 정렬 근거에 따른 검토 표시입니다. **미확정 싱크 재분석**은 양옆의 후보나 곡 경계로 범위를 확인할 수 있는 짧은 구간을 분석합니다. 결과는 기존 후보 옆에 별도로 저장됩니다. 각 시각의 앞뒤를 들어 보고 원하는 후보를 선택해 빈 구절에 저장할 수 있습니다. **전체 초기 후보 저장**은 품질이 보통 이상인 초기 후보를 빈 구절에 저장합니다. 공급자 싱크, 저장된 싱크 및 수동 입력이 우선합니다. 작업 중 취소할 수 있으며, 초기 분석이 중단된 작업은 **이어서 분석**에서 재시도할 수 있습니다.

가창자 자동 분석은 **가창 분석 옵션**을 켜고 캐릭터 ID를 지정했을 때 실행됩니다. 기존 가사 싱크가 있으면 가창 구간을 나누는 데 사용합니다. 분석한 가창 정보는 같은 가사와 재생에 다시 연결됩니다. 출처가 있는 가창 표기가 우선하며, 모델 결과는 별도로 **가창 추정** 또는 **가창자 미확정**으로 표시됩니다.

## 일괄 등록과 처리

소스 설치에서 PowerShell로 실행합니다. 배포 EXE에서도 오디오 선택 설치 후 가사창의 로컬 분석 메뉴를 사용할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --data-dir data --manifest D:\my-audio\analysis.json
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --data-dir data --manifest D:\my-audio\analysis.json --watch 30
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --data-dir data --job JOB_ID --retry
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --data-dir data --job JOB_ID --refresh-voice
.\.venv\Scripts\python.exe -X utf8 local_analysis_worker.py --data-dir data --show JOB_ID
```

manifest의 상대 경로는 manifest 파일의 폴더가 기준입니다. `--watch 30`으로 실행하면 지정한 폴더와 manifest를 30초 간격으로 확인하고 새 파일을 등록합니다. Ctrl+C를 누르면 감시를 종료합니다. 아래의 파일명, 곡 ID와 캐릭터 ID를 실제 자료로 바꿉니다.

```json
{
  "folders": ["music"],
  "inputs": [
    {
      "kind": "lyrics",
      "path": "music/solo.wav",
      "song_id": "library-song-id",
      "alignment": {"engine": "romaji_ctc", "device": "cpu"}
    },
    {
      "kind": "solo",
      "path": "music/solo.wav",
      "character": {"id": "character-a", "name": "캐릭터 A", "actor": "성우 이름"},
      "source": "출처와 캐릭터를 확인한 솔로 녹음",
      "evidence": "verified_solo",
      "isolated": false
    },
    {
      "kind": "speech",
      "path": "lines/character-a.flac",
      "character": {"id": "character-a", "name": "캐릭터 A"},
      "source": "확인한 대사 녹음",
      "evidence": "user_verified"
    },
    {
      "kind": "group",
      "song_id": "group-lyric-id",
      "title": "Spotify에 표시되는 제목",
      "artist": "Spotify에 표시되는 가수",
      "album": "Spotify에 표시되는 앨범",
      "expected_duration": 232.7,
      "link_playback": true,
      "participants": ["character-a", "character-b"]
    }
  ]
}
```

- 가사는 `data/library.json`의 `song_id`나 `lines` 원문 문자열 배열로 제공합니다. 그룹곡에 기존 싱크가 있으면 같은 길이의 `times` 배열을 입력할 수 있습니다. 단위는 초이며 미확인 줄은 `null`입니다. 새 싱크도 생성하려면 `recompute_alignment: true`를 지정합니다. 이때 새 정렬에는 원문과 오디오만 입력합니다.
- `path`를 생략하면 음악 사전에서 명시적으로 연결한 파일이나, 등록된 폴더의 ISRC 또는 전체 제목·가수·앨범과 길이가 일치하는 단일 파일을 찾습니다. 여러 버전이 남으면 `ambiguous_audio`, 파일이 없으면 `audio_missing`을 출력합니다. 파일을 추가한 뒤 같은 명령을 다시 실행할 수 있습니다. 직접 지정한 단일 파일은 PyAV/FFmpeg가 디코딩할 수 있는 형식을 받으며, 폴더 검색은 로컬 파일 탭의 지원 확장자를 사용합니다.
- `track_id`, `isrc`, `version`을 녹음에 저장할 수 있습니다. Windows 재생 세션에서 관찰 가능한 표시 문자열과 길이로 오버레이에 연결하므로, 길이와 표시 정보가 같은 녹음은 파일을 직접 지정해 구분합니다. 분석 결과는 SHA-256과 작업 설정이 같으면 재사용합니다.
- 가창 프로필을 만드는 작업은 `solo`, 대사 프로필은 `speech`입니다. `evidence`에 `verified_solo`, `official`, `user_verified` 중 하나와 출처를 지정합니다. 가창자가 확인된 부분만 등록하려면 `ranges`에 시작·끝 밀리초 쌍을 넣습니다. `isolated: true`는 이미 분리된 보컬 파일에 사용합니다. 분석이 추정한 가창자는 프로필에 자동 등록되지 않습니다.
- 작업과 결과는 데이터 폴더의 `local_analysis.sqlite3`에, 보컬 분리 캐시는 `voice_cache`에 기록합니다. 전체 입력 목록이 같은 경우 동일 작업을 다시 실행하지 않습니다. 새 프로필이 이전 그룹 결과에 사용된 모델과 후보에 해당하면 재분석 대기로 전환합니다.

분석에 필요한 가중치와 모델 의존성은 [캐릭터 음성 분석](VOICE_ANALYSIS.md)을 따릅니다. Spotify 오프라인 저장소는 로컬 음원 입력 대상에서 제외됩니다.

## 재생목록 로컬 음원 자동 분석

`run-playlist-mp3-analysis.bat`는 `output/mp3`의 완성된 음원을 재생 없이 분석합니다. 재생목록 순번과 제목으로 MP3, WAV, FLAC 또는 로컬 라이브러리에서 지원하는 다른 음원 형식을 찾습니다. 같은 순번과 제목에 파일이 여러 개 있으면 `ambiguous_audio`로 기록합니다. 트랙 ID로 저장 원문을 불러오고 싱크가 비어 있는 곡부터 처리합니다. 대기 중에는 10초마다 새 파일을 확인합니다. 현재 파일만 처리하려면 `run-playlist-mp3-analysis.bat --once`를 실행합니다.

일본어 가창은 RomajiASR를 기본으로 사용합니다. 분석 장치는 자동으로 선택하며 `data/settings.json`의 `audio_alignment`에서 엔진과 장치를 지정할 수 있습니다. SHA-256, 원문과 독음, 정렬 설정, 알고리즘 버전 및 모델 파일 정보가 같은 이전 계산을 `data/local_analysis.sqlite3`에서 재사용합니다.

신규 결과는 길이가 맞고 음원의 ISRC 또는 전체 제목·가수·앨범 태그가 트랙과 일치해야 자동 표시 대상이 됩니다. 대응하는 Spotify 또는 수동 기준 줄이 충분할 때는 시작 시각 차이도 비교합니다. 확인된 후보 중 전체 구절의 60% 이상이 사용 가능하면 빈 구절에 **MP3 음원 싱크**를 표시합니다. 전체 후보는 **로컬 음원 분석…**에서 확인하거나 진행 로그의 `id`로 `local_analysis_worker.py --show JOB_ID`를 실행해 볼 수 있습니다. Ctrl+C로 종료한 뒤 다시 실행하면 중단된 작업부터 처리할 수 있습니다.
