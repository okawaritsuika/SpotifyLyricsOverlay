"""Rebuild the selected official live-history snapshot; never discover dates.

Run from the project: python tools/build_live_catalog.py --output candidate.json
Review a candidate before replacing resources/live_catalog.json. The selected
events and performance dates below were checked against their date_source_url.
New events require an explicit source/date addition here. This is a maintenance
tool, not a promise of a complete or automatically updated event database.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tempfile
import os
import urllib.request
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_catalog import read_catalog  # noqa: E402


PORTAL = 'https://idolmaster-official.jp/news/'
GAKUEN_DATE = 'https://idolmaster-official.jp/live_event/gkmas_debutlivehajime/streaming/revival.php'
MILLION_SOURCE = 'https://www.lantis.jp/imas/release_LABX-38806.html'
MILLION_DATE = 'https://idolmaster-official.jp/live_event/million10th/information/act4.php'
SIDEM_SOURCE = 'https://lantis.jp/sidem/releaseinfo/release_LABX-8723.html'
SHINY_DATE = 'https://idolmaster-official.jp/live_event/shinycolors_6th/information/ff.php'
PYLON = 'https://pylonport.bandainamcomusiclive.co.jp/report/'
OFFICIAL_HOSTS = {'idolmaster-official.jp', 'www.lantis.jp', 'lantis.jp', 'columbia.jp',
                  'pylonport.bandainamcomusiclive.co.jp'}

# Explicitly reviewed associations, not an algorithm that strips any suffix.
# The source's complete stage arrangement remains in performance_title.
WORK_TITLES = {
    'KisS（Long Intro Ver.）': 'KisS',
    'なんどでも笑おう（Long Intro Ver.）': 'なんどでも笑おう',
    'Pon De Beach（Long Ver.）': 'Pon De Beach',
    'LEMONADE（Long Intro Ver.）': 'LEMONADE',
    '想い出は永遠に（Long Intro Ver.）': '想い出は永遠に',
    'Honey Heartbeat（Long Intro Ver.）': 'Honey Heartbeat',
    'クルリウタ（Long Intro ver.）': 'クルリウタ',
    '赤い世界が消える頃（Long Intro ver.）': '赤い世界が消える頃',
    'Crossing!（Long Intro ver.）': 'Crossing!',
    'Star Impression（Long Intro ver.）': 'Star Impression',
    '海風とカスタネット（Long Intro ver.）': '海風とカスタネット',
    'catch my feeling（Long Intro ver.）': 'catch my feeling',
    'ESPADA（Long Intro ver.）': 'ESPADA',
    'Spread the Wings!! (Come and Unite! Ver.)': 'Spread the Wings!!',
    "Let's get a chance (Come and Unite! Ver.)": "Let's get a chance",
    '素敵なキセキ（Long Intro Ver.）': '素敵なキセキ',
    'ハッピー☆ラッキー☆ジェットマシーン（Long Intro Ver.）': 'ハッピー☆ラッキー☆ジェットマシーン',
    'Blue Symphony（Long Intro Ver.）': 'Blue Symphony',
    'Crossing!（Long Intro Ver.）': 'Crossing!',
    'オルゴールの小箱（Shout out Arrange）': 'オルゴールの小箱',
    '純情Midnight伝説（Long Intro Ver.）': '純情Midnight伝説',
    '星環世界（Shout out Arrange）': '星環世界',
    'Next Chapter（Long Intro Ver.）': 'Next Chapter',
    'Love∞Destiny（Shout out Arrange）': 'Love∞Destiny',
    'ミライコンパス（Long Intro Ver.）': 'ミライコンパス',
    '認めてくれなくたっていいよ（Long Intro Ver.）': '認めてくれなくたっていいよ',
    'ミラーボール・ラブ（Shout out Arrange）': 'ミラーボール・ラブ',
    'アタシポンコツアンドロイド（Shout out Arrange）': 'アタシポンコツアンドロイド',
    'Drastic Melody（Long Intro Ver.）': 'Drastic Melody',
    'Trinity Field（Long Ver.）': 'Trinity Field',
    '生存本能ヴァルキュリア（Long Intro Ver.）': '生存本能ヴァルキュリア',
    'お願い！シンデレラ（和太鼓アレンジ）': 'お願い！シンデレラ',
}


def song_row(title, artists=None):
    title = re.sub(r'\s+', ' ', title).strip()
    row = {'title': title}
    if title in WORK_TITLES:
        row.update(title=WORK_TITLES[title], song_title=WORK_TITLES[title], performance_title=title)
    if artists:
        row['artists'] = [artist.strip() for artist in artists if artist.strip()]
        row['artist_basis'] = 'official_performance_credit'
    return row


def fetch_soup(url):
    if urlsplit(url).hostname not in OFFICIAL_HOSTS:
        raise ValueError('Only the explicitly listed official sources are supported.')
    request = urllib.request.Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay-live-catalog/1.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        if urlsplit(response.url).hostname not in OFFICIAL_HOSTS:
            raise ValueError('The source moved; inspect the replacement before updating.')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Source exceeded the expected document size.')
    return BeautifulSoup(raw, 'html.parser')


def numbered(soup, prefix=''):
    rows = []
    separator = r'\s*[.．]\s*' if not prefix else r'\s+'
    pattern = re.compile(r'^' + prefix + r'(\d+)' + separator + r'(.+)$')
    for line in soup.get_text('\n', strip=True).splitlines():
        match = pattern.match(line)
        if match:
            rows.append((int(match[1]), song_row(match[2])))
    return rows


def expect(rows, count):
    if len(rows) != count or [n for n, _ in rows] != list(range(1, count + 1)):
        raise ValueError(f'Setlist changed or could not be read: expected {count}, got {len(rows)}.')
    return [song for _, song in rows]


def setlist_rows(text, credits=False):
    """Read reviewed list formats, excluding years, times and prices."""
    rows = []
    for line in text.splitlines():
        match = re.match(r'^(?:M)?(\d{1,3})(?:[.．]\s*|\s+)(.+)$', line.strip())
        if not match:
            continue
        title = match[2]
        artists = None
        if credits:
            parts = re.split(r'\s+/\s*|／', title, maxsplit=1)
            title = parts[0]
            # Preserve published credit as one credit if punctuation is ambiguous.
            artists = [parts[1]] if len(parts) == 2 else None
        rows.append((int(match[1]), song_row(title, artists)))
    return rows


def pylon_setlist(soup, day, count):
    pattern = re.compile(rf'^■?\s*DAY{day}(?:\s+.*Set List)?$', re.I)
    heading = next((h for h in soup.find_all(['h3', 'h4'])
                    if pattern.fullmatch(h.get_text(' ', strip=True))), None)
    if heading is None:
        raise ValueError(f'Missing reviewed DAY{day} setlist heading')
    parts = []
    for sibling in heading.next_siblings:
        if getattr(sibling, 'name', None) in ('h2', 'h3', 'h4'):
            break
        if hasattr(sibling, 'get_text'):
            parts.append(sibling.get_text('\n', strip=True))
    return expect(setlist_rows('\n'.join(parts), credits=True), count)


def collect_expansion(fetch):
    """Additional sources reviewed on 2026-10-02; dates are performance dates."""
    events = []
    # Each PYLON report is published by Bandai Namco Music Live. Its numbered
    # text setlist is used, not the accompanying streaming playlist (which can
    # omit unreleased songs). Expected lengths fail closed if the layout changes.
    reports = [
        ('185', 'shiny-7th-first', 'SHINY COLORS 7th UNITLIVE TOUR 円環 -Halo around- first quadrant',
         'shiny', '福岡国際センター', [(1, '2025-06-21', 20), (2, '2025-06-22', 20)]),
        ('194', 'shiny-7th-second', 'SHINY COLORS 7th UNITLIVE TOUR 円環 -Halo around- second quadrant',
         'shiny', '横浜BUNTAI', [(1, '2025-07-05', 20), (2, '2025-07-06', 21)]),
        ('384', 'shiny-7th-spiral', 'SHINY COLORS 7th LIVE TOUR 螺旋 -Halo around- “The origin on the axes”',
         'shiny', 'Kアリーナ横浜', [(1, '2025-10-25', 28), (2, '2025-10-26', 28)]),
        ('609', 'shiny-8th', 'SHINY COLORS ∞th LIVE iと夢',
         'shiny', 'Kアリーナ横浜', [(1, '2026-04-25', 32), (2, '2026-04-26', 32)]),
        ('629', 'million-13th', 'MILLION LIVE! 13thLIVE 全力援走',
         'million', '有明アリーナ', [(1, '2026-05-05', 23)]),
        ('630', 'million-13th', 'MILLION LIVE! 13thLIVE Grand bal masqué',
         'million', '有明アリーナ', [(2, '2026-05-06', 23)]),
        ('842', 'sidem-11th', 'SideM 11th STAGE ～EVER EVER＠FTER～',
         'sidem', 'TOYOTA ARENA TOKYO', [(1, '2026-09-12', 34)]),
        ('843', 'sidem-11th', 'SideM 11th STAGE ～EVER EVER＠FTER～',
         'sidem', 'TOYOTA ARENA TOKYO', [(2, '2026-09-13', 38)]),
    ]
    for report, identifier, title, series, venue, days in reports:
        source = PYLON + report
        soup = fetch(source)
        for day, stamp, count in days:
            entry = event(f'{identifier}-day{day}', f'{title} DAY{day}', stamp,
                          series, source, pylon_setlist(soup, day, count), venue=venue)
            entry['source_name'] = '반다이남코 뮤직라이브 PYLON PORT 공식 세트리스트'
            if report == '185':
                # The report's introductory paragraph repeats 21 by mistake.
                # The organizer's schedule independently confirms DAY2 is 22.
                entry['date_source_url'] = ('https://idolmaster-official.jp/live_event/'
                                            'shinycolors_7th/information/first.php')
            events.append(entry)

    portal_events = [
        ('01_8391', 'million-10th-act1-day1', 'MILLION LIVE! 10thLIVE TOUR Act-1 H@PPY 4 YOU! DAY1',
         '2023-04-22', 'million', 30,
         'https://idolmaster-official.jp/live_event/million10th/information/act1.php'),
        ('01_8750', 'cinderella-yomatsuri-day1', 'CINDERELLA GIRLS 燿城夜祭 -かがやきよまつり- DAY1',
         '2023-06-10', 'cinderella', 27, PORTAL + '01_8750'),
        ('01_9420', 'cinderella-shout-day2', 'CINDERELLA GIRLS Shout out Live!!! DAY2',
         '2023-09-10', 'cinderella', 28, PORTAL + '01_9420'),
        ('01_16365', '765-never-end-day1', '765PRO ALLSTARS LIVE ～NEVER END IDOL!!!!!!!!!!!!!～ DAY1',
         '2025-08-02', '765', 36,
         'https://idolmaster-official.jp/live_event/765_never_end_idol/information/'),
    ]
    for article, identifier, title, stamp, series, count, date_source in portal_events:
        source = PORTAL + article
        soup = fetch(source)
        songs = expect(setlist_rows(soup.get_text('\n', strip=True)), count)
        entry = event(identifier, title, stamp, series, source, songs)
        entry['date_source_url'] = date_source
        if series == '765':
            entry['note'] = '공식 메들리 표기를 유지했습니다. 복합 곡목은 개별 곡으로 추측하여 나누지 않았습니다.'
        events.append(entry)

    # Visually transcribed from official images; no automatic OCR or guessed
    # artist credit. Image dates also agree with the organizer's event schedule.
    image_events = [
        ('01_15495', 'spotlight', '2025-05-24', 'Spotlight Star', 'nZWGW4gwgUV2ZqC0tClsBwQ65hnGmJKn.jpeg',
         ['白線', 'Luna say maybe', 'Tame-Lie-One-Step', 'Fluorite', 'clumsy trick', '光景',
          '世界一可愛い私', 'The Rolling Riceball', 'Fighting My Way', 'Wonder Scale', 'ツキノカメ',
          '小さな野望', '初', 'たいせつなもの', 'コンテンポラリのダンス', '歌声は君いろ',
          '憧れをいっぱい', 'Kira Kira', 'Cosmetic', 'EGO', 'Wake up!!', 'Sweet Magic', 'ふわふわ',
          'Unhappy Light', 'Superlative', '古今東西ちょちょいのちょい', '標', 'Campus mode!!',
          'Star-mine', '雨上がりのアイリス']),
        ('01_15498', 'harmony', '2025-05-31', 'Harmony Star', 'vynvUwIIreYSWlQUgjBIvVS549amc4JG.jpeg',
         ['雨上がりのアイリス', 'キミとセミブルー', 'アイヴイ', '光景', '世界一可愛い私',
          'White Night! White Wish!', 'みちなるひろがる', 'clumsy trick', 'Yellow Big Bang！',
          'ENDLESS DANCE', '小さな野望', 'Feel Jewel Dream', 'ツキノカメ', 'SUGAR FLAVOR', '白線',
          'Wonder Scale', 'グースーピー', 'Boom Boom Pow', 'Fluorite', 'Tame-Lie-One-Step',
          'コントラスト', 'The Rolling Riceball', 'Fighting My Way', 'Luna say maybe', 'Star-mine',
          '仮装狂騒曲', '標', '初', '冠菊', 'ミラクルナナウ(ﾟ∀ﾟ)！', 'Campus mode!!']),
    ]
    for article, identifier, stamp, title, image_file, titles in image_events:
        source = PORTAL + article
        entry = event(f'gakuen-1st-{identifier}-day1', f'学園アイドルマスター The 1st Period {title} DAY1',
                      stamp, 'gakuen', source, [song_row(title) for title in titles], venue='立川ステージガーデン')
        entry.update(date_source_url='https://idolmaster-official.jp/live_event/gkmas_1stperiod/information/',
                     setlist_source_url=('https://cmsapi-frontend.idolmaster-official.jp/sitern/api/idolmaster/Image/get'
                                         f'?path=/idolmaster/jp/article/012/2025/05/{image_file}'),
                     note='공식 세트리스트 이미지의 곡명·날짜를 2026-10-02 확인했습니다.')
        events.append(entry)
    return events


def event(event_id, title, date, series, source, songs, **extra):
    return dict(id=event_id, title=title, date=date, series=[series], source_url=source,
                source_name='아이돌마스터 공식 포털', date_source_url=source,
                setlist_source_url=source, setlist_coverage='official_setlist',
                songs=songs, **extra)


def collect(fetch=fetch_soup):
    events = []
    # Official day reports provide the actual performance date, not video release dates.
    for day, identifier, stamp in [(1, '01_4278.html', '2022-07-09'), (2, '01_4279.html', '2022-07-10')]:
        source = PORTAL + identifier
        soup = fetch(source)
        rows = [(n, row) for n, row in numbered(soup) if n <= 24]
        events.append(event(f'765-sunrich-day{day}', f'765PRO ALLSTARS LIVE SUNRICH COLORFUL DAY{day}',
                            stamp, '765', source, expect(rows, 24)))

    for day, stamp, short_date, count in [(1, '2022-04-02', '220402', 51), (2, '2022-04-03', '220403', 50)]:
        source = f'https://columbia.jp/idolmaster/imasnews/{short_date}setlist.html'
        soup = fetch(source)
        rows = []
        for table_row in soup.find_all('tr'):
            cell = table_row.find('td')
            if cell:
                match = re.match(r'^(\d+)\.(.+)', cell.get_text(' ', strip=True))
                if match:
                    rows.append((int(match[1]), song_row(match[2])))
        entry = event(f'cinderella-magical-day{day}',
                      f'CINDERELLA GIRLS 10th ANNIVERSARY M@GICAL WONDERLAND!!! DAY{day}',
                      stamp, 'cinderella', source, expect(rows, count), venue='ベルーナドーム')
        entry['source_name'] = '일본 컬럼비아 공식 세트리스트'
        events.append(entry)

    # The published video track list contains MC and narration: skip those tracks.
    text = fetch(MILLION_SOURCE).get_text('\n', strip=True)
    for day, stamp, end_marker, expected in [(1, '2024-02-24', '■【DAY2】', 44),
                                               (2, '2024-02-25', '【SPECIAL DISC】', 44)]:
        section = text.split(f'■【DAY{day}】', 1)[1].split(end_marker, 1)[0]
        songs = []
        for line in section.splitlines():
            match = re.match(r'^\d+\.\s*(.+?)\s*\[(.+)\]$', line)
            if not match or any(word in match[1] for word in ('ご挨拶', 'ナレーション')):
                continue
            songs.append(song_row(match[1], match[2].split('/')))
        if len(songs) != expected:
            raise ValueError(f'Million DAY{day}: expected {expected} song tracks, got {len(songs)}')
        entry = event(f'million-10th-act4-day{day}',
                      f'MILLION LIVE! 10thLIVE TOUR Act-4 MILLION THE@TER!!!! DAY{day}',
                      stamp, 'million', MILLION_SOURCE, songs, venue='Kアリーナ横浜')
        entry.update(date_source_url=MILLION_DATE, source_name='Lantis 공식 공연 영상 트랙리스트',
                     setlist_coverage='official_video_tracks')
        events.append(entry)

    text = fetch(SIDEM_SOURCE).get_text('\n', strip=True)
    for day, stamp, expected in [(1, '2023-10-28', 23), (2, '2023-10-29', 32)]:
        section = text.split(f'＜DAY{day}＞', 1)[1].split('【EXTRA】', 1)[0]
        songs = []
        for line in section.splitlines():
            line = line.strip()
            if (not line or line.startswith('【DISC') or line in ('OPENING', 'MC', 'ENDING')
                    or line == 'みんなで歌おう！SideMメドレー'):
                continue
            if line == 'CALLING[神速一魂]':
                songs.append(song_row('CALLING', ['神速一魂']))
            else:
                songs.append(song_row(line))
        if len(songs) != expected:
            raise ValueError(f'SideM DAY{day}: expected {expected} song tracks, got {len(songs)}')
        entry = event(f'sidem-8th-day{day}', f'SideM 8th STAGE ～ALL H@NDS TOGETHER～ DAY{day}',
                      stamp, 'sidem', SIDEM_SOURCE, songs, venue='Kアリーナ横浜',
                      note='공식 영상 곡목 기준. 개별 곡명이 없는 메들리는 포함하지 않았습니다.')
        entry.update(source_name='Lantis 공식 공연 영상 트랙리스트', setlist_coverage='official_video_tracks_partial')
        events.append(entry)

    for day, identifier, stamp in [(1, '01_11277.html', '2024-04-20'), (2, '01_11290', '2024-04-21')]:
        source = PORTAL + identifier
        songs = expect(numbered(fetch(source), 'M'), 34)
        entry = event(f'shiny-6th-ff-day{day}',
                      f'SHINY COLORS 6thLIVE TOUR Come and Unite! Fantastic Fireworks DAY{day}',
                      stamp, 'shiny', source, songs, venue='Kアリーナ横浜')
        entry['date_source_url'] = SHINY_DATE
        events.append(entry)

    # The official Gakuen setlists are images, visually checked on 2026-10-02.
    # Do not OCR/guess future replacements or advertise automatic image updates.
    titles = ['初', 'Campus mode!!', 'Fighting My Way', 'Luna say maybe', '世界一可愛い私',
              'Boom Boom Pow', 'Yellow Big Bang！', 'アイヴイ', 'キミとセミブルー']
    for stamp, place, suffix, tail, image_file in [
        ('2024-08-10', '名古屋クラブクアトロ', '', ['EGO', 'がむしゃらに行こう！'], 'vW6YTmGlbBrITkgVaXlu6lu99gYl8tVt.jpeg'),
        ('2024-08-18', '梅田クラブクアトロ', ' 昼の部', ['Unhappy Light', 'Howling over the World'], 'KKxSTSKcTr4cv02k4xra6Ezvl34OfL4c.jpeg')]:
        source = PORTAL + '01_14540'
        entry = event(f'gakuen-debut-{stamp.replace("-", "")}',
                      f'学園アイドルマスター DEBUT LIVE 初 TOUR 初声公演 {place}{suffix}',
                      stamp, 'gakuen', source, [song_row(title) for title in titles + tail + ['冠菊', '初']],
                      venue=place)
        entry.update(date_source_url=GAKUEN_DATE,
                     setlist_source_url=('https://cmsapi-frontend.idolmaster-official.jp/sitern/api/idolmaster/Image/get'
                                         f'?path=/idolmaster/jp/article/1007/2025/02/{image_file}'),
                     note='공식 세트리스트 이미지의 곡명을 2026-10-02 확인했습니다.')
        events.append(entry)

    events.extend(collect_expansion(fetch))
    verified_at = '2026-10-02'
    return dict(schema_version=1, coverage='partial', verified_at=verified_at,
                generated_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                note='공식 자료로 확인한 일부 공연. 누락은 미확인이며 미공연을 뜻하지 않습니다.',
                association_note='곡명과 시리즈 기준 기록이며 특정 녹음·가창 버전의 공연을 보증하지 않습니다.',
                maintenance='빌드 도구는 선정된 출처의 텍스트 곡목만 다시 읽습니다. 신규 공연·공연일·이미지는 별도 검증이 필요합니다.',
                events=sorted(events, key=lambda item: (item['date'], item['id']), reverse=True))


def write_candidate(value, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json',
                                         dir=destination.parent, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        if read_catalog(name) is None:
            raise ValueError('Invalid live catalog; the previous file was preserved.')
        os.replace(name, destination)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'resources' / 'live_catalog.candidate.json')
    args = parser.parse_args()
    value = collect()
    write_candidate(value, args.output)
    print(json.dumps({'events': len(value['events']), 'setlist_entries': sum(len(e['songs']) for e in value['events']),
                      'coverage': value['coverage'], 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
