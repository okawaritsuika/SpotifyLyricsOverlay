"""Verify local playlist MP3 metadata and prepare a solo enrollment manifest."""
import argparse
import json
from pathlib import Path

from export_playlist_mp3 import ROOT, mp3_filename
from playlist_youtube_mp3 import PLAYLIST_MP3, solo_credit, video_id

from artist_credit_overrides import ArtistCreditOverrides
from group_credits import RESOURCE_PATH, _catalog
from local_analysis import input_file
from local_library import _key
from storage import atomic_json


def verified_inputs(links, folder, data, backend, positions=()):
    import mutagen

    credits = _catalog(RESOURCE_PATH)
    if credits is None:
        raise ValueError('캐릭터 크레딧 카탈로그를 읽을 수 없습니다.')
    overrides = ArtistCreditOverrides(data)
    for row in links['entries']:
        position = row['position']
        if positions and position not in positions:
            continue
        path = folder / mp3_filename(position, row['title'])
        if not row.get('local_mp3') and not path.is_file():
            continue
        credit = solo_credit(row, credits, overrides)
        url = row.get('youtube_url')
        sources = [candidate for candidate in row.get('candidates', [])
                   if url and candidate.get('url') == url and candidate.get('metadata_match') is True
                   and (candidate.get('official_channel') is True
                        or candidate.get('channel') == '日本コロムビア 公式YouTubeチャンネル')]
        try:
            if credit is None or credit != row.get('solo_credit') or not sources:
                raise ValueError('솔로 크레딧 또는 음원 출처를 확인할 수 없습니다.')
            video_id(url)
            path = input_file(path)
            audio = mutagen.File(path, easy=True)
            if audio is None or abs(audio.info.length - row['duration']) > 2:
                raise ValueError('음원과 곡 길이가 일치하지 않습니다.')
            expected = {'title': [row['title']], 'album': [row['album']], 'artist': row['artists']}
            if any([_key(value) for value in audio.get(tag, [])] != [_key(value) for value in values]
                   for tag, values in expected.items()):
                raise ValueError('MP3 제목·앨범·가수 태그가 곡 목록과 일치하지 않습니다.')
        except (OSError, ValueError, mutagen.MutagenError) as error:
            yield None, {'position': position, 'error': str(error)}
            continue
        yield {'path': str(path), 'kind': 'solo', 'track_id': row['track_id'],
               'title': row['title'], 'album': row['album'], 'expected_duration': row['duration'],
               'character': {'id': credit['character_id'], 'name': credit['character'],
                             'actor': credit['voice_actor'] or ''},
               'source': url, 'recording_source': row['url'], 'evidence': 'verified_solo',
               'voice': {'singer_model': backend, 'device': 'auto'}}, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--links', type=Path, required=True)
    parser.add_argument('--audio-dir', type=Path, default=PLAYLIST_MP3)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=('sony', 'sony-contrastive', 'ecapa'), default='sony')
    parser.add_argument('--position', type=int, action='append', default=[])
    args = parser.parse_args()
    links = json.loads(args.links.read_text(encoding='utf-8-sig'))
    checked = list(verified_inputs(links, args.audio_dir.absolute(), args.data_dir, args.backend, args.position))
    inputs = [item for item, error in checked if item]
    errors = [error for item, error in checked if error]
    atomic_json(args.output, {'inputs': inputs, 'skipped': errors})
    print(json.dumps({'verified': len(inputs), 'skipped': len(errors)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
