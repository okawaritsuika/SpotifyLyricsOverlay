"""Build a Windows EXE from public allowlisted files, without optional AI models."""
import argparse
from importlib import metadata
from pathlib import Path
import re
import shutil
import sys
from zipfile import ZIP_DEFLATED, ZipFile


def collect_notices(destination):
    destination.mkdir(parents=True, exist_ok=True)
    packages=[]
    for distribution in sorted(metadata.distributions(), key=lambda item: item.metadata['Name'].lower()):
        name=distribution.metadata['Name']
        if name.lower() in ('pip','setuptools','wheel','pyinstaller-hooks-contrib','altgraph','pefile','pywin32-ctypes'):continue
        packages.append(f'{name} {distribution.version}\nLicense: {distribution.metadata.get("License-Expression") or distribution.metadata.get("License", "See package notices")}\n')
        for file in distribution.files or []:
            filename=Path(file).name.lower()
            if (any(word in filename for word in ('license','licence','copying','notice'))
                    or filename.split('.')[0] in ('bsd','gpl','lgpl','mit','apache')):
                source=Path(distribution.locate_file(file))
                if source.is_file():
                    target=destination/name/str(file).replace('..','_').replace(':','_')
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source,target)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.is_file():shutil.copyfile(python_license,destination/'Python-LICENSE.txt')
    for path in (Path(sys.base_prefix)/'tcl').rglob('license.terms'):
        target=destination/'Tcl-Tk'/path.relative_to(Path(sys.base_prefix)/'tcl')
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    license_directory=Path(__file__).resolve().parent/'resources/licenses'
    for path in license_directory.glob('*.txt'):shutil.copyfile(path,destination/path.name)
    (destination/'PACKAGES.txt').write_text('\n'.join(packages),encoding='utf-8')


def build(version, output_dir):
    if sys.platform != 'win32':raise ValueError('Windows에서 빌드하세요.')
    if sys.maxsize <= 2**32:raise ValueError('64비트 Python으로 빌드하세요.')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*',version):raise ValueError('버전 형식을 확인하세요.')
    from app_version import VERSION
    if version != VERSION:raise ValueError('빌드 버전과 app_version.VERSION이 일치해야 합니다.')
    base=Path(__file__).resolve().parent
    from package_release import release_paths, MODULES
    paths=release_paths(base)
    if not (base/'resources/seed_data/_manifest.json').is_file():raise ValueError('공개 데이터 내보내기를 먼저 실행하세요: python public_data.py')
    output_dir=Path(output_dir).resolve();output_dir.mkdir(parents=True,exist_ok=True)
    name='SpotifyLyricsOverlay'
    notices=output_dir/'THIRD_PARTY_LICENSES'
    collect_notices(notices)
    arguments=[str(base/'frozen_entry.py'),'--name',name,'--onefile','--windowed','--noconfirm',
               '--distpath',str(output_dir),'--workpath',str(base/'build/pyinstaller'),
               '--specpath',str(base/'build'),'--noupx',
               '--add-data',str(base/'LICENSE')+';.',
               '--add-data',str(notices)+';THIRD_PARTY_LICENSES']
    for path in paths:
        relative=path.relative_to(base)
        if relative.parts[0] in ('resources','extension') or (len(relative.parts)==1 and
                (path.suffix=='.py' or path.name in ('native_page.js','requirements-audio.txt','requirements-browser.txt'))):
            arguments += ['--add-data',str(path)+';'+str(relative.parent)]
    for module in list(MODULES)+['app_paths','optional_runtime','spotify_controls']:
        arguments += ['--hidden-import',module]
    for package in ('pykakasi','unidic_lite','fugashi','uiautomation','winrt'):
        arguments += ['--collect-all',package]
    for package in ('torch','torchaudio','faster_whisper','stable_whisper','whisper','numpy','ctranslate2','webview','clr','pythonnet','PyQt5','PyQt6','PySide6','pytest'):
        arguments += ['--exclude-module',package]
    import PyInstaller.__main__
    PyInstaller.__main__.run(arguments)
    executable=output_dir/(name+'.exe')
    archive=output_dir/f'{name}-{version}-windows-x64.zip'
    with ZipFile(archive,'w',ZIP_DEFLATED) as bundle:
        bundle.write(executable,executable.name)
        for path in sorted(notices.rglob('*')):
            if path.is_file():bundle.write(path,path.relative_to(output_dir).as_posix())
        for document in ('README.md','LICENSE','DISTRIBUTION.md','RELEASE_NOTES.md'):
            if (base/document).is_file():bundle.write(base/document,document)
    print(executable)
    print(archive)
    from package_release import package_data
    print(package_data(base,output_dir))
    print('EXE bytes:',executable.stat().st_size)
    return executable


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',default='0.1.0')
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent/'release')
    args=parser.parse_args()
    build(args.version,args.output_dir)
