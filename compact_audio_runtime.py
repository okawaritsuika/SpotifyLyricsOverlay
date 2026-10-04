"""Remove only verified, unused Torch build archives during an explicit install."""
from pathlib import Path
import sys
import sysconfig

BUILD_ARCHIVES=('dnnl.lib','kineto.lib','libprotobuf.lib','libprotobuf-lite.lib','libprotoc.lib')


def check_runtime(packages):
    import torch
    import torchaudio
    import stable_whisper
    if str(torch.__version__)!='2.8.0+cpu':
        raise ValueError('검증한 CPU Torch 2.8.0 구성이 아니므로 파일을 정리하지 않았습니다.')
    if Path(torch.__file__).resolve().parent!=packages/'torch':
        raise ValueError('다른 Python 환경의 Torch이므로 파일을 정리하지 않았습니다.')
    torch.set_num_threads(1)
    if float(torch.ones(1).sum())!=1:raise ValueError('런타임 검사에 실패했습니다.')


def compact(base):
    base=Path(base).resolve(strict=True)
    packages=Path(sysconfig.get_path('purelib')).resolve(strict=True)
    if not packages.is_relative_to(base) or packages==base:
        raise ValueError('앱 폴더 밖의 Python 환경은 정리하지 않습니다.')
    library=(packages/'torch/lib').resolve(strict=True)
    if not library.is_relative_to(packages):
        raise ValueError('Torch 폴더 연결 경로가 앱 범위 밖입니다.')
    paths=[]
    # Validate every target before removing any file. DLLs and own import
    # libraries are never candidates; no recursive directory deletion.
    for name in BUILD_ARCHIVES:
        path=library/name
        if path.is_symlink():raise ValueError('빌드 파일 연결 경로는 정리하지 않습니다.')
        if not path.exists():continue
        if path.resolve().parent!=library or not path.is_file():
            raise ValueError('빌드 파일 경로가 올바르지 않습니다.')
        with path.open('rb') as stream:header=stream.read(8)
        if header!=b'!<arch>\n':raise ValueError('예상한 빌드 파일 형식이 아니므로 정리하지 않았습니다.')
        paths.append(path)
    check_runtime(packages)
    removed=sum(path.stat().st_size for path in paths)
    for path in paths:path.unlink()
    return {'files':len(paths),'removed_bytes':removed}


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('앱 설치 폴더를 지정하세요.')
    from analysis_limits import apply_analysis_limits
    apply_analysis_limits(cpu_percent=2,memory_mb=1024)
    result=compact(sys.argv[1])
    print(f"실행에 필요 없는 빌드 파일 {result['files']}개 정리 · {result['removed_bytes']/1e9:.2f}GB 절약",flush=True)
