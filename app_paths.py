"""Separate bundled resources from writable user data in the Windows release."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import sysconfig


CATALOG_RELEASE_STAMP = 'release_snapshot_at'


def catalog_snapshot_time(value, fields=('generated_at', 'verified_at')):
    for field in fields:
        stamp = value.get(field) if isinstance(value, dict) else None
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def prefer_catalog_update(base, update):
    """Preserve existing local snapshots and avoid overriding a newer app bundle."""
    if update is None:
        return False
    bundled = catalog_snapshot_time(base)
    downloaded = catalog_snapshot_time(update, (CATALOG_RELEASE_STAMP,))
    return bundled is None or downloaded is None or downloaded >= bundled


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


def webview2_available():
    import ctypes

    loader = resource_dir() / 'webview/lib/runtimes/win-x64/native/WebView2Loader.dll'
    get_version = ctypes.WinDLL(str(loader)).GetAvailableCoreWebView2BrowserVersionString
    get_version.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))
    get_version.restype = ctypes.c_long
    version = ctypes.c_void_p()
    result = get_version(None, ctypes.byref(version))
    try:
        if result == 0:
            return bool(version.value)
        if result & 0xFFFFFFFF == 0x80070002:  # No installed WebView2 runtime.
            return False
        raise ctypes.WinError(result)
    finally:
        if version.value:
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = (ctypes.c_void_p,)
            free.restype = None
            free(version)


def seed_data(directory):
    """Install bundled public data once per release while holding SingleInstance."""
    source = resource_dir() / 'resources/seed_data'
    if not source.is_dir():
        return

    from app_version import VERSION
    from data_updates import (MAX_ENTRY_BYTES, MAX_FILES, MAX_TOTAL_BYTES, _json, _name,
                              _plan, _read, _root, _safe, _validate_documents)
    from storage import atomic_json

    directory = _root(directory)
    marker = directory / '.public-seed-installed'
    manifest = source / '_manifest.json'
    _safe(manifest)
    with manifest.open('rb') as incoming:
        manifest_body = incoming.read(MAX_ENTRY_BYTES + 1)
    if len(manifest_body) > MAX_ENTRY_BYTES:
        raise ValueError('Bundled public data manifest is too large')
    release = {'version': 1, 'app_version': VERSION,
               'manifest_sha256': hashlib.sha256(manifest_body).hexdigest()}
    _safe(marker)
    installed = marker.exists()
    try:
        receipt = _read(marker)
    except (ValueError, UnicodeError):
        receipt = None  # Pre-versioned installs used an empty marker.
    if (isinstance(receipt, dict) and all(receipt.get(key) == release[key]
                                        for key in ('version','manifest_sha256'))
            and isinstance(receipt.get('content_sha256'), str)):
        return

    payload = hashlib.sha256()
    documents, sizes = {}, {}
    total = len(manifest_body)
    seen = set()
    safe_directories = set()

    def check_path(path):
        if path.parent not in safe_directories:
            _safe(path.parent)
            safe_directories.add(path.parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked paths are not allowed in public data')
        return info

    for path in sorted(source.rglob('*.json'), key=lambda path: path.relative_to(source).as_posix()):
        name = _name(path.relative_to(source).as_posix())
        if name.casefold() in seen or len(seen) >= MAX_FILES:
            raise ValueError('Duplicate or excessive bundled public data files')
        seen.add(name.casefold())
        if name == '_manifest.json':
            continue
        info = check_path(path)
        if info is None or not stat.S_ISREG(info.st_mode):
            raise ValueError('Bundled public data file is missing or invalid')
        size = info.st_size
        total += size
        if size > MAX_ENTRY_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError('Bundled public data is too large')
        with path.open('rb') as incoming:
            body = incoming.read(size + 1)
        if len(body) != size:
            raise ValueError('Bundled public data changed during installation')
        payload.update(name.encode('utf-8') + b'\0' + hashlib.sha256(body).digest())
        if installed:
            try:
                documents[name] = _json(body)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ValueError(f'Invalid bundled public data: {name}') from error
            sizes[name] = size
            continue

        target = directory / name
        check_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            output = target.open('xb')
        except FileExistsError:
            continue
        try:
            with output:
                output.write(body)
        except OSError:
            target.unlink(missing_ok=True)
            raise

    if installed:
        try:
            documents['_manifest.json'] = _json(manifest_body)
            sizes['_manifest.json'] = len(manifest_body)
            _validate_documents(documents, sizes)
        except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError) as error:
            raise ValueError('Invalid bundled public data') from error
        missing = object()
        local_data = {}

        def read_local(path, default=None):
            if path not in local_data:
                info = check_path(path)
                if info is None:
                    local_data[path] = missing
                else:
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENTRY_BYTES:
                        raise ValueError('Invalid local public data file')
                    with path.open('rb') as incoming:
                        body = incoming.read(info.st_size + 1)
                    if len(body) != info.st_size:
                        raise ValueError('Local public data changed during installation')
                    local_data[path] = _json(body)
            value = local_data[path]
            return default if value is missing else value

        try:
            changes, _ = _plan(directory, documents, read=read_local)
        except (KeyError, TypeError, AttributeError, RecursionError) as error:
            raise ValueError('Invalid current data; bundled data was not applied') from error
        for name, value in sorted(changes.items()):
            target = directory / name
            _safe(target)
            atomic_json(target, value)
    _safe(marker)
    atomic_json(marker, {**release, 'content_sha256': payload.hexdigest()})


def worker_command(base, module, optional=False):
    if frozen():
        if optional:
            python = runtime_dir() / 'python/python.exe'
            if not python.is_file():
                raise ValueError('AI 음원 분석 설치가 필요합니다. AI 설치 화면에서 다운로드하세요.')
            # The embedded interpreter is isolated, so add only bundled sources.
            code = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name="__main__")'
            return [str(python), '-X', 'utf8', '-c', code, str(base), str(Path(base) / (module + '.py'))]
        return [sys.executable, '--worker', module]
    executable = Path(getattr(sys, '_base_executable', sys.executable))
    if executable.name.lower() == 'pythonw.exe':executable = executable.with_name('python.exe')
    return [str(executable), '-X', 'utf8', str(Path(base) / (module + '.py'))]


def worker_environment(base, browser=False, *, prefer_inherited=False, reuse_frozen_bundle=False):
    environment = dict(os.environ)
    if frozen():
        environment.pop('PYTHONPATH', None)
        if reuse_frozen_bundle:
            # Only workers that exit with the app may use its temporary bundle.
            environment.pop('PYINSTALLER_RESET_ENVIRONMENT', None)
        else:
            environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        bundle = str(resource_dir()).casefold()
        environment['PATH'] = os.pathsep.join(value for value in environment.get('PATH', '').split(os.pathsep)
                                              if not value.casefold().startswith(bundle))
    else:
        paths = [sysconfig.get_path('purelib'), environment.get('PYTHONPATH', '')]
        if prefer_inherited:
            paths.reverse()
        if browser:paths.insert(0, str(Path(base) / '.browser_deps'))
        environment['PYTHONPATH'] = os.pathsep.join(filter(None, paths))
    environment.update(PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    return environment


def prepare_external_processes():
    if frozen() and sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
