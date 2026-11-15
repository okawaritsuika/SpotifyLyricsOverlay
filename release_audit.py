"""Fail closed on private paths and credential-shaped values before packaging."""
import argparse
import json
from pathlib import Path
import re


PRIVATE_PARTS = frozenset({
    'data', 'models', 'output', 'release', '.git', '.venv', '.exe-venv',
    '.build-venv', '.browser_deps', '__pycache__', 'browser_profile',
    'spotify_profile', 'diagnostics', 'source_backups',
})
PRIVATE_NAMES = frozenset({'settings.json', '.env', 'verification.md', 'design.md'})
TOKEN_PATTERNS = (
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(r'\bsk-proj-[A-Za-z0-9_-]{35,}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
)
TEXT_SUFFIXES = {'.py', '.json', '.js', '.cjs', '.md', '.txt', '.yml', '.yaml',
                 '.ps1', '.bat', '.html', '.css', '.toml'}


def _public_song_links(base, path):
    """Only reconstructed lyric-edit ownership may seed first-run settings."""
    if path.resolve() != base/'resources/seed_data/settings.json':return False
    try:
        from public_data import _text
        value=json.loads(path.read_text(encoding='utf-8'))
        library=json.loads((path.parent/'library.json').read_text(encoding='utf-8'))
        identities={song['id'] for song in library}
        if not isinstance(value,dict) or set(value)!={'lyric_sources','links'}:return False
        sources,links=value['lyric_sources'],value['links']
        if not isinstance(sources,dict) or not sources or not isinstance(links,dict):return False
        for identity,metadata in sources.items():
            if (identity not in identities or not isinstance(metadata,dict)
                    or set(metadata)!={'user_override'} or metadata['user_override'] is not True):return False
        for key,identity in links.items():
            if not isinstance(key,str) or identity not in sources:return False
            parts=key.split('\0')
            if len(parts)!=3 or not parts[0].strip():return False
            if any(len(part)>4000 for part in parts):return False
            for part in parts:_text(part)
        return True
    except (OSError,ValueError,TypeError,KeyError):
        return False


def audit_paths(base, paths):
    base = Path(base).resolve()
    checked = 0
    errors = []
    # A contributor's actual home path must not escape through docs or fixtures.
    home = str(Path.home())
    home_variants = {home, home.replace('\\', '/'), home.replace('\\', '\\\\')}
    for path in paths:
        path = Path(path)
        try:
            relative = path.resolve().relative_to(base)
        except ValueError:
            errors.append('A release file resolves outside the source tree')
            continue
        name = relative.as_posix()
        if (any(part.lower() in PRIVATE_PARTS for part in relative.parts)
                or path.name.lower() in PRIVATE_NAMES and not _public_song_links(base,path)
                or path.name.startswith('.env')
                or path.suffix.lower() in {'.log', '.wav', '.mp3', '.flac', '.db', '.sqlite'}):
            errors.append(f'{name}: private/runtime file')
            continue
        if path.stat().st_size >= 100 * 1024 * 1024:
            errors.append(f'{name}: exceeds source repository file limit')
        if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith('.'):
            content = path.read_text(encoding='utf-8-sig')
            if any(pattern.search(content) for pattern in TOKEN_PATTERNS):
                errors.append(f'{name}: credential-shaped value (value hidden)')
            if any(value and value in content for value in home_variants):
                errors.append(f'{name}: local home path (value hidden)')
        checked += 1
    if errors:
        raise ValueError('Release privacy audit failed:\n' + '\n'.join(errors))
    return checked


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    from package_release import release_paths
    paths = release_paths(args.directory)
    print(f'Release allowlist and credential/path audit passed: {len(paths)} files')
