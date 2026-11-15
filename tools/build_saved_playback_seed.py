"""Pair exported song annotations with exact recorded playback identities."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artist_metadata import validated_track
from live_timing import original_signature
from lyrics import Line, Song
from public_data import _cache, _encoded, _read


def recording(track):
    return '\0'.join((track['title'], track['artists'][0], track['album']))


def add_playback_seed(seed, previous, verified_cache=None):
    manifest = seed / '_manifest.json'
    summary = _read(manifest)
    directories = {name: seed / name for name in ('lyrics_cache', 'live_timings',
                                                   'singer_edits', 'lyric_text_edits')}
    clocks = {path.stem: _read(path) for path in directories['live_timings'].glob('*.json')}
    durations, candidates = defaultdict(list), defaultdict(list)
    caches = {}
    for directory in (directories['lyrics_cache'], previous / 'lyrics_cache'):
        for path in directory.glob('*.json'):
            value = _cache(_read(path))
            key = value['key']
            durations[key].append(value['duration'])
            candidates[key].append(value)
            if directory == directories['lyrics_cache']:caches[key] = value

    for entry in _read(seed / 'artist_metadata.json')['entries']:
        request = entry['request']
        track = validated_track(request, entry['track'])
        if track:
            durations['\0'.join(request[name] for name in ('title', 'artist', 'album'))].append(request['duration'])
    tracks = _read(seed / 'playlist_lyrics' / 'index.json')['tracks']
    playlist_keys = {recording(track) for track in tracks.values()}
    for track in tracks.values():
        durations[recording(track)].append(track['duration'])
    for track in _read(seed / 'spotify_public_tracks.json')['tracks']:
        durations[recording(track)].append(track['duration'])

    if verified_cache:
        reference = _cache(_read(verified_cache))
        direct = reference.get('direct')
        key = reference['key']
        title, artist, album = key.split('\0')
        request = {'title': title, 'artist': artist, 'album': album, 'duration': reference['duration']}
        provider_id = direct.get('provider_id') if direct else None
        metadata = _read(seed / 'artist_metadata.json')['entries']
        if (not direct or direct['source'] != 'Spotify' or not all(stamp is not None for stamp in direct['times'])
                or not any(entry['track']['id'] == provider_id and validated_track(request, entry['track'])
                           for entry in metadata)):
            raise ValueError('The additional Spotify clock could not be tied to the exported recording')
        candidates[key].append(reference)

    for path in (seed / 'ai_translations' / 'history').glob('*.json'):
        value = _read(path)
        key = '\0'.join(value[name] for name in ('title', 'artist', 'album'))
        if key not in durations:continue
        candidates[key].append({'key': key, 'lines': value['lines'], 'direct': None})

    def quality(key, lines):
        suffix = sha256(key.encode()).hexdigest()[:24]
        originals = tuple(row['original'] for row in lines)
        layout = sha256(json.dumps((key, originals)).encode()).hexdigest()
        song = Song('memory-' + suffix, *key.split('\0'), tuple(Line(**row) for row in lines))
        signature = original_signature(song)
        authored = audio = 0
        for prefix in ('memory-', 'completed-'):
            clock = clocks.get(sha256((prefix + suffix).encode()).hexdigest(), {})
            if clock.get('signature') == signature:
                origins = clock.get('origins', ())
                authored += origins.count('manual') + bool(clock.get('cleared_rows') or clock.get('offset'))
                audio += origins.count('audio')
        edit = any((directories[name] / (layout + '.json')).is_file()
                   for name in ('singer_edits', 'lyric_text_edits'))
        return (bool(authored), edit, audio)

    changed = Counter()
    for key, values in candidates.items():
        choices = durations[key]
        if not choices or max(choices) - min(choices) > 1.0:continue
        if any(not part.strip() for part in key.split('\0')):continue
        by_layout = {}
        current = caches.get(key)
        for value in values:
            originals = tuple(row['original'] for row in value['lines'])
            if not originals:continue
            group = by_layout.setdefault(originals, [])
            group.append(value)
        best = None
        for group in by_layout.values():
            rows = [dict(row) for row in group[0]['lines']]
            for value in group[1:]:
                for old, row in zip(rows, value['lines']):
                    for field in ('korean', 'reading'):
                        if row[field].strip() and (field not in old or not old[field].strip()):
                            old[field] = row[field]
            owner = max((value for value in group if value.get('direct')),
                        key=lambda value: sum(stamp is not None for stamp in value['direct']['times']), default=None)
            direct = owner.get('direct') if owner else None
            authored, edit, audio = quality(key, rows)
            translated = sum(bool(row['korean'].strip()) for row in rows)
            synced = sum(stamp is not None for stamp in direct['times']) if direct else 0
            if not (authored or edit or audio or key not in playlist_keys and (translated or synced)):
                continue
            rank = (authored, edit, audio > 0, synced > 0, translated, synced, len(rows))
            if best is None or rank > best[0]:best = (rank, rows, owner or group[0])
        if best is None:continue
        _, rows, owner = best
        # Runtime verifies this identity, the clock's row mapping and both durations again.
        duration = current['duration'] if current else choices[0]
        value = _cache({**owner, 'key': key, 'duration': duration, 'lines': rows})
        path = directories['lyrics_cache'] / (sha256(key.encode()).hexdigest() + '.json')
        body = _encoded(value)
        before = path.read_bytes() if path.is_file() else None
        if before == body:continue
        path.write_bytes(body)
        changed['new' if before is None else 'updated'] += 1
        changed['bytes'] += len(body) - len(before or b'')
    summary['files'] += changed['new']
    summary['bytes'] += changed['bytes']
    summary['categories']['lyrics_cache'] = summary['categories'].get('lyrics_cache', 0) + changed['new']
    manifest.write_bytes(_encoded(summary))
    return dict(changed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('seed', type=Path)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--verified-cache', type=Path)
    args = parser.parse_args()
    print(add_playback_seed(args.seed, args.previous, args.verified_cache))
