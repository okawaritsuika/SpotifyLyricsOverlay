"""Collect public Spotify recording facts for an offline dictionary audit.

No login, playback, or personal-library writes. Only normalized metadata is saved.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import heapq
from pathlib import Path
import re
import sys
import threading
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spotify_public_catalog import _source_url, parse_public_catalog, parse_public_tracks, parse_embed_tracks
from artist_names import name_key
from spotify_api import text_key
from storage import atomic_json


def collect(manifest, directory, interval=1.0):
    directory.mkdir(parents=True, exist_ok=True)
    tracks = {}
    discovered = {}
    pages = {}
    stopped = threading.Event()
    request_lock = threading.Lock()
    next_request = 0.0
    path = directory / 'public-tracks.json'
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        tracks.update((track['id'], track) for track in previous.get('tracks', []))
        pages.update(previous.get('pages', {}))
        discovered.update((track['id'], track) for track in previous.get('discovered', []))

    def save():
        atomic_json(path, {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
                          'tracks': list(tracks.values()), 'discovered': list(discovered.values()),
                          'pages': pages})

    def fetch(url, embed=False):
        nonlocal next_request
        if stopped.is_set():return url, [], [], 'stopped'
        # Pace the shared request stream, including across the small worker pool.
        with request_lock:
            if stopped.wait(max(0, next_request - time.monotonic())):
                return url, [], [], 'stopped'
            next_request = time.monotonic() + interval
        try:
            response = requests.get(url, timeout=(8, 20), headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code == 429:
                stopped.set()
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:raise ValueError('page too large')
            return url, parse_public_tracks(response.text, url), parse_embed_tracks(response.text) if embed else [], ''
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            return url, [], [], type(error).__name__

    def batch(jobs):
        count = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetch, url, embed) for url, embed in jobs
                       if url not in pages or pages[url].get('error')]
            for future in as_completed(futures):
                url, values, hints, error = future.result()
                for track in values:tracks[track['id']] = track
                for track in hints:discovered[track['id']] = track
                if error != 'stopped':pages[url] = {'tracks': len(values), 'discovered': len(hints), 'error': error}
                count += 1
                if count % 20 == 0:
                    save();print(f'pages={len(pages)} tracks={len(tracks)} discovered={len(discovered)}', flush=True)
        save()

    playlists = [entry['url'] for entry in manifest['playlists']]
    batch([(url, False) for url in playlists] +
          [(url.replace('open.spotify.com/', 'open.spotify.com/embed/'), True) for url in playlists])
    # Fetch each discovered recording once; a track page also exposes its album's
    # track list, so later recordings from that album can reuse the same facts.
    for identifier in manifest.get('track_ids', []):
        discovered.setdefault(identifier, {'id': identifier})
    pending = [identifier for identifier in discovered if identifier not in tracks]
    for start in range(0, len(pending), 80):
        if stopped.is_set():break
        batch([('https://open.spotify.com/track/' + identifier, False)
               for identifier in pending[start:start + 80] if identifier not in tracks])
    print(json.dumps({'pages': len(pages), 'tracks': len(tracks), 'discovered': len(discovered),
                      'rate_limited': stopped.is_set()}, ensure_ascii=False), flush=True)


def _artist_key(value):
    value = re.sub(r'\s*[（(]\s*CV\s*[.:：]?\s*[^()（）]+[)）]\s*$', '', value, flags=re.I)
    return name_key(value)


def _related_album(name, known_albums):
    key = text_key(name)
    return key in known_albums or _brand_album(name)


def _brand_album(name):
    key = text_key(name)
    return 'idolmaster' in key or 'idolm@ster' in key or 'アイドルマスター' in name


def expand(manifest, directory, seed, catalog, interval=1.0, max_pages=1600, verified_artists=(), verified_track_ids=()):
    """Follow public artist/album references for known dictionary performers.

    Album pages provide whole release track lists, including tracksV2. Public
    artist pages provide a limited release sample, not exhaustive discography.
    Related artists are followed only when their names occur in the dictionary
    or the supplied validated seed. A 429 stops this run without retrying it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'public-tracks.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    prior_expansion = previous.get('expansion', {})
    if prior_expansion.get('rate_limited'):
        try:
            collected_at = datetime.fromisoformat(previous['collected_at']).timestamp()
            remaining = collected_at + (prior_expansion.get('retry_after') or 60) - time.time()
        except (KeyError, ValueError, TypeError):remaining = 0
        if remaining > 0:
            summary = {'requests': 0, 'tracks': len(previous.get('tracks', [])),
                       'rate_limited': True, 'retry_after_remaining': int(remaining) + 1}
            print(json.dumps(summary), flush=True)
            return summary
    tracks = {track['id']: track for track in seed.get('tracks', [])}
    tracks.update((track['id'], track) for track in previous.get('tracks', []))
    pages = previous.get('pages', {})
    resources = previous.get('resources', {'albums': {}, 'artists': {}})
    discovered = {track['id']: track for track in previous.get('discovered', [])}
    songs = catalog.get('songs', [])
    character_artists = {_artist_key(name) for person in catalog.get('performers', [])
                     if person.get('kind') in ('idol', 'group')
                     for name in [person.get('name', ''), *person.get('aliases', [])]
                     if isinstance(name, str) and name.strip()}
    known_artists = character_artists | {_artist_key(name) for name in verified_artists if isinstance(name, str) and name.strip()}
    verified_track_ids = set(verified_track_ids)
    # Titles such as ALIVE recur outside this franchise. A same-title seed must
    # never authorize a new artist. Explicit branding is independent evidence.
    for row in tracks.values():
        if _brand_album(row.get('album', '')):
            known_artists.update(_artist_key(name) for name in row.get('artists', []))
    scoped_seed = [row for row in tracks.values() if _brand_album(row.get('album', ''))
                   or row['id'] in verified_track_ids
                   or any(_artist_key(name) in character_artists for name in row.get('artists', []))]
    known_albums = {text_key(row.get('album', '')) for row in [*songs, *scoped_seed]}
    known_albums.discard('')
    old_artist_count = len(resources.get('artists', {}));old_album_count = len(resources.get('albums', {}))
    resources['artists'] = {identifier: item for identifier, item in resources.get('artists', {}).items()
                            if _artist_key(item['name']) in known_artists}
    resources['albums'] = {identifier: item for identifier, item in resources.get('albums', {}).items()
                           if _related_album(item['name'], known_albums)
                           or _artist_key(resources['artists'].get(item.get('discovery_artist_id'), {}).get('name', '')) in character_artists}
    pruned_artists = old_artist_count - len(resources['artists'])
    pruned_albums = old_album_count - len(resources['albums'])
    pending = [];queued = set();serial = 0;attempts = 0;rate_limited = False;retry_after = None

    def enqueue(url, priority, album=''):
        nonlocal serial
        canonical = _source_url(url)
        if not canonical:return
        if '/embed/' in url:canonical = canonical.replace('open.spotify.com/', 'open.spotify.com/embed/')
        if canonical in queued or (canonical in pages and not pages[canonical].get('error')):return
        queued.add(canonical);serial += 1
        heapq.heappush(pending, (priority, serial, canonical, album))

    def save():
        pending_types = {}
        for _, _, url, _ in pending:
            kind = _source_url(url).split('/')[3]
            pending_types[kind] = pending_types.get(kind, 0) + 1
        atomic_json(path, {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
                          'tracks': list(tracks.values()), 'discovered': list(discovered.values()),
                          'resources': resources, 'pages': pages,
                          'expansion': {'requests_this_run': attempts, 'pending_pages': len(pending),
                                        'rate_limited': rate_limited, 'retry_after': retry_after,
                                        'pruned_artists': pruned_artists, 'pruned_albums': pruned_albums,
                                        'pending_types': pending_types,
                                        'limit_reached': attempts >= max_pages and bool(pending)}})

    for item in manifest.get('playlists', []):
        url = _source_url(item.get('url'))
        if url:
            enqueue(url, 0)
            enqueue(url.replace('open.spotify.com/', 'open.spotify.com/embed/'), 0)
    for kind in ('artists', 'albums'):
        for item in resources.get(kind, {}).values():
            enqueue(item['url'], 2 if kind == 'artists' else 1)
            if kind == 'albums' and item.get('track_count', 0) > item.get('listed_tracks', 0):
                enqueue(item['url'].replace('open.spotify.com/', 'open.spotify.com/embed/'), 1)
    # One representative per known album/credit seeds previously unseen regions
    # of the catalog. Prefer broad ensemble credits for early artist discovery.
    seen_artists = set();seen_albums = set()
    fetched_album_tracks = {identifier for identifier, item in tracks.items()
                            if '/album/' in item.get('source_url', '') and item['source_url'] in pages
                            and not pages[item['source_url']].get('error')}
    for row in sorted(scoped_seed, key=lambda row: -len(row.get('artists', []))):
        artists = {_artist_key(name) for name in row.get('artists', [])}
        album = text_key(row.get('album', ''))
        source = _source_url(row.get('source_url'))
        if '/album/' in source:
            enqueue(source, 1)
            seen_artists.update(artists);seen_albums.add(album)
            continue
        if row['id'] in fetched_album_tracks:continue
        new_artist = bool(artists - seen_artists)
        if new_artist or album not in seen_albums:
            enqueue(row.get('url') or 'https://open.spotify.com/track/' + row['id'], 3 if new_artist else 4, album)
            seen_artists.update(artists);seen_albums.add(album)
    for identifier in manifest.get('track_ids', []):
        if isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9]{22}', identifier):
            discovered.setdefault(identifier, {'id': identifier})
    for identifier in discovered:
        if identifier not in tracks:enqueue('https://open.spotify.com/track/' + identifier, 3)
    next_request = time.monotonic()
    save()
    print(json.dumps({'phase': 'scope', 'tracks': len(tracks), 'pruned_artists': pruned_artists,
                      'pruned_albums': pruned_albums, 'pending': len(pending)}), flush=True)
    while pending and attempts < max_pages:
        priority, _, url, seed_album = heapq.heappop(pending)
        identifier = url.rsplit('/', 1)[-1]
        if '/track/' in url and (identifier in fetched_album_tracks or (not seed_album and identifier in tracks)):
            continue
        time.sleep(max(0, next_request - time.monotonic()))
        next_request = time.monotonic() + interval
        attempts += 1
        try:
            response = requests.get(url, timeout=(8, 20), headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code == 429:
                rate_limited = True
                value = response.headers.get('Retry-After', '')
                retry_after = int(value) if value.isdigit() else None
                pages[url] = {'error': 'rate_limited', 'http_status': 429}
                break
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:raise ValueError('page too large')
            result = parse_public_catalog(response.text, url)
            hints = parse_embed_tracks(response.text) if '/embed/' in url else []
            for track in result['tracks']:tracks[track['id']] = track
            for track in hints:discovered[track['id']] = track
            related_artists = [item for item in result['artists'] if _artist_key(item['name']) in known_artists]
            for item in related_artists:
                resources['artists'][item['id']] = item;enqueue(item['url'], 2)
            # A known performer's own release sample is useful discovery. Do
            # not recursively crawl unrelated solo releases merely because a
            # voice actor also appears somewhere in the dictionary.
            source_artist = resources['artists'].get(url.rsplit('/', 1)[-1], {}) if '/artist/' in url else {}
            relevant_source = _artist_key(source_artist.get('name', '')) in character_artists or any(
                track['id'] in verified_track_ids or (_related_album(track['album'], known_albums)
                and any(_artist_key(name) in known_artists for name in track['artists']))
                for track in result['tracks'])
            for item in result['albums']:
                if relevant_source or _related_album(item['name'], known_albums):
                    resources['albums'].setdefault(item['id'], {}).update(item)
                    if _artist_key(source_artist.get('name', '')) in character_artists:
                        resources['albums'][item['id']]['discovery_artist_id'] = url.rsplit('/', 1)[-1]
                    enqueue(item['url'], 1)
                    if item.get('track_count', 0) > item.get('listed_tracks', 0):
                        enqueue(item['url'].replace('open.spotify.com/', 'open.spotify.com/embed/'), 1)
            # An embed can reveal rows after the first 30 regular playlist rows.
            # Fetch representative discoveries to find their actual album IDs.
            for track in hints:
                if track['id'] not in tracks:enqueue('https://open.spotify.com/track/' + track['id'], 3)
            pages[url] = {'tracks': len(result['tracks']), 'discovered': len(hints),
                          'albums': len(result['albums']), 'artists': len(result['artists']), 'error': ''}
            if '/album/' in url:
                fetched_album_tracks.update(track['id'] for track in result['tracks'])
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            pages[url] = {'error': type(error).__name__}
        if attempts % 20 == 0:
            save()
            print(json.dumps({'requests': attempts, 'tracks': len(tracks), 'albums': len(resources['albums']),
                              'artists': len(resources['artists']), 'pending': len(pending)}, ensure_ascii=False), flush=True)
    save()
    summary = {'requests': attempts, 'tracks': len(tracks), 'albums': len(resources['albums']),
               'artists': len(resources['artists']), 'pending': len(pending), 'rate_limited': rate_limited}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/diagnostics/spotify-bulk-20261003')
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--expand', action='store_true')
    parser.add_argument('--seed', type=Path, default=ROOT / 'data/spotify_public_tracks.json')
    parser.add_argument('--catalog', type=Path, default=ROOT / 'resources/song_catalog.json')
    parser.add_argument('--linked-library', type=Path, default=ROOT / 'data/explorer_library.json')
    parser.add_argument('--playlist-url', action='append', default=[])
    parser.add_argument('--max-pages', type=int, default=1600)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:parser.error('interval must be between 1 and 60 seconds')
    if not 1 <= args.max_pages <= 10000:parser.error('max-pages must be between 1 and 10000')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    manifest['playlists'] = [{'url': url} for url in args.playlist_url] + manifest.get('playlists', [])
    if args.expand:
        linked = json.loads(args.linked_library.read_text(encoding='utf-8')).get('spotify', {}) if args.linked_library.exists() else {}
        verified_artists = {name for track in linked.values() if isinstance(track, dict) and track.get('verification')
                            for name in track.get('artists', []) if isinstance(name, str)}
        verified_track_ids = {track['id'] for track in linked.values() if isinstance(track, dict)
                              and track.get('verification') and isinstance(track.get('id'), str)}
        expand(manifest, args.directory, json.loads(args.seed.read_text(encoding='utf-8')),
               json.loads(args.catalog.read_text(encoding='utf-8')), args.interval, args.max_pages, verified_artists, verified_track_ids)
    else:collect(manifest, args.directory, args.interval)
