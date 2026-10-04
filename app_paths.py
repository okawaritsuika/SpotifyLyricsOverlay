"""Separate bundled resources from writable user data in the Windows release."""
import os
from pathlib import Path
import shutil
import sys
import sysconfig


def frozen():
    return bool(getattr(sys, 'frozen', False))


def resource_dir():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def user_dir():
    override = os.environ.get('SPOTIFY_LYRICS_HOME')
    return Path(override) if override else Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'SpotifyLyricsOverlay'


def default_data_dir():
    return user_dir() / 'data' if frozen() else resource_dir() / 'data'


def runtime_dir():
    return user_dir() / 'runtime' if frozen() else resource_dir()


def seed_data(directory):
    """Copy public defaults once, preserving every existing user file."""
    source = resource_dir() / 'resources/seed_data'
    directory = Path(directory)
    marker = directory / '.public-seed-installed'
    if marker.exists() or not source.is_dir():return
    for path in sorted(source.rglob('*.json')):
        if path.name == '_manifest.json':continue
        target = directory / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open('xb') as output, path.open('rb') as incoming:
                shutil.copyfileobj(incoming, output)
        except FileExistsError:pass
    marker.touch()


def worker_command(base, module, optional=False):
    if frozen():
        if optional:
            python = runtime_dir() / 'python/python.exe'
            if not python.is_file():
                raise ValueError('선택 기능 설치가 필요합니다. 연결 설정 또는 AI 설치 화면에서 다운로드하세요.')
            # The embedded interpreter is isolated, so add only bundled sources.
            code = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name="__main__")'
            return [str(python), '-X', 'utf8', '-c', code, str(base), str(Path(base) / (module + '.py'))]
        return [sys.executable, '--worker', module]
    executable = Path(getattr(sys, '_base_executable', sys.executable))
    if executable.name.lower() == 'pythonw.exe':executable = executable.with_name('python.exe')
    return [str(executable), '-X', 'utf8', str(Path(base) / (module + '.py'))]


def worker_environment(base, browser=False):
    environment = dict(os.environ)
    if frozen():
        environment.pop('PYTHONPATH', None)
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        bundle = str(resource_dir()).casefold()
        environment['PATH'] = os.pathsep.join(value for value in environment.get('PATH', '').split(os.pathsep)
                                              if not value.casefold().startswith(bundle))
    else:
        paths = [sysconfig.get_path('purelib'), environment.get('PYTHONPATH', '')]
        if browser:paths.insert(0, str(Path(base) / '.browser_deps'))
        environment['PYTHONPATH'] = os.pathsep.join(filter(None, paths))
    environment.update(PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    return environment


def prepare_external_processes():
    if frozen() and sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
