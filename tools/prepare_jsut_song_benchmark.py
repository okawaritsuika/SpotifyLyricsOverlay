"""Create a local benchmark from the official JSUT-song WAV and HTS label ZIPs."""
import argparse
import json
from pathlib import Path
import re
from zipfile import ZipFile


# The corpus labels the sung pronunciation, including breath boundaries and
# vowel extensions. Render those phones as kana so all engines get the same text.
KANA = dict(pair.split('=') for pair in (
    'N=ん a=あ ba=ば be=べ bi=び bo=ぼ bu=ぶ cha=ちゃ chi=ち cho=ちょ chu=ちゅ cl=っ '
    'da=だ de=で do=ど e=え fu=ふ ga=が ge=げ gi=ぎ go=ご gu=ぐ ha=は hi=ひ ho=ほ i=い '
    'ja=じゃ ji=じ ju=じゅ ka=か ke=け ki=き ko=こ ku=く kyo=きょ ma=ま me=め mi=み mo=も mu=む '
    'na=な ne=ね ni=に no=の nu=ぬ nya=にゃ o=お pa=ぱ pi=ぴ po=ぽ pu=ぷ pyo=ぴょ '
    'ra=ら re=れ ri=り ro=ろ ru=る rya=りゃ sa=さ se=せ sha=しゃ shi=し sho=しょ shu=しゅ so=そ su=す '
    'ta=た te=て to=と tsu=つ tyu=てゅ u=う wa=わ ya=や yo=よ yu=ゆ za=ざ ze=ぜ zo=ぞ zu=ず'
).split())
LABEL = re.compile(r'^([0-9]+) [0-9]+ \S+?\^\w+-([^+]+)\+')


def transcribe(data):
    lines, times = [], []
    start = None
    phones = ''
    phrase = []
    for line in data.splitlines():
        match = LABEL.match(line)
        if match is None:
            raise ValueError('Malformed HTS label row')
        onset, phone = match.groups()
        if phone in ('pau', 'sil', 'br'):
            if phones:
                raise ValueError('Incomplete mora at a phrase boundary')
            if phrase:
                lines.append(''.join(phrase))
                times.append(start)
                start, phrase = None, []
            continue
        if start is None:
            start = int(onset) / 10_000_000
        phones += phone
        if phone in ('a', 'e', 'i', 'o', 'u', 'N', 'cl'):
            phrase.append(KANA[phones])
            phones = ''
    if phones:
        raise ValueError('Incomplete final mora')
    if phrase:
        lines.append(''.join(phrase))
        times.append(start)
    return lines, times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio_zip', type=Path)
    parser.add_argument('label_zip', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    audio_dir = args.destination / 'wav'
    audio_dir.mkdir(parents=True, exist_ok=True)
    tracks = []
    excluded = []
    with ZipFile(args.audio_zip) as audio, ZipFile(args.label_zip) as labels:
        known_labels = set(labels.namelist())
        for member in sorted(audio.namelist()):
            match = re.fullmatch(r'jsut-song_ver1/child_song/wav/([0-9]+)\.wav', member)
            if match is None:
                continue
            song = match[1]
            label = f'todai_child/{song}.lab'
            if label not in known_labels:
                excluded.append({'song_id': song, 'reason': 'No official HTS label'})
                continue
            lines, times = transcribe(labels.read(label).decode('ascii'))
            filename = f'{song}.wav'
            (audio_dir / filename).write_bytes(audio.read(member))
            tracks.append({'song_id': song, 'split': 'evaluation', 'audio': f'wav/{filename}',
                           'lines': lines, 'reference_times': times,
                           'reference_source': f'JSUT-song HTS labels, {label}'})
    path = args.destination / 'manifest.json'
    path.write_text(json.dumps({'tracks': tracks, 'excluded': excluded}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(tracks)} labeled songs, {len(excluded)} without labels: {path}')


if __name__ == '__main__':
    main()
