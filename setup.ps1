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
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location (Join-Path $PSScriptRoot 'desktop_ui')
    try {
        & npm ci --ignore-scripts
        if ($LASTEXITCODE -eq 0) {
            & npm exec --no -- install-electron
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Electron 설치에 실패했습니다. 기존 화면은 사용할 수 있습니다. Node.js 22.12 이상을 설치한 뒤 desktop_ui에서 npm ci --ignore-scripts 및 npm exec --no -- install-electron을 실행하세요.'
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Warning 'npm을 찾지 못해 Electron 화면을 설치하지 않았습니다. Node.js 22.12 이상 설치 후 setup.ps1을 다시 실행하세요.'
}
Write-Output '설치 완료. run.bat를 실행하세요.'
