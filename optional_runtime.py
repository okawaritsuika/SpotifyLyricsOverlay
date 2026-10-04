"""Explicit, per-user downloads for optional browser and audio dependencies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
from zipfile import ZipFile

from app_paths import resource_dir, runtime_dir, worker_command, worker_environment

PYTHON_URL = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
PYTHON_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'


def installed(kind):
    root = runtime_dir()
    if not (root / 'python/python.exe').is_file():return False
    packages = root / 'python/Lib/site-packages'
    names = ('webview',) if kind == 'browser' else ('faster_whisper', 'stable_whisper', 'torch', 'torchaudio')
    return ((root / (kind + '-installed.json')).is_file()
            and all((packages / name).is_dir() for name in names))


def download(url, destination, expected):
    digest = hashlib.sha256()
    with urlopen(url, timeout=60) as response, Path(destination).open('wb') as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk);output.write(chunk)
    if digest.hexdigest() != expected:
        Path(destination).unlink(missing_ok=True)
        raise ValueError('다운로드 검증에 실패했습니다. 다시 설치하세요.')


def command(args):
    subprocess.run(list(map(str, args)), check=True, env=worker_environment(resource_dir()),
                   stdout=sys.stdout,stderr=sys.stderr,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def ensure_python():
    root = runtime_dir() / 'python'
    python = root / 'python.exe'
    ready_marker=root / '.python-ready'
    if not python.is_file() or not ready_marker.is_file():
        print('1/4 선택 기능용 Python 다운로드 중 (약 11MB)', flush=True)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='spotify-lyrics-runtime-') as temporary:
            archive = Path(temporary) / 'python.zip'
            download(PYTHON_URL, archive, PYTHON_SHA256)
            with ZipFile(archive) as bundle:bundle.extractall(root)
    # Absolute paths also work inside pip's temporary build subprocesses.
    (root / 'python312._pth').write_text(str(root / 'python312.zip') + '\n' + str(root) + '\n'
                                       + str(root / 'Lib/site-packages') + '\nimport site\n', encoding='utf-8')
    ready_marker.touch()
    if not (root / 'Lib/site-packages/pip').is_dir():
        with urlopen('https://pypi.org/pypi/pip/25.1.1/json', timeout=30) as response:
            metadata = json.load(response)
        wheel = next(item for item in metadata['urls'] if item['filename'].endswith('-py3-none-any.whl'))
        if urlsplit(wheel['url']).hostname != 'files.pythonhosted.org':raise ValueError('예상하지 못한 패키지 출처입니다.')
        with tempfile.TemporaryDirectory(prefix='spotify-lyrics-pip-') as temporary:
            destination = Path(temporary) / wheel['filename']
            download(wheel['url'], destination, wheel['digests']['sha256'])
            code = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("pip",run_name="__main__")'
            command([python, '-c', code, destination, 'install', '--no-cache-dir', destination])
    return python


def install(kind):
    if kind not in ('audio', 'browser'):raise ValueError('알 수 없는 선택 기능입니다.')
    python = ensure_python()
    base = resource_dir()
    pip = [python, '-m', 'pip', 'install', '--no-cache-dir', '--no-compile', '--disable-pip-version-check']
    print('2/4 선택 기능 패키지 다운로드·설치 중', flush=True)
    command(pip + ['setuptools', 'wheel'])
    if kind == 'browser':
        command(pip + ['--no-build-isolation', '-r', base / 'requirements-browser.txt',
                      'beautifulsoup4==4.15.0', 'pykakasi==2.3.0'])
        command([python, '-c', 'import webview;import clr'])
    else:
        command(pip + ['--only-binary=:all:', 'torch==2.8.0+cpu', 'torchaudio==2.8.0+cpu',
                      '--index-url', 'https://download.pytorch.org/whl/cpu'])
        command(pip + ['--no-build-isolation', '-r', base / 'requirements-audio.txt',
                      'winrt-runtime==3.2.1', 'winrt-Windows.Foundation==3.2.1',
                      'winrt-Windows.Foundation.Collections==3.2.1', 'winrt-Windows.Media.Control==3.2.1'])
        print('3/4 small 모델 다운로드 중 (약 486MB)', flush=True)
        command(worker_command(base, 'audio_worker', optional=True) + ['--install-model', str(runtime_dir() / 'models/whisper-small')])
        command([python, '-c', 'import torch,torchaudio,stable_whisper,faster_whisper;print(torch.ones(1).sum())'])
        command(worker_command(base, 'compact_audio_runtime', optional=True) + [str(runtime_dir())])
    print('4/4 설치 확인 완료', flush=True)
    (runtime_dir() / (kind + '-installed.json')).write_text(json.dumps({'version': 1}), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 2:raise SystemExit(2)
    try:
        from windows import SingleInstance
        with SingleInstance(runtime_dir() / '.installer') as instance:
            if not instance.acquired:raise ValueError('다른 선택 기능 설치가 진행 중입니다. 완료 후 다시 시도하세요.')
            install(sys.argv[1])
    except Exception as error:
        print('설치 실패: ' + str(error), flush=True)
        raise SystemExit(1)
