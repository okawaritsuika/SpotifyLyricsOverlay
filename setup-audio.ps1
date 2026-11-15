param([string]$Python = '', [switch]$Gpu, [switch]$VoiceCuda)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$env:PYTHONDONTWRITEBYTECODE = '1'
$taskPython = if ($Python) { $Python } else { Join-Path $PSScriptRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '먼저 setup.ps1로 가사 앱을 설치해 주세요.'
}
& $taskPython -c "import sys; sys.exit(0 if sys.getwindowsversion().build >= 20348 else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Spotify 전용 음원 분석은 Windows 11 또는 빌드 20348 이상이 필요합니다.' }
$torchBuild = if ($VoiceCuda) { 'cu126' } else { 'cpu' }
$whisperGpu = $Gpu -or $VoiceCuda -or (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)
Write-Output "1/4 PyTorch 분석 런타임($torchBuild) 다운로드·설치 중"
& $taskPython -m pip install --no-cache-dir --no-compile --only-binary=:all: "torch==2.8.0+$torchBuild" "torchaudio==2.8.0+$torchBuild" "torchvision==0.23.0+$torchBuild" --index-url "https://download.pytorch.org/whl/$torchBuild"
if ($LASTEXITCODE -ne 0) { throw 'PyTorch 분석 런타임 설치에 실패했습니다.' }
Write-Output '2/4 분석 패키지 다운로드·설치 중'
& $taskPython -m pip install --no-cache-dir --no-compile -r (Join-Path $PSScriptRoot 'requirements-audio.txt')
if ($LASTEXITCODE -ne 0) { throw '음원 분석 패키지 설치에 실패했습니다.' }
if ($whisperGpu) {
    Write-Output '선택한 NVIDIA GPU 분석 런타임 설치 중'
    & $taskPython -m pip install --no-cache-dir --no-compile --only-binary=:all: -r (Join-Path $PSScriptRoot 'requirements-audio-gpu.txt')
    if ($LASTEXITCODE -ne 0) { throw 'GPU 분석 런타임 설치에 실패했습니다. CPU 분석은 계속 사용할 수 있습니다.' }
}
Write-Output '3/4 Whisper small CPU 모델 다운로드 중 (약 486MB)'
& $taskPython -X utf8 (Join-Path $PSScriptRoot 'audio_worker.py') --install-model (Join-Path $PSScriptRoot 'models/whisper-small')
if ($LASTEXITCODE -ne 0) { throw '모델 다운로드에 실패했습니다. 다시 실행하면 이어받습니다.' }
if ($whisperGpu) {
    Write-Output '3/4 Whisper medium GPU 실시간 모델 다운로드 중 (약 1.53GB)'
    & $taskPython -X utf8 (Join-Path $PSScriptRoot 'audio_worker.py') --install-model (Join-Path $PSScriptRoot 'models/whisper-medium') medium
    if ($LASTEXITCODE -ne 0) { throw 'GPU 모델 다운로드에 실패했습니다. 다시 실행하면 이어받습니다.' }
}
Write-Output '4/4 실행 확인 중'
& $taskPython -X utf8 -c "import torch,torchaudio,torchvision,speechbrain,nnAudio,scipy,yaml; print('Voice analysis device:', 'CUDA' if torch.cuda.is_available() else 'CPU')"
if ($LASTEXITCODE -ne 0) { throw '가창자 분석 런타임 검사에 실패했습니다.' }
if (-not $VoiceCuda) {
    & $taskPython -B -X utf8 (Join-Path $PSScriptRoot 'compact_audio_runtime.py') $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw '용량 정리 검증에 실패했습니다. 설치 완료로 처리하지 않았습니다.' }
}
Write-Output '음원 분석 준비 완료. 앱에서 자동 분석을 켜거나 분석 대기를 시작하세요.'
