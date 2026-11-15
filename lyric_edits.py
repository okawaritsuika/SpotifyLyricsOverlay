"""Explicit library corrections with a durable backup and local write rollback."""
import base64
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
import uuid

from live_timing import LiveTimingStore
from storage import atomic_json


def _song_files(directory, song):
    if not song.id or song.id in ('.', '..') or any(char in song.id for char in '/\\:'):
        raise ValueError('저장된 곡 식별자를 확인할 수 없습니다.')
    return [directory / folder / (song.id + '.json')
            for folder in ('timings', 'lyric_annotations', 'audio_drafts', 'audio_backups')]


def _contents(path):
    try:return path.read_bytes()
    except FileNotFoundError:return None


def _restore(path, body):
    if body is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:temporary.unlink(missing_ok=True)


def save_lyric_edit(store, songs, settings, target, incoming=None, source=None, playback_key=None,
                    *, row_timing=None, row_annotations=None):
    """Save a user-confirmed replacement/removal; callers publish state on success.

    A replacement keeps the saved song's identity and metadata. Only an exact,
    ordered original-text match permits its clock to survive annotation edits.
    """
    if target is None and incoming is None:raise ValueError('수정할 가사를 선택하세요.')
    temporary_target = bool(target and target.id.startswith(('memory-', 'completed-'))
                            and not any(song.id == target.id for song in songs))
    if target is not None and not temporary_target and not any(song == target for song in songs):
        raise ValueError('수정할 가사가 라이브러리에서 바뀌었습니다. 다시 선택하세요.')
    if target is None and any(song.id == incoming.id for song in songs):
        raise ValueError('이미 저장된 곡입니다. 덮어쓸 가사를 선택하세요.')
    saved = replace(target, lines=tuple(incoming.lines)) if target and incoming else incoming
    if saved is not None and not saved.lines:raise ValueError('저장할 가사가 비어 있습니다.')
    identity = target or saved
    files = _song_files(store.directory, identity)
    timing_path, annotation_path = files[:2]
    live_files = []
    if temporary_target:
        live_path = LiveTimingStore(store.directory).path(target)
        live_files = [live_path, store.directory / 'live_audio_drafts' / live_path.name,
                      store.directory / 'live_audio_backups' / live_path.name]
    paths = [store.directory / 'library.json', store.directory / 'settings.json', *files,
             store.directory / 'audio_backups' / (identity.id + '.before-public.json'), *live_files]
    paths.extend(row_annotations or {})
    before = {path: _contents(path) for path in paths}
    timing = None
    if saved is not None and target is not None:
        same = tuple(line.original for line in target.lines) == tuple(line.original for line in saved.lines)
        if same:
            timing = LiveTimingStore(store.directory).load(target) if temporary_target else store.timing(target)
        if timing is None:timing = {'times': [None] * len(saved.lines), 'offset': 0., 'source': ''}

    updated = [saved if target and song.id == target.id else song for song in songs]
    updated = [song for song in updated if song is not None]
    if saved is not None and (target is None or temporary_target):updated.append(saved)
    changed = deepcopy(settings)
    links = changed.setdefault('links', {})
    affected = {'\0'.join((identity.title, identity.artist, identity.album))}
    if target is not None:affected.update(key for key, value in links.items() if value == target.id)
    if playback_key:affected.add(playback_key)
    deleted = set(changed.get('deleted_lyrics', []))
    sources = changed.setdefault('lyric_sources', {})
    if saved is None:
        deleted.update(affected)
        sources.pop(target.id, None)
        changed['links'] = {key: value for key, value in links.items() if value != target.id}
    else:
        deleted.difference_update(affected)
        sources[saved.id] = {**(source or {}), 'user_override': True}
        if playback_key:links[playback_key] = saved.id
    changed['deleted_lyrics'] = sorted(deleted)

    backup = store.directory / 'lyric_edit_backups' / (uuid.uuid4().hex + '.json')
    atomic_json(backup, {'schema_version': 1, 'saved_at': time.time(),
                        'operation': 'delete' if saved is None else 'replace' if target else 'add',
                        'song': target.to_dict() if target else None,
                        'files': {str(path.relative_to(store.directory)).replace('\\', '/'):
                                  base64.b64encode(body).decode('ascii') if body is not None else None
                                  for path, body in before.items()}})
    touched = []
    try:
        if target is not None or row_timing is not None:
            touched.append(timing_path)
            if saved is None:timing_path.unlink(missing_ok=True)
            else:store.save_timing(saved, row_timing if row_timing is not None else timing)
        if target is not None:
            touched.append(annotation_path)
            annotation_path.unlink(missing_ok=True)
            if saved is None:
                for path in live_files[:2]:
                    touched.append(path)
                    path.unlink(missing_ok=True)
        for path, rows in (row_annotations or {}).items():
            touched.append(path)
            atomic_json(path, rows)
        touched.append(store.directory / 'library.json')
        store.save_library(updated)
        touched.append(store.directory / 'settings.json')
        store.save_settings(changed)
    except Exception as error:
        failures = []
        for path in reversed(touched):
            try:
                if _contents(path) != before[path]:_restore(path, before[path])
            except OSError:failures.append(path.name)
        if failures:
            raise OSError(f'가사 수정 실패 후 일부 파일을 복구하지 못했습니다. 백업: {backup}') from error
        raise
    return updated, changed, saved, backup
