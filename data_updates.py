"""Validated public data updates, staged until the next exclusive app startup.

Existing text and clocks always win. A durable pending marker is removed only
after every atomic file write and the receipt succeed, so interrupted updates
can be replayed without replacing user work.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from zipfile import BadZipFile, ZipFile

import public_data as public
from lyrics import Song
from storage import Store, atomic_json, validate_times


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_FILES = 12000
MAX_RATIO = 200
PENDING = '.pending-data-update.json'
RECEIPTS = '.data-update-receipts.json'
STAGING = '.data-updates'
ROOT_FILES = {'_manifest.json', 'library.json', 'settings.json', 'artist_metadata.json',
              'explorer_library.json', 'spotify_public_tracks.json', 'wiki_documents.json',
              'wiki_lyric_parts.json', 'lyric_parts.json', 'ai_translations/imports.json',
              'playlist_lyrics/index.json'}
FOLDERS = {'timings', 'live_timings', 'native_timings', 'lyrics_cache', 'lyric_annotations',
           'ai_translations/history', 'playlist_lyrics/tracks', 'user_lyrics', 'manual_annotations'}
MANIFEST_KEYS = {'schema_version', 'files', 'bytes', 'categories', 'library_songs',
                 'playlist_tracks', 'translation_documents', 'translated_rows', 'public_tracks',
                 'recording_links', 'user_lyric_overrides', 'privacy', 'rights'}


def _safe(path):
    public._safe_path(path)


def _root(directory):
    path = Path(directory).absolute()
    _safe(path)
    return path.resolve()


def _name(name):
    if (not isinstance(name, str) or '\\' in name or ':' in name or '\0' in name
            or any(part in ('', '.', '..') for part in name.split('/'))
            or PurePosixPath(name).is_absolute()):
        raise ValueError('Unsafe update archive path')
    if name in ROOT_FILES:return name
    folder, _, filename = name.rpartition('/')
    if folder not in FOLDERS or not filename.endswith('.json'):
        raise ValueError('Unknown public data file')
    pattern = public.TRACK_ID if folder == 'playlist_lyrics/tracks' else public.SONG_ID if folder in ('timings', 'lyric_annotations') else public.HEX
    public._identifier(filename[:-5], pattern)
    return name


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:raise ValueError('Duplicate JSON key in update')
        result[key] = value
    return result


def _json(body):
    return json.loads(body, object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite update number')))


def _require_equal(value, cleaned):
    if value != cleaned:raise ValueError('Unknown or invalid public data fields')


def _imports(value):
    result = {}
    for identifier, rows in public._object(value['documents']).items():
        public._identifier(identifier)
        result[identifier] = {}
        for index, text in public._object(rows).items():
            if (not isinstance(index, str) or not index.isascii() or not index.isdigit()
                    or not 1 <= int(index) <= 1500 or str(int(index)) != index):
                raise ValueError('Invalid imported translation row')
            result[identifier][index] = public._text(text)
    return {'version': 1, 'documents': result}


def _validate_documents(documents, sizes):
    manifest = documents.get('_manifest.json')
    if (not isinstance(manifest, dict) or set(manifest) - MANIFEST_KEYS
            or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or type(manifest.get('files')) is not int or manifest['files'] != len(documents) - 1
            or type(manifest.get('bytes')) is not int
            or manifest['bytes'] != sum(size for name, size in sizes.items() if name != '_manifest.json')):
        raise ValueError('Invalid public data manifest')
    library = documents.get('library.json', [])
    if not isinstance(library, list):raise ValueError('Invalid library update')
    songs = {row['id']: row for row in library}
    if len(songs) != len(library):raise ValueError('Duplicate public song')
    for name, value in documents.items():
        if name == '_manifest.json':continue
        folder, _, filename = name.rpartition('/')
        for version in ('version', 'schema_version'):
            if isinstance(value, dict) and version in value and (type(value[version]) is not int or value[version] != 1):
                raise ValueError('Unsupported public data version')
        if name == 'library.json':clean = [public._song(row) for row in value]
        elif name == 'settings.json':clean = public._edited_settings(value, library)
        elif name == 'ai_translations/imports.json':clean = _imports(value)
        elif name == 'artist_metadata.json':
            if len(value['entries']) > 512:raise ValueError('Artist metadata update too large')
            clean = {'version': 1, 'entries': [
                {'request': {**public._fields(row['request'], ('title', 'artist', 'album')),
                             'duration': public._number(row['request']['duration'])},
                 'track': public._track(row['track'])} for row in value['entries']]}
        elif name == 'spotify_public_tracks.json':
            clean = {'schema_version': 1, 'tracks': [{**public._track(row),
                     'source_url': 'https://open.spotify.com/track/' + row['id']} for row in value['tracks']]}
        elif name == 'explorer_library.json':
            clean = {'schema_version': 1, 'spotify': {public._identifier(key, public.SONG_ID): {
                **public._track(row), 'url': 'https://open.spotify.com/track/' + row['id']}
                for key, row in value['spotify'].items()}}
        elif name in ('wiki_lyric_parts.json', 'lyric_parts.json'):clean = public._parts(value)
        elif name == 'wiki_documents.json':
            clean = {public._identifier(key): {'url': public._url(item['url']), 'learned': True}
                     for key, item in value.items()}
            if any(not item['url'] for item in clean.values()):raise ValueError('Invalid wiki URL')
        elif name == 'playlist_lyrics/index.json':
            clean = {'schema_version': 1, 'tracks': {public._identifier(key, public.TRACK_ID): public._track(row)
                     for key, row in value['tracks'].items()}}
        elif folder in ('timings', 'live_timings', 'native_timings'):
            clean = public._timing(value)
            if not 0 < len(clean['times']) <= 2000:raise ValueError('Invalid timing row count')
            identity = 'fingerprint' if folder == 'timings' else 'signature' if folder == 'live_timings' else 'duration'
            if identity not in clean:raise ValueError('Missing timing identity')
            if folder == 'timings' and filename[:-5] in songs:
                if clean['fingerprint'] != Song.from_dict(songs[filename[:-5]]).fingerprint:
                    raise ValueError('Timing does not match the packaged song')
        elif folder == 'lyrics_cache':
            clean = public._cache(value)
            if hashlib.sha256(clean['key'].encode()).hexdigest() + '.json' != filename:
                raise ValueError('Invalid cache identity')
            from lyric_cache import _provider
            _provider(clean.get('direct'), clean['duration'], decoded=True)
        elif folder == 'ai_translations/history':
            clean = public._history(value)
            if clean['id'] + '.json' != filename:raise ValueError('Invalid translation identity')
        elif folder == 'playlist_lyrics/tracks':
            clean = public._playlist(value)
            if clean is None or clean['track']['id'] + '.json' != filename:raise ValueError('Invalid playlist lyric identity')
            from playlist_lyrics_store import _direct
            _direct(clean.get('direct'), clean['track'], decoded=True)
        elif folder == 'user_lyrics':clean = public._user_lyrics(value)
        elif folder == 'manual_annotations':
            clean = {**{key: public._text(value[key]) for key in ('title', 'artist') if key in value},
                     'lines': public._rows(value['lines'])}
        elif folder == 'lyric_annotations':
            clean = {'fingerprint': public._identifier(value['fingerprint']),
                     'lines': [None if row is None else public._fields(row, ('original', 'reading', 'korean'))
                               for row in value['lines']]}
        else:raise ValueError('Unknown public data schema')
        _require_equal(value, clean)
    tracks = {value['track']['id']: value['track'] for name, value in documents.items()
              if name.startswith('playlist_lyrics/tracks/')}
    index = documents.get('playlist_lyrics/index.json', {}).get('tracks', {})
    if tracks != index:raise ValueError('Playlist index and lyric files do not match')


def _package(path):
    path = Path(path)
    _safe(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:raise ValueError('Data archive is too large')
    with path.open('rb') as stream:body = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(body) > MAX_ARCHIVE_BYTES:raise ValueError('Data archive is too large')
    documents, sizes, seen = {}, {}, set()
    try:
        with ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:raise ValueError('Too many update files')
            total = 0
            for entry in entries:
                name = _name(entry.filename)
                mode = entry.external_attr >> 16
                if (entry.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or entry.flag_bits & 1 or name.casefold() in seen):
                    raise ValueError('Linked, encrypted or duplicate update file')
                seen.add(name.casefold())
                total += entry.file_size
                if (entry.file_size > MAX_ENTRY_BYTES or total > MAX_TOTAL_BYTES
                        or entry.file_size > max(1, entry.compress_size) * MAX_RATIO):
                    raise ValueError('Unsafe data archive expansion')
                raw = archive.read(entry)
                if len(raw) != entry.file_size:raise ValueError('Truncated data update')
                documents[name] = _json(raw)
                sizes[name] = len(raw)
        _validate_documents(documents, sizes)
    except (BadZipFile, KeyError, TypeError, AttributeError, UnicodeError, RecursionError,
            OverflowError, RuntimeError, NotImplementedError) as error:
        raise ValueError('Invalid public data update') from error
    return documents, body


def validate_package(zip_path):
    """Return validated JSON documents; never extract or change any files."""
    return _package(zip_path)[0]


def _read(path, default=None):
    _safe(path)
    try:
        if path.stat().st_size > MAX_ENTRY_BYTES:raise ValueError('Local data file is too large')
        return _json(path.read_bytes())
    except FileNotFoundError:return default


def _receipts(root):
    value = _read(root / RECEIPTS, {'version': 1, 'applied': {}})
    if (not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('applied'), dict)
            or any(not isinstance(key, str) or not public.HEX.fullmatch(key) for key in value['applied'])):
        raise ValueError('Invalid data update receipt')
    counts = ('added_files', 'updated_files', 'deferred_files', 'unchanged_files')
    for digest, result in value['applied'].items():
        if (not isinstance(result, dict) or set(result) != {'status', 'pack_hash', *counts}
                or result.get('status') != 'applied' or result.get('pack_hash') != digest
                or any(type(result.get(key)) is not int or result[key] < 0 for key in counts)):
            raise ValueError('Invalid data update receipt entry')
    return value


def _atomic_bytes(path, body):
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def stage_package(zip_path, data_dir):
    """Validate and queue an immutable package without touching live music data."""
    root = _root(data_dir)
    documents, body = _package(zip_path)
    digest = hashlib.sha256(body).hexdigest()
    result = {'status': 'staged', 'pack_hash': digest, 'files': len(documents) - 1,
              'bytes': documents['_manifest.json']['bytes']}
    if digest in _receipts(root)['applied']:return {**result, 'status': 'already_applied'}
    destination = root / STAGING / (digest + '.zip')
    _atomic_bytes(destination, body)
    _safe(root / PENDING)
    atomic_json(root / PENDING, {'version': 1, 'pack_hash': digest})
    return result


def _same_rows(left, right):
    return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
            and all(isinstance(a, dict) and isinstance(b, dict) and a.get('original') == b.get('original')
                    for a, b in zip(left, right)))


def _rows(left, right):
    if not _same_rows(left, right):return None
    result = deepcopy(left)
    for row, incoming in zip(result, right):
        for key in ('reading', 'korean'):
            existing = row.get(key, '')
            if not isinstance(existing, str):return None
            if not existing.strip() and incoming.get(key, '').strip():row[key] = incoming[key]
    return result


def _timing(left, right):
    for key in ('fingerprint', 'signature', 'duration'):
        if key in left or key in right:
            if left.get(key) != right.get(key):return None
    if left.get('offset', 0) != right.get('offset', 0):return None
    old, incoming = left.get('times'), right.get('times')
    if not isinstance(old, list) or not isinstance(incoming, list) or len(old) != len(incoming):return None
    try:validate_times(old, len(old))
    except ValueError:return None
    if any(a is not None and b is not None and a != b for a, b in zip(old, incoming)):return None
    times = [b if a is None else a for a, b in zip(old, incoming)]
    try:validate_times(times, len(times))
    except ValueError:return None
    result = deepcopy(left)
    if times == old:return result
    result['times'] = times
    if 'origins' in left or 'origins' in right:
        from timing_quality import timing_origins
        old_origins, new_origins = timing_origins(left), timing_origins(right)
        result['origins'] = [new_origins[i] if old[i] is None and times[i] is not None else old_origins[i]
                             for i in range(len(times))]
    return result


def _dictionary(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):return None
    return {**deepcopy(right), **deepcopy(left)}


def _list(left, right, key, limit=None):
    if not isinstance(left, list) or not isinstance(right, list):return None
    result = deepcopy(left)
    try:known = {key(item) for item in left}
    except (KeyError, TypeError):return None
    for item in right:
        identity = key(item)
        if identity in known:continue
        if limit is not None and len(result) >= limit:break
        result.append(deepcopy(item));known.add(identity)
    return result


def _protected_keys(settings, library):
    sources = settings.get('lyric_sources', {})
    protected = {identifier for identifier, value in sources.items()
                 if isinstance(value, dict) and value.get('user_override') is True}
    keys = {key for key, identifier in settings.get('links', {}).items() if identifier in protected}
    keys.update('\0'.join(row[name] for name in ('title', 'artist', 'album'))
                for row in library if row['id'] in protected)
    return protected, keys


def _recording_key(value):
    if 'key' in value:return value['key']
    if 'track' in value:
        track = value['track']
        return '\0'.join((track['title'], track['artists'][0], track['album']))
    if all(key in value for key in ('title', 'artist', 'album')):
        return '\0'.join(value[key] for key in ('title', 'artist', 'album'))
    return None


def _clock_authorities(root, documents, library, settings):
    """Map all known clock namespaces to exact recording identities.

    Unknown hashes can defer unidentifiable incoming clocks, while recordings
    with a proven identity remain independent of unrelated local clocks.
    """
    from live_timing import original_signature
    paths, authorities, unknown = {}, {}, set()
    candidates = list(documents.values())
    current = {}
    for folder in ('lyrics_cache', 'playlist_lyrics/tracks'):
        _safe(root / folder)
        for path in (root / folder).glob('*.json'):
            value = _read(path)
            if isinstance(value, dict):current[path.relative_to(root).as_posix()] = value
    candidates.extend(current.values())
    candidates.extend(library)
    candidates.extend(documents.get('library.json', []))
    aliases = {}
    for key, identity in settings.get('links', {}).items():aliases.setdefault(identity, set()).add(key)
    seen = set()
    for value in candidates:
        if not isinstance(value, dict) or not isinstance(value.get('lines'), list):continue
        key = _recording_key(value)
        if not isinstance(key, str) or len(key.split('\0')) != 3:continue
        title, artist, album = key.split('\0')
        identities = ['memory-' + hashlib.sha256(key.encode()).hexdigest()[:24],
                      'completed-' + hashlib.sha256(key.encode()).hexdigest()[:24]]
        if value in library or value in documents.get('library.json', []):identities.append(value['id'])
        for identity in identities:
            song = Song.from_dict({'id': identity, 'title': title, 'artist': artist, 'album': album, 'lines': value['lines']})
            marker = (identity, key, song.fingerprint)
            if marker in seen:continue
            seen.add(marker)
            keys = {key, *aliases.get(identity, ())}
            mapped = ['timings/' + identity + '.json',
                      'live_timings/' + hashlib.sha256(identity.encode()).hexdigest() + '.json',
                      'native_timings/' + hashlib.sha256((identity + original_signature(song)).encode()).hexdigest() + '.json',
                      'native_timings/' + hashlib.sha256((identity + song.fingerprint).encode()).hexdigest() + '.json']
            for name in mapped:paths.setdefault(name, set()).update(keys)
    def nonempty(value):
        return isinstance(value, dict) and (any(stamp is not None for stamp in value.get('times', []))
                                            or value.get('offset', 0) != 0 or nonempty(value.get('fallback')))
    for name, value in current.items():
        key = _recording_key(value)
        if nonempty(value.get('direct')):
            if key is None:unknown.add(name)
            else:authorities.setdefault(key, set()).add(name)
    for folder in ('timings', 'live_timings', 'native_timings'):
        _safe(root / folder)
        for path in (root / folder).glob('*.json'):
            value = _read(path)
            if not nonempty(value):continue
            name = path.relative_to(root).as_posix()
            if name not in paths:unknown.add(name)
            for key in paths.get(name, ()):authorities.setdefault(key, set()).add(name)
    return paths, authorities, unknown


def _merge_document(name, left, right, protected_keys):
    folder = name.rpartition('/')[0]
    if name == 'settings.json':return deepcopy(left)
    if folder in ('timings', 'live_timings', 'native_timings'):return _timing(left, right)
    if folder in ('lyrics_cache', 'ai_translations/history', 'playlist_lyrics/tracks', 'user_lyrics', 'manual_annotations'):
        if _recording_key(right) in protected_keys:return deepcopy(left)
        identities = ('key', 'duration') if folder == 'lyrics_cache' else ('id', 'title', 'artist', 'album') if folder == 'ai_translations/history' else ('track',) if folder == 'playlist_lyrics/tracks' else ('title', 'artists', 'artist')
        if any(left.get(key) != right.get(key) for key in identities):return None
        rows = _rows(left.get('lines'), right.get('lines'))
        if rows is None:return None
        result = deepcopy(left);result['lines'] = rows
        # Provider clocks never replace any existing direct clock. A missing
        # provider may be adopted only against the exact current original rows.
        if folder in ('lyrics_cache', 'playlist_lyrics/tracks'):
            direct = right.get('direct')
            if left.get('direct') is None and direct and _same_rows(left['lines'], direct['lines']):result['direct'] = deepcopy(direct)
            elif isinstance(left.get('direct'), dict) and isinstance(direct, dict):
                previous = left['direct']
                if (all(previous.get(key) == direct.get(key) for key in ('source', 'provider_id'))
                        and _same_rows(previous.get('lines'), direct.get('lines'))):
                    merged_clock = _timing(previous, direct)
                    if merged_clock is not None:result['direct'] = merged_clock
        return result
    if folder == 'lyric_annotations':
        if left.get('fingerprint') != right.get('fingerprint') or len(left.get('lines', [])) != len(right['lines']):return None
        rows = []
        for a, b in zip(left['lines'], right['lines']):
            if a is None:rows.append(deepcopy(b))
            elif b is None:rows.append(deepcopy(a))
            else:
                merged = _rows([a], [b])
                if merged is None:return None
                rows.append(merged[0])
        return {**deepcopy(left), 'lines': rows}
    if name == 'ai_translations/imports.json':
        if not isinstance(left.get('documents'), dict):return None
        result = deepcopy(left)
        for identifier, rows in right['documents'].items():
            current = result['documents'].setdefault(identifier, {})
            if not isinstance(current, dict):return None
            for index, text in rows.items():
                if index not in current or isinstance(current[index], str) and not current[index].strip():current[index] = text
        return result
    maps = {'playlist_lyrics/index.json': 'tracks', 'explorer_library.json': 'spotify'}
    if name in maps:
        field = maps[name];merged = _dictionary(left.get(field), right[field])
        return None if merged is None else {**deepcopy(left), field: merged}
    if name == 'wiki_documents.json':return _dictionary(left, right)
    fields = {'spotify_public_tracks.json': ('tracks', lambda item: item['id'], 50000),
              'artist_metadata.json': ('entries', lambda item: tuple(item['request'][key] for key in ('title', 'artist', 'album', 'duration')), 512),
              'wiki_lyric_parts.json': ('recordings', lambda item: (item['title'], tuple(item['artists']), item['album'], item['text_sha256']), 5000),
              'lyric_parts.json': ('recordings', lambda item: (item['title'], tuple(item['artists']), item['album'], item['text_sha256']), 5000)}
    if name in fields:
        field, key, limit = fields[name];merged = _list(left.get(field), right[field], key, limit)
        return None if merged is None else {**deepcopy(left), field: merged}
    return None


def _plan(root, documents):
    changes, deferred, unchanged, added = {}, set(), set(), set()
    existing_library = _read(root / 'library.json', [])
    settings = _read(root / 'settings.json', {})
    if not isinstance(existing_library, list) or not isinstance(settings, dict):raise ValueError('Invalid current library/settings')
    Store(root).library()
    protected, protected_keys = _protected_keys(settings, existing_library)
    deleted = settings.get('deleted_lyrics', [])
    if not isinstance(deleted, list) or any(not isinstance(key, str) for key in deleted):raise ValueError('Invalid lyric deletion preferences')
    deleted = set(deleted)
    blocked_ids = {row['id'] for row in documents.get('library.json', [])
                   if row['id'] in deleted or _recording_key(row) in deleted}
    deleted_histories = {value['id'] for name, value in documents.items()
                         if name.startswith('ai_translations/history/') and _recording_key(value) in deleted}
    incoming_library = [row for row in documents.get('library.json', []) if row['id'] not in blocked_ids]
    if blocked_ids:deferred.add('library.json')
    merged_library = _list(existing_library, incoming_library, lambda song: song['id'])
    if merged_library is None:raise ValueError('Invalid current library')
    songs = {row['id']: row for row in merged_library}
    existing_songs = {row['id']: row for row in existing_library}
    from live_timing import original_signature
    native_aliases = {}
    for row in merged_library:
        song = Song.from_dict(row)
        modern = hashlib.sha256((song.id + original_signature(song)).encode()).hexdigest() + '.json'
        legacy = hashlib.sha256((song.id + song.fingerprint).encode()).hexdigest() + '.json'
        native_aliases[modern] = legacy
    native_directory = root / 'native_timings'
    _safe(native_directory)
    has_native = any(native_directory.glob('*.json'))
    clock_paths, clock_authorities, unknown_clocks = _clock_authorities(root, documents, existing_library, settings)

    def other_clock(name, value, exceptions=()):
        keys = set(clock_paths.get(name, ()))
        if key := _recording_key(value):keys.add(key)
        if not keys and unknown_clocks:return True
        clocks = set()
        for key in keys:clocks.update(clock_authorities.get(key, ()))
        return bool(clocks - {name, *exceptions})

    def propose(name, incoming):
        path = root / name
        current = changes.get(name, _read(path))
        if current is None:
            changes[name] = deepcopy(incoming);added.add(name)
        else:
            try:merged = _merge_document(name, current, incoming, protected_keys)
            except (KeyError, TypeError, ValueError):merged = None
            if merged is None:deferred.add(name)
            elif merged != current:changes[name] = merged
            elif name not in changes:unchanged.add(name)

    if 'library.json' in documents:
        if merged_library != existing_library:
            changes['library.json'] = merged_library
            if not (root / 'library.json').exists():added.add('library.json')
        else:unchanged.add('library.json')
    for name, value in sorted(documents.items()):
        if name in ('_manifest.json', 'library.json'):continue
        if name.startswith(('timings/', 'lyric_annotations/')) and Path(name).stem in blocked_ids:
            deferred.add(name);continue
        if isinstance(value, dict) and _recording_key(value) in deleted:
            deferred.add(name);continue
        if name == 'ai_translations/imports.json' and deleted_histories:
            value = {**value, 'documents': {key: rows for key, rows in value['documents'].items() if key not in deleted_histories}}
        if name == 'playlist_lyrics/index.json':
            value = {**value, 'tracks': {key: track for key, track in value['tracks'].items()
                                       if _recording_key({'track': track}) not in deleted}}
        if name == 'settings.json':
            if (root / name).exists():unchanged.add(name);continue
            sources = {key: metadata for key, metadata in value['lyric_sources'].items()
                       if songs.get(key) == next((row for row in documents.get('library.json', []) if row['id'] == key), None)}
            if not sources:deferred.add(name);continue
            value = {'lyric_sources': sources, 'links': {key: identity for key, identity in value['links'].items() if identity in sources}}
        if name.startswith('timings/'):
            song = songs.get(Path(name).stem)
            if song is None or song['id'] in protected and (root / name).exists():
                deferred.add(name);continue
            fingerprint = Song.from_dict(song).fingerprint
            if fingerprint != value['fingerprint']:
                incoming = next((row for row in documents.get('library.json', []) if row['id'] == song['id']), None)
                if (song['id'] in protected or incoming is None or not _same_rows(song['lines'], incoming['lines'])
                        or any(song.get(key) != incoming.get(key) for key in ('title', 'artist', 'album'))):
                    deferred.add(name);continue
                value = {**value, 'fingerprint': fingerprint}
        clock_exceptions = set()
        if name.startswith('native_timings/') and not (root / name).exists():
            filename = Path(name).name
            legacy = native_aliases.get(filename)
            if legacy is not None:
                previous = _read(native_directory / legacy)
                if previous is not None:
                    merged_clock = _timing(previous, value)
                    if merged_clock is None:deferred.add(name);continue
                    value = merged_clock
                    clock_exceptions.add('native_timings/' + legacy)
            elif has_native and name not in clock_paths:
                # An unidentifiable new hash could hide an active legacy clock.
                deferred.add(name);continue
        if name.startswith(('timings/', 'live_timings/', 'native_timings/')):
            current_clock = _read(root / name)
            if current_clock != value and other_clock(name, value, clock_exceptions):
                deferred.add(name);continue
        if name.startswith(('lyrics_cache/', 'playlist_lyrics/tracks/')) and value.get('direct') is not None:
            current_value = _read(root / name, {})
            previous = current_value.get('direct')
            if previous != value['direct'] and other_clock(name, value):
                value = {**value, 'direct': deepcopy(previous)}
                deferred.add(name)
        if name.startswith('lyric_annotations/'):
            song = songs.get(Path(name).stem)
            if (song is None or song['id'] in protected or Song.from_dict(song).fingerprint != value['fingerprint']
                    or len(song['lines']) != len(value['lines'])):
                deferred.add(name);continue
        propose(name, value)
    # Filling a library translation changes its fingerprint. Use the existing
    # annotation channel instead, keeping both the base song and its clock.
    for incoming in documents.get('library.json', []):
        current = existing_songs.get(incoming['id'])
        if current is None or incoming['id'] in protected or incoming['id'] in blocked_ids:continue
        if any(current.get(key) != incoming.get(key) for key in ('title', 'artist', 'album')):
            deferred.add('library.json');continue
        rows = _rows(current.get('lines'), incoming['lines'])
        if rows is None:deferred.add('library.json');continue
        if rows == current['lines']:continue
        annotation = {'fingerprint': Song.from_dict(current).fingerprint, 'lines': rows}
        propose('lyric_annotations/' + incoming['id'] + '.json', annotation)
    # Finish JSON serialization and size checks for every change before the
    # first live write. Existing corrupt files cause failure, never reset.
    for name, value in changes.items():
        _safe(root / name)
        if len(public._encoded(value)) > MAX_ENTRY_BYTES:raise ValueError('Merged data file is too large')
    summary = {'added_files': len(added), 'updated_files': len(changes) - len(added),
               'deferred_files': len(deferred), 'unchanged_files': len(unchanged - changes.keys())}
    return changes, summary


def apply_pending(data_dir):
    """Apply only while the caller owns SingleInstance and no app is reading."""
    root = _root(data_dir)
    pending = _read(root / PENDING)
    if pending is None:return {'status': 'none', 'added_files': 0, 'updated_files': 0, 'deferred_files': 0, 'unchanged_files': 0}
    if (not isinstance(pending, dict) or set(pending) != {'version', 'pack_hash'}
            or type(pending['version']) is not int or pending['version'] != 1):
        raise ValueError('Invalid pending data update')
    digest = public._identifier(pending['pack_hash'])
    archive = root / STAGING / (digest + '.zip')
    receipts = _receipts(root)
    if digest in receipts['applied']:
        (root / PENDING).unlink(missing_ok=True)
        _safe(archive);archive.unlink(missing_ok=True)
        return {**receipts['applied'][digest], 'status': 'already_applied', 'pack_hash': digest}
    documents, body = _package(archive)
    if hashlib.sha256(body).hexdigest() != digest:raise ValueError('Staged data update was modified')
    try:changes, counts = _plan(root, documents)
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError('Invalid current data; update was not applied') from error
    result = {'status': 'applied', 'pack_hash': digest, **counts}
    for name, value in sorted(changes.items()):
        _safe(root / name)
        atomic_json(root / name, value)
    receipts['applied'][digest] = result
    _safe(root / RECEIPTS);atomic_json(root / RECEIPTS, receipts)
    (root / PENDING).unlink(missing_ok=True)
    archive.unlink(missing_ok=True)
    return result
