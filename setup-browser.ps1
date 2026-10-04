$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '먼저 setup.ps1로 가사 앱을 설치하세요.' }
& $taskPython -m pip install --upgrade --target (Join-Path $PSScriptRoot '.browser_deps') -r (Join-Path $PSScriptRoot 'requirements-browser.txt')
if ($LASTEXITCODE -ne 0) { throw '내부 브라우저 모듈 설치에 실패했습니다.' }
Write-Host '설치 완료. 앱 재실행 후 가사 웹 연결 설정에서 내부 브라우저를 선택하세요. WebView2 Runtime이 필요합니다.'
