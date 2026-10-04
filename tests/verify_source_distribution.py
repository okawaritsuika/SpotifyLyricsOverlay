"""Check a real source archive for completeness, syntax, and private-data exclusion."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def verify(path):
    prefix = 'SpotifyLyricsOverlay/'
    local_modules = {item.stem for item in ROOT.glob('*.py')}
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert all(name.startswith(prefix) and '..' not in Path(name).parts for name in names)
        relative = {name[len(prefix):] for name in names}
        assert not any(Path(name).parts[0] in ('data', 'models', '.venv', '.exe-venv', '.codex', 'output') for name in relative)
        import sys
        sys.path.insert(0, str(ROOT))
        from package_release import release_paths
        assert relative == {item.relative_to(ROOT).as_posix() for item in release_paths(ROOT)}
        python_count = json_count = 0
        for name in relative:
            data = archive.read(prefix + name)
            assert (ROOT / name).read_bytes() == data, f'Archive differs from source: {name}'
            if name.endswith('.py'):
                python_count += 1
                tree = ast.parse(data, filename=name)
                compile(tree, name, 'exec')
                for node in ast.walk(tree):
                    imports = [item.name for item in node.names] if isinstance(node, ast.Import) else (
                        [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
                    for module in imports:
                        top = module.split('.')[0]
                        if top in local_modules:
                            assert top + '.py' in relative, f'Missing runtime module {top} imported by {name}'
            elif name.endswith('.json'):
                json_count += 1
                json.loads(data)
    return {'archive': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'files': len(names), 'python_modules_compiled': python_count, 'json_resources_parsed': json_count,
            'project_imports_present': True, 'matches_workspace_sources': True, 'personal_data_excluded': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = verify(args.archive)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
