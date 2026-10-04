"""User-requested GitHub downloads and a detached, rollback-capable updater."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from app_paths import frozen, resource_dir, user_dir, worker_environment
from app_version import VERSION

REPOSITORY = 'okawaritsuika/SpotifyLyricsOverlay'
REPOSITORY_URL = 'https://github.com/' + REPOSITORY
LATEST_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
EXE_ASSET_NAME = 'SpotifyLyricsOverlay.exe'
DATA_ASSET_NAME = 'SpotifyLyricsOverlay-data.zip'
CHECKSUM_ASSET_NAME = 'SHA256SUMS.txt'
ASSET_LIMITS = {EXE_ASSET_NAME: 512 * 1024**2, DATA_ASSET_NAME: 256 * 1024**2,
                CHECKSUM_ASSET_NAME: 1024**2}
_verified_downloads = {}
_download_lock = threading.Lock()
_prepare_lock = threading.Lock()
_pending_job = None
_helper_process = None


class UpdateError(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    html_url: str
    body: str
    assets: dict


def _version(value):
    match = re.fullmatch(r'v?(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', str(value))
    if not match:raise UpdateError('정식 버전 번호를 확인할 수 없습니다.')
    return tuple(map(int, match.groups()))


def is_newer(version, current=VERSION):
    return _version(version) > _version(current)


def _https(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment)
    except (ValueError, TypeError):return False


class _GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        parsed = urlsplit(newurl)
        if not _https(newurl) or parsed.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
            raise UpdateError('GitHub 이외의 다운로드 경로를 거부했습니다.')
        if parsed.hostname == 'github.com' and not parsed.path.startswith('/' + REPOSITORY + '/releases/download/'):
            raise UpdateError('다른 저장소의 다운로드 경로를 거부했습니다.')
        return super().redirect_request(request, response, code, message, headers, newurl)


def _open(url):
    headers = {'User-Agent': 'SpotifyLyricsOverlay/' + VERSION,
               'Accept': 'application/vnd.github+json' if url == LATEST_URL else 'application/octet-stream',
               'X-GitHub-Api-Version': '2022-11-28'}
    return build_opener(_GitHubRedirects()).open(Request(url, headers=headers), timeout=30)


def _limited_read(response, limit):
    body = response.read(limit + 1)
    if len(body) > limit:raise UpdateError('서버 응답이 허용 크기를 초과했습니다.')
    return body


def _asset(release, name):
    if name not in ASSET_LIMITS:raise UpdateError('지원하지 않는 업데이트 파일입니다.')
    _version(release.version)
    if _version(release.tag) != _version(release.version):raise UpdateError('릴리스 버전이 일치하지 않습니다.')
    value = release.assets.get(name)
    if not isinstance(value, dict):raise UpdateError('릴리스에 ' + name + ' 파일이 없습니다.')
    expected = REPOSITORY_URL + '/releases/download/' + release.tag + '/' + name
    if value.get('browser_download_url') != expected or not _https(expected):
        raise UpdateError('공식 저장소의 다운로드 주소가 아닙니다.')
    size = value.get('size')
    if type(size) is not int or not 0 < size <= ASSET_LIMITS[name]:
        raise UpdateError('업데이트 파일 크기가 올바르지 않습니다.')
    return value


def check_release():
    try:
        with _open(LATEST_URL) as response:
            if response.geturl() != LATEST_URL:raise UpdateError('공식 저장소 응답이 아닙니다.')
            value = json.loads(_limited_read(response, 2 * 1024**2))
    except HTTPError as error:
        if error.code == 404:return None
        raise UpdateError(f'GitHub 업데이트 확인에 실패했습니다 (HTTP {error.code}).') from error
    except (URLError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UpdateError('GitHub 업데이트 정보를 받지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.') from error
    if not isinstance(value, dict) or value.get('draft') or value.get('prerelease'):
        raise UpdateError('정식 공개 릴리스 정보를 확인할 수 없습니다.')
    tag = value.get('tag_name', '')
    version = '.'.join(map(str, _version(tag)))
    if value.get('html_url') != REPOSITORY_URL + '/releases/tag/' + tag:
        raise UpdateError('공식 저장소의 릴리스가 아닙니다.')
    raw_assets = value.get('assets')
    if not isinstance(raw_assets, list) or len(raw_assets) > 100:raise UpdateError('릴리스 파일 목록이 올바르지 않습니다.')
    assets = {}
    for item in raw_assets:
        if not isinstance(item, dict) or item.get('name') not in ASSET_LIMITS:continue
        name = item['name']
        if name in assets:raise UpdateError('동일한 업데이트 파일이 여러 개 있습니다.')
        assets[name] = {key: item.get(key) for key in ('name', 'browser_download_url', 'size', 'digest')}
    release = Release(version, tag, value['html_url'], str(value.get('body') or '')[:100000], assets)
    for name in assets:_asset(release, name)
    return release


def _checksums(release):
    asset = _asset(release, CHECKSUM_ASSET_NAME)
    with _open(asset['browser_download_url']) as response:
        body = _limited_read(response, ASSET_LIMITS[CHECKSUM_ASSET_NAME])
    if len(body) != asset['size']:raise UpdateError('체크섬 파일 크기가 달라졌습니다. 다시 확인하세요.')
    try:lines = body.decode('utf-8-sig').splitlines()
    except UnicodeError as error:raise UpdateError('체크섬 파일을 읽을 수 없습니다.') from error
    result = {}
    for line in lines:
        if not line.strip():continue
        match = re.fullmatch(r'([a-fA-F0-9]{64})[ \t]+\*?([A-Za-z0-9][A-Za-z0-9._-]{0,180})', line.strip())
        if not match:raise UpdateError('체크섬 파일 형식이 올바르지 않습니다.')
        digest, name = match.groups()
        if name in result:raise UpdateError('체크섬 파일에 중복 항목이 있습니다.')
        result[name] = digest.lower()
    return result


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024**2):digest.update(block)
    return digest.hexdigest()


def download_asset(release, name, destination, progress=None):
    if name not in (EXE_ASSET_NAME, DATA_ASSET_NAME):raise UpdateError('프로그램 또는 자료 파일을 선택하세요.')
    asset = _asset(release, name)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
    try:
        expected = _checksums(release).get(name)
        if not expected:raise UpdateError('릴리스 체크섬에 선택한 파일이 없습니다.')
        advertised = asset.get('digest')
        if advertised and advertised != 'sha256:' + expected:raise UpdateError('GitHub 파일 체크섬이 일치하지 않습니다.')
        digest = hashlib.sha256();received = 0
        with _open(asset['browser_download_url']) as response, temporary.open('xb') as output:
            if progress:progress(0, asset['size'])
            while block := response.read(1024**2):
                received += len(block)
                if received > asset['size'] or received > ASSET_LIMITS[name]:raise UpdateError('다운로드 크기가 허용 범위를 초과했습니다.')
                output.write(block);digest.update(block)
                if progress:progress(received, asset['size'])
            output.flush();os.fsync(output.fileno())
        if received != asset['size'] or digest.hexdigest() != expected:
            raise UpdateError('다운로드한 파일의 SHA-256 검증에 실패했습니다. 기존 앱은 그대로 유지됩니다.')
        os.replace(temporary, destination)
        with _download_lock:
            _verified_downloads[os.path.normcase(str(destination))] = (release.tag, name, received, expected)
        return destination
    except (HTTPError, URLError, OSError) as error:
        raise UpdateError('업데이트 파일을 저장하지 못했습니다. 연결과 저장 공간을 확인하세요.') from error
    finally:temporary.unlink(missing_ok=True)


def _write_json(path, value):
    temporary = Path(path).with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _restart_command(data_dir):
    executable = Path(sys.executable).resolve()
    if frozen():return [str(executable), '--data-dir', str(Path(data_dir).resolve())]
    windowless = executable.with_name('pythonw.exe')
    if windowless.is_file():executable = windowless
    return [str(executable), str(resource_dir() / 'app.py'), '--data-dir', str(Path(data_dir).resolve())]


def _start_job(request, candidate=None):
    global _pending_job, _helper_process
    with _prepare_lock:
        if _pending_job:raise UpdateError('재시작 또는 업데이트가 이미 준비되었습니다.')
        job = user_dir() / 'updates/jobs' / uuid.uuid4().hex
        job.mkdir(parents=True, exist_ok=False)
        request = {**request, 'parent_pid': os.getpid(), 'job_id': job.name}
        if candidate is not None:
            staged = job / 'candidate.exe'
            shutil.copyfile(candidate, staged)
            if staged.stat().st_size != request['size'] or _sha256(staged) != request['sha256']:
                raise UpdateError('업데이트 파일이 준비 중 변경되었습니다.')
            request['candidate'] = str(staged)
        _write_json(job / 'request.json', request)
        if frozen():
            helper = job / 'Updater.exe'
            shutil.copyfile(sys.executable, helper)
            command = [str(helper), '--worker', 'app_updates', str(job)]
        else:
            helper = Path(getattr(sys, '_base_executable', sys.executable))
            if helper.name.lower() == 'pythonw.exe':helper = helper.with_name('python.exe')
            command = [str(helper), '-X', 'utf8', str(resource_dir() / 'app_updates.py'), '--worker', str(job)]
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        try:
            process = subprocess.Popen(command, cwd=str(job), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=flags, env=worker_environment(resource_dir()))
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if (job / 'ready.json').is_file():
                    _pending_job = job
                    _helper_process = process
                    return job
                if (job / 'error.json').is_file():
                    detail = json.loads((job / 'error.json').read_text(encoding='utf-8'))
                    raise UpdateError(detail.get('error', '업데이트 준비에 실패했습니다.'))
                if process.poll() is not None:raise UpdateError('업데이트 도우미를 시작하지 못했습니다.')
                time.sleep(.1)
            raise UpdateError('업데이트 도우미 준비 시간이 초과했습니다. 기존 앱은 유지됩니다.')
        except Exception:
            (job / 'cancelled').touch()
            raise


def prepare_executable_update(downloaded_path, release, data_dir):
    try:return _prepare_executable_update(downloaded_path, release, data_dir)
    except OSError as error:raise UpdateError('업데이트를 준비하지 못했습니다. 앱 폴더와 저장 공간을 확인하세요.') from error


def _prepare_executable_update(downloaded_path, release, data_dir):
    if not frozen():raise UpdateError('프로그램 업데이트는 배포 EXE에서 사용할 수 있습니다. 소스 실행은 GitHub에서 새 소스를 받아 주세요.')
    path = Path(downloaded_path).resolve(strict=True)
    with _download_lock:verified = _verified_downloads.get(os.path.normcase(str(path)))
    if not verified or verified[:2] != (release.tag, EXE_ASSET_NAME):
        raise UpdateError('이 실행에서 검증한 프로그램 파일이 아닙니다. 다시 다운로드하세요.')
    _, _, size, digest = verified
    if path.stat().st_size != size or _sha256(path) != digest:raise UpdateError('검증 후 업데이트 파일이 변경되었습니다.')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':raise UpdateError('Windows 실행 파일이 아닙니다.')
    target = Path(sys.executable).resolve(strict=True)
    # Detect a read-only installation before asking the GUI to close.
    try:
        with tempfile.TemporaryFile(dir=target.parent):pass
    except OSError as error:raise UpdateError('앱 폴더에 쓸 수 없습니다. 쓰기 가능한 폴더로 앱을 옮긴 뒤 다시 시도하세요.') from error
    return _start_job({'action': 'replace', 'target': str(target), 'size': size, 'sha256': digest,
        'old_sha256': _sha256(target), 'restart_command': _restart_command(data_dir)}, candidate=path)


def restart_app(data_dir):
    try:return _start_job({'action': 'restart', 'restart_command': _restart_command(data_dir)})
    except OSError as error:raise UpdateError('재시작 도우미를 준비하지 못했습니다. 앱은 그대로 유지됩니다.') from error


class _ParentProcess:
    def __init__(self, pid):
        if sys.platform != 'win32':raise UpdateError('Windows 업데이트 도우미입니다.')
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.OpenProcess(0x00100000, False, pid)
        if not self.handle:raise UpdateError('현재 앱의 종료를 기다릴 수 없습니다.')

    def exited(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0xffffffff:raise UpdateError('현재 앱의 종료 상태를 확인하지 못했습니다.')
        return result == 0

    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle = None


def _replace_retry(source, destination, cancelled, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        if cancelled():raise UpdateError('업데이트를 취소했습니다.')
        try:os.replace(source, destination);return
        except PermissionError:
            if time.monotonic() >= deadline:raise
            time.sleep(.2)


def _launch_restarted(command):
    environment = worker_environment(resource_dir()) if frozen() else dict(os.environ)
    return subprocess.Popen(command, cwd=str(Path(command[0]).parent), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)


def _apply_job(job, request):
    cancelled = lambda: (job / 'cancelled').exists()
    if cancelled():raise UpdateError('업데이트를 취소했습니다.')
    command = request['restart_command']
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise UpdateError('재시작 명령 형식이 올바르지 않습니다.')
    if request['action'] == 'restart':
        process = _launch_restarted(command)
        return {'status': 'restarted', 'pid': process.pid}
    if request['action'] != 'replace':raise UpdateError('알 수 없는 업데이트 작업입니다.')
    target = Path(request['target']).resolve()
    candidate = job / 'candidate.exe'
    if Path(command[0]).resolve() != target:raise UpdateError('재시작 대상이 일치하지 않습니다.')
    staged = target.with_name('.' + target.name + '.' + job.name + '.new')
    backup = target.with_name('.' + target.name + '.' + job.name + '.previous')
    moved = False
    try:
        if candidate.stat().st_size != request['size'] or _sha256(candidate) != request['sha256']:
            raise UpdateError('적용 직전 파일 검증에 실패했습니다.')
        if _sha256(target) != request['old_sha256']:raise UpdateError('기존 프로그램이 준비 후 변경되어 교체하지 않았습니다.')
        shutil.copyfile(candidate, staged)
        if _sha256(staged) != request['sha256']:raise UpdateError('복사한 실행 파일 검증에 실패했습니다.')
        _replace_retry(target, backup, cancelled);moved = True
        _replace_retry(staged, target, cancelled)
        process = _launch_restarted(command)
        return {'status': 'updated', 'pid': process.pid, 'backup': str(backup)}
    except Exception:
        if moved:
            _replace_retry(backup, target, lambda: False)
            _launch_restarted(command)
        else:
            _launch_restarted(command)
        raise
    finally:staged.unlink(missing_ok=True)


def _validate_job(job, request):
    if not isinstance(request, dict) or request.get('action') not in ('replace', 'restart'):
        raise UpdateError('업데이트 작업이 올바르지 않습니다.')
    command = request.get('restart_command')
    if (not isinstance(command, list) or not command or not all(isinstance(part, str) and '\0' not in part for part in command)
            or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()):
        raise UpdateError('재시작 프로그램을 찾을 수 없습니다.')
    if request['action'] == 'replace':
        target = Path(request.get('target', ''))
        if not target.is_absolute() or target.resolve() != Path(command[0]).resolve():
            raise UpdateError('교체할 프로그램 경로가 올바르지 않습니다.')
        candidate = job / 'candidate.exe'
        if (candidate.is_symlink() or not candidate.is_file() or type(request.get('size')) is not int
                or not 0 < request['size'] <= ASSET_LIMITS[EXE_ASSET_NAME]
                or candidate.stat().st_size != request['size']
                or _sha256(candidate) != request.get('sha256')
                or _sha256(target) != request.get('old_sha256')):
            raise UpdateError('도우미의 프로그램 파일 검증에 실패했습니다.')


def _notify_failure(error):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            '업데이트 또는 재시작을 완료하지 못했습니다. 기존 자료와 설정은 유지됩니다.\n\n' + str(error),
            'Spotify 가사 · 업데이트 실패', 0x10 | 0x10000)


def run_worker(directory):
    job = Path(directory).resolve(strict=True)
    parent = None
    try:
        request_path = job / 'request.json'
        if request_path.stat().st_size > 65536:raise UpdateError('업데이트 작업 정보가 너무 큽니다.')
        request = json.loads(request_path.read_text(encoding='utf-8'))
        _validate_job(job, request)
        pid = request.get('parent_pid')
        if type(pid) is not int or pid <= 0 or pid == os.getpid():raise UpdateError('종료 대기 대상이 올바르지 않습니다.')
        parent = _ParentProcess(pid)
        if (job / 'cancelled').exists():raise UpdateError('업데이트 준비를 취소했습니다.')
        _write_json(job / 'ready.json', {'pid': os.getpid()})
        deadline = time.monotonic() + 600
        while not parent.exited():
            if (job / 'cancelled').exists():raise UpdateError('업데이트를 취소했습니다.')
            if time.monotonic() >= deadline:raise UpdateError('앱이 종료되지 않아 업데이트를 중단했습니다.')
            time.sleep(.1)
        result = _apply_job(job, request)
        _write_json(job / 'result.json', result)
        return 0
    except Exception as error:
        _write_json(job / 'error.json', {'error': str(error), 'type': type(error).__name__})
        if parent and (job / 'ready.json').exists() and not (job / 'cancelled').exists():
            try:ended = parent.exited()
            except Exception:ended = False
            if ended:_notify_failure(error)
        return 1
    finally:
        if parent:parent.close()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':raise SystemExit(2)
    raise SystemExit(run_worker(sys.argv[2]))
