"""Create YouTube candidate links and export MP3s from permitted video links."""

import argparse
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import sleep
from types import SimpleNamespace
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from export_playlist_mp3 import ROOT, export_mp3, mp3_filename
from artist_credit_overrides import ArtistCreditOverrides
from artist_names import name_key, split_artist_affiliation
from catalog_sources import recording_kind
from group_credits import RESOURCE_PATH, _catalog
from local_analysis import worker_lock
from local_library import _key
from media import Playback
from playlist_lyrics_store import PlaylistLyricsStore
from recording_reuse import anniversary_reference, compatibility, select_related
from storage import atomic_json, read_json
from work_reuse import anniversary_work


MANIFEST = ROOT / 'data/playlist_lyrics/manifest.json'
LOCAL_YT_DLP = ROOT / 'output/playlist-youtube/yt-dlp.exe'
PLAYLIST_LINKS = ROOT / 'output/playlist-youtube/idolmaster-youtube-links.json'
PLAYLIST_MP3 = ROOT / 'output/mp3'
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
WATCH = 'https://www.youtube.com/watch?v='
CV_CREDIT = re.compile(r'(.*?)\s*\(\s*CV\s*[.:]?\s*([^()]+)\)', re.IGNORECASE)


def video_id(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port is not None:
        raise ValueError('HTTPS YouTube 영상 주소를 입력하세요.')
    if parsed.hostname == 'youtu.be':
        value = parsed.path.strip('/')
    elif parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        value = parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch' else ''
        if parsed.path.startswith('/shorts/'):
            value = parsed.path.removeprefix('/shorts/').rstrip('/')
    else:
        value = ''
    if not VIDEO_ID.fullmatch(value):
        raise ValueError('재생목록이나 검색 주소 대신 개별 YouTube 영상 주소를 입력하세요.')
    return value


def youtube_api(resource, key, **params):
    address = f'https://www.googleapis.com/youtube/v3/{resource}?' + urlencode({**params, 'key': key})
    try:
        with urlopen(address, timeout=20) as response:
            return json.load(response)['items']
    except HTTPError as error:
        raise RuntimeError(f'YouTube Data API가 HTTP {error.code}를 반환했습니다.') from None
    except URLError:
        raise RuntimeError('YouTube Data API에 연결하지 못했습니다.') from None


def candidate_links(track, query, key):
    found = youtube_api('search', key, part='snippet', type='video', maxResults=5, q=query)
    ids = [item['id']['videoId'] for item in found]
    if not ids:
        return []
    videos = youtube_api('videos', key, part='snippet,contentDetails', id=','.join(ids))
    records = []
    for video in videos:
        snippet = video['snippet']
        duration = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',
                                video['contentDetails']['duration'])
        seconds = (sum(int(value or 0) * scale for value, scale in zip(duration.groups(), (3600, 60, 1)))
                   if duration else None)
        records.append({'id': video['id'], 'title': snippet['title'], 'channel': snippet.get('channelTitle'),
                        'description': snippet.get('description'), 'duration': seconds})
    return rank_candidates(track, records)


def artist_name(artist):
    return re.split(r'\s*\(cv\s*[:. ]', _key(artist), maxsplit=1)[0].strip()


def title_key(value):
    return re.sub(r'[\s()\[\]\-–—]+', ' ', _key(value)).strip()


def rank_candidates(track, videos):
    title = title_key(track['title'])
    artists = tuple(filter(None, (artist_name(artist) for artist in track.get('artists', []))))
    expected_duration = track.get('duration')
    album = _key(track.get('album') or '')
    candidates = {}
    for video in videos:
        if not video or not VIDEO_ID.fullmatch(video.get('id') or ''):
            continue
        name = unescape(video.get('title') or '')
        channel = unescape(video.get('channel') or video.get('uploader') or '')
        description = unescape(video.get('description') or '')
        display_title = re.sub(r'\s*[\[(]official audio[\])]\s*$', '', name, flags=re.IGNORECASE)
        prefix, separator, suffix = display_title.partition(' - ')
        title_match = title_key(display_title) == title or (
            bool(separator) and title_key(suffix) == title and any(artist in _key(prefix) for artist in artists))
        credit = _key(f'{name} {channel} {video.get("artist") or ""} {description}')
        artist_match = any(artist in credit for artist in artists)
        album_match = bool(album and (album == _key(video.get('album') or '') or album in _key(description)))
        seconds = video.get('duration')
        close_duration = bool(seconds and expected_duration and abs(seconds - expected_duration) <= 2)
        verified = title_match and (artist_match or album_match) and close_duration
        official = channel.endswith(' - Topic') or _key(description).startswith('provided to youtube by')
        candidates[video['id']] = {'url': WATCH + video['id'], 'title': name, 'channel': channel,
                                   'duration': seconds, 'metadata_match': verified, 'official_channel': official}
    return sorted(candidates.values(), key=lambda item: (item['metadata_match'], item['official_channel']), reverse=True)


def public_video_details(executable, url):
    command = [executable, '--ignore-config', '--no-playlist', '--simulate', '--ignore-no-formats-error',
               '--encoding', 'utf-8', '--socket-timeout', '15', '--print',
               '%(.{id,title,channel,duration,artist,album,description})j', '--', url]
    result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    return json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else None


def search_video_links(track, query, executable):
    results = []
    queries = dict.fromkeys((query, title_key(track['title'])))
    for index, search in enumerate(queries):
        if index:
            sleep(0.6)
        command = [executable, '--ignore-config', '--flat-playlist', '--skip-download', '--dump-single-json',
                   '--encoding', 'utf-8', '--socket-timeout', '15', '--', f'ytsearch8:{search}']
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'YouTube 검색 실패')
        results.extend(json.loads(result.stdout).get('entries') or [])
        ranked = rank_candidates(track, results)
        if any(candidate['metadata_match'] for candidate in ranked):
            return ranked
    duration = track.get('duration')
    promising = [item for item in ranked if item['official_channel']
                 and title_key(item['title']) == title_key(track['title'])
                 and duration and item['duration'] and abs(item['duration'] - duration) <= 2]
    for candidate in promising[:2]:
        sleep(0.6)
        details = public_video_details(executable, candidate['url'])
        if details:
            results.append(details)
    return rank_candidates(track, results) if promising else ranked


def _spotify_synced(record, track_id):
    if not record or not record['lines']:
        return False
    direct = record.get('direct')
    spotify_sources = [source for source in record.get('snapshots', ())
                       if source['source'] == 'Spotify' and source.get('recording_verified')
                       and source.get('track_id') == track_id and not source.get('annotation_only')]
    if direct and direct['source'] == 'Spotify' and direct.get('provider_id') == track_id:
        spotify_sources.append(direct)
    return any(len(source['times']) == len(record['lines']) and all(stamp is not None for stamp in source['times'])
               for source in spotify_sources)


def track_playback(track):
    return Playback(title=track['title'], artist=(track.get('artists') or [''])[0],
                    album=track.get('album', ''), duration=track.get('duration'))


def anniversary_reference_index(store):
    grouped = {}
    for track in store.metadata():
        playback = track_playback(track)
        work = anniversary_work(playback)
        if not work:
            continue
        record = store.load(track['id'])
        if record and record.get('direct'):
            grouped.setdefault(work, []).append({
                'title': playback.title, 'artist': playback.artist, 'album': playback.album,
                'duration': playback.duration, 'key': playback.key, 'track_id': track['id'],
                'lines': record['lines'], 'direct': record['direct'],
            })
    return grouped


def has_anniversary_reference(track, track_id, record, grouped):
    playback = track_playback(track)
    candidates = []
    for candidate in grouped.get(anniversary_work(playback), ()):
        if candidate['track_id'] == track_id or candidate['key'] == playback.key:
            continue
        compatible = compatibility(candidate, playback)
        if compatible and compatible['work_evidence'] and compatible['duration_difference'] <= 2:
            candidates.append({**candidate, 'compatibility': compatible})
    related = select_related(candidates, playback)
    if not related:
        return False
    lines = record['lines'] if record else related['lines']
    return anniversary_reference(lines, related.get('anniversary_clocks', ())) is not None


def save_link_results(path, playlist_id, rows, include_synced=False, one_per_character=False,
                      include_anniversary_reference=False):
    result = {'playlist_id': playlist_id, 'entries': rows}
    if one_per_character:
        result['one_per_character'] = True
    atomic_json(path, result)
    failures = []
    review = []
    skipped_reference = 0
    for row in rows:
        if row.get('youtube_url') or (row.get('spotify_synced') and not include_synced):
            continue
        if row.get('anniversary_reference') and not include_anniversary_reference:
            skipped_reference += 1
            continue
        song = {name: row[name] for name in ('position', 'track_id', 'title')}
        if row.get('lookup_error'):
            failures.append({**song, 'error': row['lookup_error']})
        elif 'candidates' in row:
            review.append({**song, 'search_url': row.get('search_url'), 'candidates': row['candidates']})
    summary = {'tracks': len(rows), 'video_links': sum(bool(row.get('youtube_url')) for row in rows),
               'eligible_video_links': sum(bool(row.get('youtube_url')) and
                                           (include_synced or not row.get('spotify_synced')) for row in rows),
               'skipped_spotify_synced': sum(bool(row.get('spotify_synced')) and not include_synced for row in rows),
               'skipped_anniversary_reference': skipped_reference,
               'search_failed': len(failures), 'needs_review': len(review)}
    if include_synced:
        summary.update(characters=len({row['solo_credit']['character_id'] for row in rows}),
                       spotify_synced=sum(bool(row.get('spotify_synced')) for row in rows),
                       local_mp3s=sum(bool(row.get('local_mp3')) for row in rows))
    summary['unsearched'] = summary['tracks'] - sum(summary[field] for field in
                                                     ('eligible_video_links', 'skipped_spotify_synced',
                                                      'skipped_anniversary_reference', 'search_failed', 'needs_review'))
    report = path.with_name(f'{path.stem}.failures.json')
    atomic_json(report, {'summary': summary, 'search_failed': failures, 'needs_review': review})
    return summary


def read_link_list(path, playlist_id):
    try:
        previous = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    if previous and previous.get('playlist_id') != playlist_id:
        raise ValueError(f'다른 재생목록의 링크 파일입니다. {path}')
    return previous


def solo_credit(track, credits, overrides):
    artists = track.get('artists') or []
    if not artists:
        return None
    corrected = overrides.get(SimpleNamespace(**track, artist=artists[0]))
    original = overrides.entries.get(track.get('id'))
    if corrected and original and corrected == original['artists']:
        artists = corrected
    title = track['title']
    if (len(artists) != 1 or title.endswith('からのスペシャルメッセージ')
            or recording_kind(title, artists, [{'title': track.get('album') or ''}]) != 'song'):
        return None
    artist = artists[0]
    name, _ = split_artist_affiliation(unicodedata.normalize('NFKC', artist).strip())
    cv = CV_CREDIT.fullmatch(name)
    key = name_key(cv[1] if cv else name)
    identity = credits.idol_names.get(key)
    if not identity or (not cv and credits.names.get(key) != ('idol', identity)):
        return None
    return {'character_id': identity, 'character': credits.members[identity]['name'],
            'voice_actor': cv[2].strip() if cv else None, 'artist': artist}


def one_solo_per_character(rows, previous):
    def priority(row):
        return (row['track_id'] not in previous, not row['local_mp3'], not row.get('youtube_url'),
                not row['solo_credit']['voice_actor'], row['position'])

    picks = {}
    for row in rows:
        identity = row['solo_credit']['character_id']
        if identity not in picks or priority(row) < priority(picks[identity]):
            picks[identity] = row
    return sorted(picks.values(), key=lambda row: row['position'])


def write_links(args):
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    playlist_id = manifest['playlist_id']
    solos = args.command == 'solos'
    one_per_character = solos and args.one_per_character
    include_anniversary_reference = solos or getattr(args, 'include_anniversary_reference', False)
    key = os.environ.get('YOUTUBE_API_KEY') if args.resolve else None
    executable = None
    if args.resolve and not key:
        executable = shutil.which(args.yt_dlp)
        if not executable:
            raise ValueError('공개 검색에 사용할 yt-dlp.exe 경로를 --yt-dlp로 지정하세요.')
    previous = read_link_list(args.output, playlist_id)
    prior_rows = previous.get('entries', [])
    if previous and bool(previous.get('one_per_character')) != one_per_character:
        raise ValueError('이 목록을 만든 것과 동일한 --one-per-character 설정 또는 새 출력 경로를 사용하세요.')
    if solos and any('solo_credit' not in row for row in prior_rows):
        raise ValueError('기존 재생목록을 덮어쓰지 않도록 새 출력 경로를 지정하세요.')
    by_id = {row['track_id']: row for row in prior_rows}
    if solos:
        shared = {row['track_id']: row for row in read_link_list(args.reuse, playlist_id).get('entries', [])}
        credits = _catalog(RESOURCE_PATH)
        if credits is None:
            raise ValueError('캐릭터 크레딧 카탈로그를 읽을 수 없습니다.')
        overrides = ArtistCreditOverrides(ROOT / 'data')
        local_files = {path.name: str(path) for path in args.mp3_dir.glob('*.mp3')
                       if path.is_file()}
    lyrics = PlaylistLyricsStore(ROOT / 'data')
    references = anniversary_reference_index(lyrics) if not solos else {}
    rows = []
    for position, entry in enumerate(manifest['entries'], 1):
        track = entry.get('track') or {}
        if not track.get('title') or not entry.get('track_id'):
            continue
        track_id = entry['track_id']
        prior = by_id.get(track_id, {})
        if solos:
            singer = solo_credit(track, credits, overrides)
            if singer is None:
                continue
            cached = shared.get(track_id, {})
            prior = {**cached, **prior} if prior.get('youtube_url') or not cached.get('youtube_url') else cached
        record = lyrics.load(track_id)
        row = {**prior, **track, 'position': position, 'track_id': track_id,
               'spotify_synced': _spotify_synced(record, track_id)}
        row.pop('anniversary_reference', None)
        if references and not row['spotify_synced'] and has_anniversary_reference(track, track_id, record, references):
            row['anniversary_reference'] = True
        if solos:
            row.update(solo_credit=singer, local_mp3=local_files.get(mp3_filename(position, track['title'])))
        rows.append(row)
    if one_per_character:
        rows = one_solo_per_character(rows, by_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    end = None if args.limit is None else args.start + args.limit
    searched = 0
    try:
        for row in rows:
            if row['position'] < args.start or (end is not None and row['position'] >= end):
                continue
            if row['spotify_synced'] and not solos:
                continue
            if row.get('anniversary_reference') and not row.get('youtube_url') and not include_anniversary_reference:
                continue
            artists = [row['solo_credit']['artist']] if solos else row.get('artists', [])[:1]
            query = ' '.join([row['title'], *map(artist_name, artists)])
            row['search_url'] = 'https://music.youtube.com/search?' + urlencode({'q': query})
            pending = 'candidates' not in row or row.get('lookup_error') or args.retry_unmatched
            if args.resolve and not row.get('youtube_url') and not (one_per_character and row['local_mp3']) and pending:
                track = {**row, 'artists': list(dict.fromkeys([*artists, *row.get('artists', [])]))}
                try:
                    candidates = (candidate_links(track, query, key) if key
                                  else search_video_links(track, query, executable))
                except KeyboardInterrupt:
                    row['lookup_error'] = '검색이 중단됐습니다.'
                    raise
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                    row['lookup_error'] = str(error)
                    raise
                row.pop('lookup_error', None)
                row['candidates'] = candidates
                matches = [item for item in candidates if item['metadata_match']]
                official = [item for item in matches if item['official_channel']]
                preferred = official or matches
                row['youtube_url'] = preferred[0]['url'] if len(preferred) == 1 else None
                searched += 1
                if searched % 10 == 0:
                    save_link_results(args.output, playlist_id, rows, include_synced=solos,
                                      one_per_character=one_per_character,
                                      include_anniversary_reference=include_anniversary_reference)
                    print(f'{row["position"]}번 곡까지 처리 · 이번 실행 검색 {searched}곡', file=sys.stderr, flush=True)
                if executable:
                    sleep(0.6)
    finally:
        summary = save_link_results(args.output, playlist_id, rows, include_synced=solos,
                                    one_per_character=one_per_character,
                                    include_anniversary_reference=include_anniversary_reference)
    print(json.dumps({**summary, 'searched': searched}))


def load_links(path):
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.casefold() == '.json':
        return [row for row in json.loads(text)['entries'] if row.get('youtube_url')]
    urls = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    return [{'position': number, 'youtube_url': url} for number, url in enumerate(urls, 1)]


def download_source(executable, url, folder, node=None):
    command = [executable, '--ignore-config', '--no-playlist', '--no-progress', '--no-simulate',
               '--encoding', 'utf-8', '-f', 'bestaudio/best', '-P', str(folder), '-o', 'source.%(ext)s',
               '--print', 'after_move:%(.{filepath,title,artist,channel,album,duration})j']
    if node:
        command.extend(['--js-runtimes', f'node:{node}'])
    result = subprocess.run([*command, '--', url], capture_output=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '다운로드 실패')
    metadata = json.loads(result.stdout)
    source = Path(metadata['filepath']).resolve(strict=True)
    if source.parent != folder.resolve() or not source.is_file():
        raise ValueError('다운로드 결과 파일을 확인하지 못했습니다.')
    return source, metadata


def download_mp3s(args):
    from av import FFmpegError

    executable = shutil.which(args.yt_dlp)
    if not executable:
        raise ValueError('yt-dlp.exe를 설치하거나 --yt-dlp에 실행 파일 경로를 지정하세요.')
    entries = load_links(args.links)
    if not entries:
        raise ValueError('다운로드할 개별 YouTube 영상 링크가 없습니다.')
    args.output.mkdir(parents=True, exist_ok=True)
    failure_file = args.output / 'mp3-failures.json'
    failures = {(entry['link_list'], entry['position']): entry for entry in read_json(failure_file, [])}
    link_list = str(args.links.resolve())
    lyrics = PlaylistLyricsStore(ROOT / 'data')
    references = (anniversary_reference_index(lyrics)
                  if not args.include_anniversary_reference and any(row.get('track_id') and not row.get('solo_credit') for row in entries)
                  else {})
    counts = {'exported': 0, 'existing': 0, 'skipped_spotify_synced': 0,
              'skipped_anniversary_reference': 0, 'errors': 0}
    for row in entries:
        position = row['position']
        failure_key = (link_list, position)
        failure = None
        completed = False
        stage = 'link'
        try:
            track_id = row.get('track_id')
            check_reference = bool(references and track_id and row.get('title') and not row.get('solo_credit'))
            record = lyrics.load(track_id) if track_id and (not args.include_spotify_synced or check_reference) else None
            if not args.include_spotify_synced and _spotify_synced(record, track_id):
                counts['skipped_spotify_synced'] += 1
                completed = True
                continue
            if check_reference and has_anniversary_reference(row, track_id, record, references):
                counts['skipped_anniversary_reference'] += 1
                completed = True
                continue
            identifier = video_id(row['youtube_url'])
            destination = args.output / mp3_filename(position, row.get('title') or identifier)
            reused = args.reuse_mp3_dir and (args.reuse_mp3_dir / destination.name).is_file()
            if destination.exists() or reused:
                counts['existing'] += 1
                completed = True
                continue
            stage = 'download'
            with TemporaryDirectory(dir=args.output) as scratch:
                source, info = download_source(executable, WATCH + identifier, Path(scratch), args.node)
                stage = 'convert'
                if row.get('duration') and (not info.get('duration') or abs(row['duration'] - info['duration']) > 2):
                    raise ValueError('다운로드한 영상 길이가 재생목록 곡과 2초 넘게 다르거나 확인되지 않았습니다.')
                title = row.get('title') or info.get('title') or identifier
                artist = info.get('artist') or info.get('channel')
                track = {'title': title, 'album': row.get('album') or info.get('album'),
                         'artists': row.get('artists') or ([artist] if artist else [])}
                export_mp3(source, destination, track)
            counts['exported'] += 1
            completed = True
            print(f'{position:04d} {title}', file=sys.stderr)
        except FileExistsError:
            counts['existing'] += 1
            completed = True
        except KeyboardInterrupt:
            failure = '작업이 중단됐습니다.'
            raise
        except (FFmpegError, OSError, RuntimeError, ValueError) as error:
            counts['errors'] += 1
            failure = str(error)
            print(f'{position:04d} {error}', file=sys.stderr)
        finally:
            if failure is not None:
                failures[failure_key] = {'link_list': link_list, 'position': position, 'title': row.get('title'),
                                         'youtube_url': row['youtube_url'], 'stage': stage, 'error': failure}
                atomic_json(failure_file, list(failures.values()))
            elif completed and failures.pop(failure_key, None) is not None:
                atomic_json(failure_file, list(failures.values()))
    print(json.dumps(counts))
    return bool(counts['errors'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='command', required=True)
    links = subcommands.add_parser('links', help='재생목록에서 검색 주소 및 영상 후보 만들기')
    solos = subcommands.add_parser('solos', help='가창 분석용 솔로곡 목록과 영상 후보 만들기')
    for command in (links, solos):
        command.add_argument('output', type=Path)
        command.add_argument('--manifest', type=Path, default=MANIFEST)
        command.add_argument('--resolve', action='store_true', help='공개 검색 또는 설정된 YouTube Data API로 영상 후보를 찾습니다')
        command.add_argument('--yt-dlp', default=str(LOCAL_YT_DLP) if LOCAL_YT_DLP.is_file() else 'yt-dlp')
        command.add_argument('--retry-unmatched', action='store_true', help='영상 주소를 찾지 못한 곡을 다시 검색합니다')
        command.add_argument('--start', type=int, default=1)
        command.add_argument('--limit', type=int)
    solos.add_argument('--reuse', type=Path, default=PLAYLIST_LINKS, help='기존 재생목록 링크 JSON')
    solos.add_argument('--mp3-dir', type=Path, default=PLAYLIST_MP3, help='이미 수집한 재생목록 MP3 폴더')
    solos.add_argument('--one-per-character', action='store_true', help='캐릭터별로 한 곡만 선택해 저장합니다')
    links.add_argument('--include-anniversary-reference', action='store_true',
                       help='다른 가창 버전의 참고 싱크가 있는 주년곡도 검색합니다')
    mp3 = subcommands.add_parser('mp3', help='개별 영상 링크 목록을 MP3로 변환')
    mp3.add_argument('links', type=Path)
    mp3.add_argument('output', type=Path)
    mp3.add_argument('--yt-dlp', default='yt-dlp')
    mp3.add_argument('--node', default=shutil.which('node'))
    mp3.add_argument('--include-spotify-synced', action='store_true', help='가창 분석용으로 Spotify 싱크가 있는 곡도 포함')
    mp3.add_argument('--include-anniversary-reference', action='store_true',
                     help='다른 가창 버전의 참고 싱크가 있는 주년곡도 다운로드합니다')
    mp3.add_argument('--reuse-mp3-dir', type=Path, help='동일한 파일명의 MP3가 이미 있으면 다운로드 생략')
    args = parser.parse_args()
    try:
        if args.command in ('links', 'solos'):
            if args.start < 1 or (args.limit is not None and args.limit < 1):
                raise ValueError('--start와 --limit는 1 이상이어야 합니다.')
            if args.command == 'solos':
                protected = {path.resolve() for path in (args.reuse, PLAYLIST_LINKS, args.manifest)}
                report = args.output.with_name(f'{args.output.stem}.failures.json')
                if args.output.resolve() in protected or report.resolve() in protected:
                    raise ValueError('솔로곡 목록은 기존 링크 JSON 및 재생목록과 다른 경로에 저장하세요.')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with worker_lock(args.output.parent, filename=f'.{args.output.name}.lock'):
                return write_links(args)
        args.output.mkdir(parents=True, exist_ok=True)
        with worker_lock(args.output, filename='.playlist-youtube-mp3.lock'):
            return download_mp3s(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'{error}\n')
    except KeyboardInterrupt:
        parser.exit(130, '작업을 중단했습니다. 같은 명령으로 이어서 실행할 수 있습니다.\n')


if __name__ == '__main__':
    sys.exit(main())
