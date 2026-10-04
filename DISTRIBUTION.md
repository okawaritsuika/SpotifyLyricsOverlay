# Windows 배포

Windows x64용 배포본은 Python을 따로 설치하지 않고 실행할 수 있습니다.
GitHub Releases에서 Windows ZIP을 내려받고 압축을 모두 푼 뒤
`SpotifyLyricsOverlay.exe`를 실행합니다. 함께 제공되는 안내와 의존성 고지 파일도 보관하세요.

## 기본 포함 범위

- 가사 오버레이와 음악 사전, 일본어 읽기 사전
- 공개용으로 정리한 저장 가사·한국어 번역·발음·곡별 싱크·파트 자료
- 공개 곡·아티스트·앨범 정보와 확인한 곡 연결

AI 모델, Torch/Whisper 등 음원 분석 엔진, 추가 브라우저 연결 모듈은 기본 EXE에
포함하지 않습니다. 해당 기능의 설치 버튼을 눌렀을 때 내려받습니다.
저장된 AI 번역 **결과**는 일반 가사 데이터로 포함하며, 모델 설치 없이 볼 수 있습니다.
기본 실행만으로 무거운 AI 파일을 다운로드하지 않습니다.

## 앱 안에서 업데이트

**더 보기 → 프로그램·자료 업데이트**에서 이 프로젝트의 GitHub 릴리스를 확인합니다.
프로그램과 가사·번역·싱크 자료는 각각 선택하여 내려받습니다.
파일은 릴리스의 SHA-256 체크섬과 대조한 뒤 적용하며, 프로그램 교체와 자료 적용은
기존 앱을 정상 종료한 뒤 진행합니다. 기존 사용자 데이터 위치를 그대로 사용합니다.

자료 업데이트는 새 곡과 비어 있는 번역·싱크를 보충합니다. 이미 있는 시각·보정값·
직접 수정한 가사·번역·개인 설정은 덮어쓰지 않습니다. 원문이나 시간표가 서로 맞지 않는
자료는 보류합니다. 같은 자료를 다시 적용해도 기존 내용을 중복 추가하지 않습니다.
프로그램 버전이 같아도 자료 패키지는 따로 내려받을 수 있습니다.

## 이후 버전과 자료 게시

프로그램을 고칠 때는 `app_version.py`의 `VERSION`과 배포 태그를 같은 버전으로 올립니다.
빌드는 버전이 다르면 중단합니다. 자료를 갱신할 때는 새 공개 스냅샷을 검토한 뒤
`resources/seed_data/`를 교체하고 다시 빌드합니다.
릴리스에는 `SpotifyLyricsOverlay.exe`, Windows ZIP, 소스 ZIP,
`SpotifyLyricsOverlay-data.zip`, `SHA256SUMS.txt`를 함께 올립니다.
체크섬 파일은 해당 릴리스의 모든 EXE·ZIP에서 새로 생성해야 합니다.
기본 GitHub Actions 배포 과정이 이 파일들을 생성하고 게시합니다.
자료 패키지만 생성하려면 `python package_release.py --data`를 실행합니다.
프로그램 버전이 같은 릴리스에서 자료만 갱신할 때도 자료 ZIP과 체크섬 파일을 함께 갱신하세요.

## 사용자 데이터

EXE의 데이터는 `%LOCALAPPDATA%\SpotifyLyricsOverlay\data`에 저장합니다.
공개 데이터는 처음 사용할 때 복사하며, 이미 있는 사용자 파일을 덮어쓰지 않습니다.
소스로 실행하는 개발 설치는 기존 프로젝트의 `data/`를 사용합니다.

배포에 포함하는 자료는 `resources/seed_data/`에 있습니다.
원래의 `data/`를 통째로 복사하지 않습니다. 계정 토큰·쿠키·브라우저 프로필·설정·
로컬 음원 경로·최근 기록·즐겨찾기·수집 작업 상태·진단 로그·녹음 파일은 제외합니다.
자료의 출처 표시는 각 자료에 보존합니다.
직접 수정한 가사와 그 곡의 연결은 보존합니다. 초기 데이터의 `settings.json`은
수정 가사 식별자·우선 사용 표시·곡 연결만 새로 구성하며, 개인 설정 파일을 복사하지 않습니다.

## 소스 배포와 검사

공개 데이터가 없는 개발 PC에서 최초로 자료를 내보낼 때:

```powershell
python public_data.py
python release_audit.py
python package_release.py
```

`public_data.py`는 허용한 파일과 필드만 새 공개 데이터로 내보냅니다.
원본 데이터는 수정하지 않습니다. `package_release.py`는 소스 파일도 명시된 목록으로
선별하고, 개인정보 경로와 자격 증명 형태의 값이 검출되면 패키징을 중단합니다.
출력은 `release/`에 생성합니다.

이미 `resources/seed_data/`가 있다면 그대로 패키징할 수 있습니다. 갱신용 데이터는
`python public_data.py --output output/seed-next`처럼 **새 빈 경로**로 내보내어
검토한 뒤 교체하세요. 내보내기는 기존 스냅샷을 덮어쓰지 않습니다.

새 Git 저장소로 올릴 때는 검사를 통과한 별도 소스 폴더를 만들 수 있습니다.
기존 폴더에는 덮어쓰지 않습니다.

```powershell
python package_release.py --stage release/github-source
```

## EXE 빌드

Windows x64와 Python 3.12 환경에서 별도 빌드 가상환경을 만듭니다.
음원 분석 의존성을 설치하지 않은 환경을 사용합니다.

```powershell
py -3.12 -m venv .exe-venv
.\.exe-venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.exe-venv\Scripts\python.exe build_exe.py
```

빌드 스크립트와 GitHub Actions 워크플로가 같은 파일 목록을 사용합니다.
GitHub 계정 연결 후 `v`로 시작하는 버전 태그를 푸시하면 Windows 배포 파일을
빌드하고 해당 릴리스에 올립니다. 수동 워크플로 실행은 검토용 빌드 파일만 만듭니다.

현재 배포본은 코드 서명 인증서를 사용하지 않습니다. 다른 PC에서의 모든
Windows/Spotify 조합과 선택 다운로드 기능은 실제 검증 범위를 릴리스 노트에서 확인하세요.

## 라이선스와 소스

애플리케이션 코드는 GNU GPL v3 또는 이후 버전으로 배포하며, 전문은 `LICENSE`에 있습니다.
각 릴리스에는 해당 EXE를 만든 전체 애플리케이션 소스 ZIP과 빌드 스크립트를 함께 제공합니다.
외부 라이브러리의 라이선스와 고지는 Windows ZIP의 `THIRD_PARTY_LICENSES/`에 있습니다.
가사·번역·음악 자료와 외부 라이브러리는 애플리케이션 코드의 라이선스 적용 대상이 아니며,
각 원저작자의 권리와 별도 이용 조건이 적용됩니다. 자료의 출처 표시는 데이터에 보존합니다.
