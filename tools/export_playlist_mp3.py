"""Match user-supplied local audio to a playlist and export MP3 copies."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_analysis import input_file
from local_analysis_worker import _reject_external_io
from local_library import LocalLibrary


def mp3_filename(position, title):
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)[:100].rstrip(' .')
    return f'{position:04d} {safe_title}.mp3'


def export_mp3(source, destination, track):
    import av

    with NamedTemporaryFile(dir=destination.parent, suffix='.mp3', delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with source.open('rb') as handle:
            with av.open(handle, io_open=_reject_external_io,
                         container_options={'protocol_whitelist': 'file'}) as original:
                if not original.streams.audio:
                    raise ValueError('오디오 스트림이 없습니다.')
                audio = original.streams.audio[0]
                if original.format.name == 'mp3' and audio.codec_context.name in ('mp3', 'mp3float'):
                    shutil.copyfile(source, temporary)
                else:
                    with av.open(str(temporary), 'w', format='mp3') as output:
                        output.metadata.update(title=track['title'], album=track.get('album') or '',
                                               artist=', '.join(track.get('artists') or ()))
                        encoded = output.add_stream('libmp3lame', rate=44100)
                        encoded.layout = 'mono' if audio.codec_context.channels == 1 else 'stereo'
                        encoded.bit_rate = 192000
                        resampler = av.AudioResampler(format='s16p', layout=encoded.layout, rate=44100)
                        for frame in original.decode(audio):
                            for sample in resampler.resample(frame):
                                output.mux(encoded.encode(sample))
                        for sample in resampler.resample(None):
                            output.mux(encoded.encode(sample))
                        output.mux(encoded.encode(None))
        if os.name == 'nt':
            temporary.rename(destination)
        else:
            os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def export_playlist(entries, source_folder, output_folder):
    from av import FFmpegError

    output_folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output_folder) as workspace:
        library = LocalLibrary(workspace)
        scan = library.scan(source_folder, persist=False)

    counts = {'exported': 0, 'existing': 0, 'unmatched': 0, 'ambiguous': 0, 'errors': 0}
    for position, entry in enumerate(entries, 1):
        track = entry.get('track') or {}
        title = track.get('title')
        if not title:
            counts['unmatched'] += 1
            continue
        destination = output_folder / mp3_filename(position, title)
        if destination.exists():
            counts['existing'] += 1
            continue
        matches = library.exact_recordings({**track, 'expected_duration': track.get('duration')})
        if len(matches) != 1:
            counts['ambiguous' if matches else 'unmatched'] += 1
            continue
        try:
            export_mp3(input_file(Path(matches[0]['path'])), destination, track)
            counts['exported'] += 1
        except FileExistsError:
            counts['existing'] += 1
        except (FFmpegError, OSError, ValueError) as error:
            counts['errors'] += 1
            print(f'{position:04d} {title}: {error}', file=sys.stderr)
    return {**counts, 'scan_errors': scan['errors'], 'scan_truncated': scan['truncated']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path, help='사용 권한이 있는 로컬 음원 폴더')
    parser.add_argument('output', type=Path, help='MP3를 저장할 폴더')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'data/playlist_lyrics/manifest.json')
    args = parser.parse_args()
    with args.manifest.open(encoding='utf-8-sig') as file:
        entries = json.load(file)['entries']
    counts = export_playlist(entries, args.sources, args.output)
    print(json.dumps(counts, ensure_ascii=False))
    return 1 if counts['errors'] or counts['scan_truncated'] else 0


if __name__ == '__main__':
    sys.exit(main())
