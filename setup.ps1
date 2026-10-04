param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 이상을 설치하거나 -Python 경로를 지정하세요.' }
}
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '필수 모듈 설치에 실패했습니다.' }
& (Join-Path $PSScriptRoot 'setup-browser.ps1')
Write-Output '설치 완료. run.bat를 실행하세요.'
