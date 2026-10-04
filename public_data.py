"""Export an explicit public-data allowlist without modifying private app data.

Unknown files/fields, account state, request logs, paths and collection times are
never copied. Lyric text and translations retain their original rights; this
export is a privacy boundary, not a license grant.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit, urlunsplit

from storage import validate_times


ROOT = Path(__file__).resolve().parent
MAX_BYTES = 64 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')
TRACK_ID = re.compile(r'[A-Za-z0-9]{22}\Z')
SONG_ID = re.compile(r'(?:[0-9a-f]{24}|mb:[0-9a-f-]{36}|[A-Za-z0-9_-]{1,160})\Z')
PRIVATE_TEXT = re.compile(
    r'(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\[^\\]+\\|file://|/(?:Users|home)/|'
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|'
    r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
    r'sk-(?:proj-)?[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})|'
    r'(?:access_token|refresh_token|client_secret|password)\s*[=:])', re.I)
SOURCE_LABELS = {'Spotify', 'LRCLIB', 'ALSong', 'LyricsPlus', 'reviewed_translation',
                 'existing_local_cache', 'playlist_lyrics', 'reviewed_source_correction', 'manual_review'}
TIMING_SOURCES = {'직접 기록', '직접 기록 / 음원 분석', '직접 저장한 싱크',
                  '구절별 수동 / 음원 분석', '음원 분석', 'LRCLIB'}
AUDIO_METHODS = {'supplied-lyrics-alignment', 'bounded-gap-alignment-v1',
                 'bounded-gap-alignment-v2', 'incremental-lyrics-alignment-v1'}


def _text(value):
    # The official Korean series name resembles an email address.
    if (not isinstance(value, str) or len(value) > 32000
            or any(match.group().casefold() != 'idolm@ster.kr' for match in PRIVATE_TEXT.finditer(value))):
        raise ValueError('Invalid or potentially private public text')
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Invalid public number')
    return value


def _strings(value):
    if not isinstance(value, list):raise ValueError('Invalid text list')
    return [_text(item) for item in value]


def _object(value):
    if not isinstance(value, dict):raise ValueError('Invalid public document')
    return value


def _identifier(value, pattern=HEX):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError('Invalid public identifier')
    return value


def _fields(value, names):
    value = _object(value)
    return {key: _text(value[key]) for key in names}


def _rows(value, annotations=False):
    if not isinstance(value, list) or not 0 < len(value) <= 2000:
        raise ValueError('Invalid public lyric rows')
    rows = []
    for row in value:
        clean = _fields(row, ('original', 'reading', 'korean'))
        if annotations:
            if 'singers' in row:clean['singers'] = _strings(row['singers'])
            if 'original_variants' in row:
                clean['original_variants'] = [
                    {'source': 'local_spotify_original', 'text': _text(item['text'])}
                    for item in row['original_variants'] if item.get('source') == 'local_spotify_original']
        rows.append(clean)
    return rows


def _url(value):
    """Only public recording or lyric URLs; strip query/fragment tracking."""
    if not isinstance(value, str):return ''
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return ''
        host = parsed.hostname
        path = parsed.path
        if host == 'open.spotify.com':
            # Playlist membership can expose a personal collection; retain
            # recording sources only, including public album/artist metadata.
            if not re.fullmatch(r'/(?:embed/)?(?:track|album|artist)/[A-Za-z0-9]{22}/?', path):return ''
        elif host == 'namu.wiki':
            if not path.startswith('/w/'):return ''
        else:return ''
        _text(unquote(path))
        return urlunsplit(('https', host, path, '', ''))
    except ValueError:
        return ''


def _sources(values):
    if not isinstance(values, list):raise ValueError('Invalid public sources')
    result = []
    for value in values:
        if not isinstance(value, str):continue
        source = _url(value) or (value.split(':', 1)[0] if value.split(':', 1)[0] in SOURCE_LABELS else '')
        if source and source not in result:result.append(source)
    return result


def _track(value):
    result = _fields(value, ('title', 'album'))
    result['id'] = _identifier(value['id'], TRACK_ID)
    result['artists'] = _strings(value['artists'])
    result['duration'] = _number(value['duration'])
    if not result['title'] or not result['artists'] or result['duration'] <= 0:
        raise ValueError('Incomplete public recording')
    return result


def _direct(value, depth=0):
    if value is None:return None
    if depth > 1 or value['source'] not in {'Spotify', 'LRCLIB', 'ALSong', 'LyricsPlus'}:
        raise ValueError('Invalid lyric provider')
    result = {'source': value['source'], 'lines': _rows(value['lines']), 'times': value['times']}
    validate_times(result['times'], len(result['lines']))
    identifier = value.get('provider_id')
    if identifier is not None:
        identifier = _text(identifier) if isinstance(identifier, str) else _number(identifier)
    result['provider_id'] = identifier
    if value.get('fallback') is not None:result['fallback'] = _direct(value['fallback'], depth + 1)
    return result


def _song(value):
    return {**_fields(value, ('title', 'artist', 'album')),
            'id': _identifier(value['id'], SONG_ID), 'lines': _rows(value['lines'])}


def _edited_settings(value, songs):
    """Keep only the public bindings needed to prefer explicitly edited lyrics."""
    value = _object(value)
    identifiers = {song['id'] for song in songs}
    sources = {identifier: {'user_override': True}
               for identifier, source in _object(value.get('lyric_sources', {})).items()
               if identifier in identifiers and isinstance(source, dict) and source.get('user_override') is True}
    if not sources:return None
    links = {}
    for key, identifier in _object(value.get('links', {})).items():
        if identifier not in sources:continue
        parts = _text(key).split('\0')
        if len(parts) != 3 or not parts[0].strip() or any(len(part) > 4000 for part in parts):
            raise ValueError('Invalid edited lyric recording key')
        links[key] = identifier
    return {'lyric_sources': sources, 'links': links}


def _history(value):
    result = {**_fields(value, ('title', 'artist', 'album')),
              'id': _identifier(value['id']), 'lines': _rows(value['lines'])}
    body = [result['title'], result['artist'], result['album'], [row['original'] for row in result['lines']]]
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if digest != result['id']:raise ValueError('Translation document identity mismatch')
    return result


def _timing(value):
    value = _object(value)
    times = value['times']
    validate_times(times, len(times))
    result = {'times': times}
    for name in ('fingerprint', 'signature'):
        if name in value:result[name] = _identifier(value[name])
    for name in ('offset', 'estimate_intro', 'estimate_outro', 'duration', 'analysis_duration'):
        if name in value:result[name] = _number(value[name])
    for name in ('manual_priority', 'audio_finalized', 'completion_pending'):
        if type(value.get(name)) is bool:result[name] = value[name]
    if value.get('source') in TIMING_SOURCES:result['source'] = value['source']
    if value.get('audio_method') in AUDIO_METHODS:result['audio_method'] = value['audio_method']
    if value.get('method') == 'observed-spotify-transition-v1':result['method'] = value['method']
    if 'origins' in value:
        origins = value['origins']
        if len(origins) != len(times) or any(item not in (None, 'manual', 'audio') for item in origins):
            raise ValueError('Invalid timing origins')
        result['origins'] = origins
    if 'automatic_times' in value:
        validate_times(value['automatic_times'], len(times))
        result['automatic_times'] = value['automatic_times']
    if 'analyzed_ranges' in value:
        result['analyzed_ranges'] = [[_number(stamp) for stamp in pair] for pair in value['analyzed_ranges']]
    return result


def _cache(value):
    key = _text(value['key'])
    if len(key.split('\0')) != 3:raise ValueError('Invalid recording cache identity')
    result = {'version': 1, 'key': key, 'duration': _number(value['duration']),
              'saved_at': 0, 'lines': _rows(value['lines']), 'direct': _direct(value.get('direct'))}
    if value.get('reuse') is not None:
        reuse = value['reuse']
        result['reuse'] = {'source_key': _text(reuse['source_key']),
                           'source_duration': _number(reuse['source_duration'])}
    return result


def _playlist(value):
    from lyrics import Line
    from public_lyrics import reviewed_original_conflict
    track, rows = _track(value['track']), _rows(value['lines'])
    if reviewed_original_conflict(track['title'], track['artists'][0], track['album'], track['duration'],
                                  tuple(Line(**row) for row in rows)):
        return None  # The runtime already rejects this independently reviewed wrong-song upload.
    return {'schema_version': 1, 'track': track, 'lines': rows,
            'direct': _direct(value.get('direct')), 'sources': _sources(value.get('sources', []))}


def _user_lyrics(value):
    source = {'kind': 'user_provided'}
    if url := _url(value.get('source', {}).get('url')):source['url'] = url
    return {'schema_version': 1, 'title': _text(value['title']), 'artists': _strings(value['artists']),
            'source': source, 'lines': _rows(value['lines'], annotations=True)}


def _parts(value):
    recordings = []
    for row in value['recordings']:
        item = {**_fields(row, ('title', 'album')), 'artists': _strings(row['artists']),
                'text_sha256': _identifier(row['text_sha256']), 'text_length': _number(row['text_length']),
                'parts': [{'start': _number(part['start']), 'end': _number(part['end']),
                           'singers': _strings(part['singers'])} for part in row['parts']], 'sources': []}
        if 'album_aliases' in row:item['album_aliases'] = _strings(row['album_aliases'])
        for source in row['sources']:
            url = _url(source.get('url'))
            kind = source.get('kind')
            if kind not in ('fan_part_transcription', 'official', 'user_provided',
                            'independent_lyrics', 'recording_metadata'):
                raise ValueError('Invalid singer-part source')
            if url:item['sources'].append({'kind': kind, 'url': url})
        if not item['sources']:raise ValueError('Singer part has no public attribution')
        recordings.append(item)
    return {'schema_version': 1, 'recordings': recordings}


def _safe_path(path):
    for item in (path, *path.parents):
        try:info = item.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked paths are not allowed in public export')


def _read(path):
    _safe_path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:raise ValueError('Invalid export input size')
    return json.loads(path.read_text(encoding='utf-8'))


def collect_public_data(source):
    """Read known schemas only. Any invalid retained value stops the export."""
    source = Path(source).absolute()
    _safe_path(source)
    source = source.resolve()
    if not source.is_dir():raise ValueError('Private data directory does not exist')
    documents = {}

    def add(relative, cleaner):
        path = source / relative
        if path.exists():
            try:
                value = cleaner(_read(path))
                if value is not None:documents[relative] = value
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError('Cannot safely export ' + relative) from error

    add('library.json', lambda value: [_song(song) for song in value])
    add('settings.json', lambda value: _edited_settings(value, documents.get('library.json', [])))
    folders = {'timings': _timing, 'live_timings': _timing, 'native_timings': _timing,
               'lyrics_cache': _cache, 'ai_translations/history': _history,
               'playlist_lyrics/tracks': _playlist, 'user_lyrics': _user_lyrics}
    for folder, cleaner in folders.items():
        _safe_path(source / folder)
        for path in sorted((source / folder).glob('*.json')):
            pattern = TRACK_ID if folder == 'playlist_lyrics/tracks' else SONG_ID if folder == 'timings' else HEX
            if folder != 'user_lyrics':_identifier(path.stem, pattern)
            relative = path.relative_to(source).as_posix()
            add(relative, cleaner)
            if folder == 'user_lyrics':
                value = documents.pop(relative)
                name = hashlib.sha256(_encoded(value)).hexdigest()
                documents[folder + '/' + name + '.json'] = value

    def imports(value):
        result = {}
        for identifier, rows in value['documents'].items():
            _identifier(identifier)
            result[identifier] = {}
            for index, text in _object(rows).items():
                if not re.fullmatch(r'[1-9][0-9]{0,3}', index) or int(index) > 1500:
                    raise ValueError('Invalid translation row index')
                result[identifier][index] = _text(text)
        return {'version': 1, 'documents': result}

    add('ai_translations/imports.json', imports)
    add('artist_metadata.json', lambda value: {'version': 1, 'entries': [
        {'request': {**_fields(row['request'], ('title', 'artist', 'album')),
                     'duration': _number(row['request']['duration'])}, 'track': _track(row['track'])}
        for row in value['entries']]})
    add('spotify_public_tracks.json', lambda value: {'schema_version': 1, 'tracks': [
        {**_track(row), 'source_url': 'https://open.spotify.com/track/' + _identifier(row['id'], TRACK_ID)}
        for row in value['tracks']]})
    add('explorer_library.json', lambda value: {'schema_version': 1, 'spotify': {
        _identifier(key, SONG_ID): {**_track(row), 'url': 'https://open.spotify.com/track/' + row['id']}
        for key, row in value.get('spotify', {}).items()}})
    for name in ('wiki_lyric_parts.json', 'lyric_parts.json'):add(name, _parts)
    add('wiki_documents.json', lambda value: {
        _identifier(key): {'url': url, 'learned': True} for key, item in value.items()
        if (url := _url(item if isinstance(item, str) else item.get('url')))})
    _safe_path(source / 'manual_annotations')
    for path in sorted((source / 'manual_annotations').glob('*.json')):
        value = _read(path)
        if not isinstance(value.get('lines'), list):continue
        cleaned = {**{key: _text(value[key]) for key in ('title', 'artist') if key in value},
                   'lines': _rows(value['lines'])}
        name = hashlib.sha256(_encoded(cleaned)).hexdigest()
        documents['manual_annotations/' + name + '.json'] = cleaned
    _safe_path(source / 'lyric_annotations')
    for path in sorted((source / 'lyric_annotations').glob('*.json')):
        _identifier(path.stem, SONG_ID)
        add(path.relative_to(source).as_posix(), lambda value: {
            'fingerprint': _identifier(value['fingerprint']),
            'lines': [None if row is None else _fields(row, ('original', 'reading', 'korean'))
                      for row in value['lines']]})
    tracks = {value['track']['id']: value['track'] for name, value in documents.items()
              if name.startswith('playlist_lyrics/tracks/')}
    if tracks:documents['playlist_lyrics/index.json'] = {'schema_version': 1, 'tracks': tracks}
    return documents


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def export_public_data(source=ROOT / 'data', destination=ROOT / 'resources' / 'seed_data'):
    """Write a fresh snapshot. Never overwrite a source or existing destination."""
    source, destination = Path(source).absolute(), Path(destination).absolute()
    _safe_path(source)
    _safe_path(destination)
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Public export must be separate from private data')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Public export destination must be empty')
    documents = collect_public_data(source)
    bodies = {name: _encoded(value) for name, value in documents.items()}
    categories = Counter(name.rsplit('/', 1)[0] if '/' in name else name for name in documents)
    summary = {'schema_version': 1, 'files': len(documents), 'bytes': sum(map(len, bodies.values())),
               'categories': dict(sorted(categories.items())),
               'library_songs': len(documents.get('library.json', [])),
               'playlist_tracks': len(documents.get('playlist_lyrics/index.json', {}).get('tracks', {})),
               'translation_documents': categories.get('ai_translations/history', 0),
               'translated_rows': sum(len(rows) for rows in documents.get('ai_translations/imports.json', {}).get('documents', {}).values()),
               'public_tracks': len(documents.get('spotify_public_tracks.json', {}).get('tracks', [])),
               'recording_links': len(documents.get('explorer_library.json', {}).get('spotify', {})),
               'user_lyric_overrides': len(documents.get('settings.json', {}).get('lyric_sources', {})),
               'privacy': 'Allowlisted music data and edited-lyric bindings only; no account state, history, personal preferences, paths, requests or collection times.',
               'rights': 'Privacy filtering does not grant redistribution rights to third-party lyrics or translations.'}
    for name, body in sorted(bodies.items()):
        path = destination / name
        _safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:stream.write(body)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / '_manifest.json').write_bytes(_encoded(summary))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data')
    parser.add_argument('--output', type=Path, default=ROOT / 'resources' / 'seed_data')
    arguments = parser.parse_args()
    print(json.dumps(export_public_data(arguments.source, arguments.output), ensure_ascii=False, indent=2))
