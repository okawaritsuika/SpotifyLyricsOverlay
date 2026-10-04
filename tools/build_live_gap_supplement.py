"""Recheck seven explicitly reviewed official performances missing from the DB.

Writes a candidate only. Each Cinderella day has its own official retrospective;
the 8th Makuhari list describes only 2013-09-22, never the preceding day.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_catalog import read_catalog, title_key
from tools.build_live_catalog import event, expect, song_row, write_candidate

CINDERELLA = 'https://cinderella-library.idolmaster-official.jp/history/detail/?p='
MAKUHARI = 'https://columbia.jp/idolmaster/XT-3324.html'
MAKUHARI_DATE = 'https://columbia.jp/idolmaster/imasnews/131213.html'
MILLION_SONGS = 'https://www.lantis.jp/imas/release_LACA-9398.html'
MAKUHARI_MILLION_TITLES = {
    17: 'キラメキ進行形', 19: 'プライヴェイト・ロードショウ（playback, Weekday）',
    24: 'Rebellion', 25: 'Snow White',
}
SHOWS = (
    ('2017-05-13', 'miyagi', 1, '宮城', '宮城・セキスイハイムスーパーアリーナ'),
    ('2017-05-14', 'miyagi', 2, '宮城', '宮城・セキスイハイムスーパーアリーナ'),
    ('2017-06-24', 'shizuoka', 1, '静岡', '静岡・エコパアリーナ'),
    ('2017-06-25', 'shizuoka', 2, '静岡', '静岡・エコパアリーナ'),
    ('2017-07-08', 'makuhari', 1, '幕張', '千葉・幕張メッセ イベントホール'),
    ('2017-07-09', 'makuhari', 2, '幕張', '千葉・幕張メッセ イベントホール'),
)
SOURCE_URLS = tuple(CINDERELLA + stamp.replace('-', '') + '_1' for stamp, *_ in SHOWS) + (MAKUHARI, MAKUHARI_DATE, MILLION_SONGS)


def cinderella_event(soup, spec):
    stamp, city_id, day, city, venue = spec
    nodes = soup.select('.description')
    if len(nodes) != 1:
        raise ValueError('Official retrospective description changed')
    text = nodes[0].get_text('\n', strip=True)
    title = f'THE IDOLM@STER CINDERELLA GIRLS 5thLIVE TOUR Serendipity Parade!!! {city}公演'
    if title not in text:
        raise ValueError('Official retrospective event title changed')
    lines = text.splitlines()
    schedule = [lines[i + 1] for i, line in enumerate(lines[:-1]) if line == '■開催日時']
    expected_date = date.fromisoformat(stamp)
    printed = f'{expected_date.year}年{expected_date.month}月{expected_date.day}日'
    if len(schedule) != 1 or not schedule[0].startswith(printed + ' '):
        raise ValueError('Official retrospective performance date changed')
    places = [lines[i + 1] for i, line in enumerate(lines[:-1]) if line == '■開催場所']
    if len(places) != 1 or re.sub(r'\s+', ' ', places[0]) != venue:
        raise ValueError('Official retrospective venue changed')
    if text.count('■セットリスト') != 1:
        raise ValueError('Official retrospective setlist heading changed')
    tracks = text.split('■セットリスト', 1)[1].strip().splitlines()
    if len(tracks) != 48:
        raise ValueError('Official retrospective title/credit row count changed')
    rows = []
    for index in range(0, len(tracks), 2):
        match = re.fullmatch(r'M(\d+)\.(.+)', tracks[index])
        credit = tracks[index + 1]
        if not match or not credit or re.match(r'M\d+\.', credit):
            raise ValueError('Official retrospective song or credit missing')
        # Keep the source's performer/role pairing, without inventing a studio
        # recording credit from the performer or expanding an all-cast label.
        rows.append((int(match[1]), song_row(match[2], credit.split('、'))))
    source = CINDERELLA + stamp.replace('-', '') + '_1'
    value = event(f'cinderella-5th-{city_id}-day{day}', title + f' DAY{day}',
                  stamp, 'cinderella', source, expect(rows, 24), venue=venue)
    value.update(source_kind='official', source_name='신데렐라 라이브러리 공식 공연 기록',
                 setlist_coverage='official_performance_setlist',
                 note='해당 날짜의 공식 공연 기록을 개별 확인했습니다. 다른 날짜의 곡목을 복사하지 않았습니다.')
    return value


def makuhari_event(soup, date_soup):
    if '2013年9月22日幕張イベントホール' not in date_soup.get_text('', strip=True):
        raise ValueError('Official Makuhari performance date changed')
    headings = [h for h in soup.find_all('h3') if 'COXC-1072' in h.get_text()]
    if len(headings) != 1:
        raise ValueError('Official Makuhari product section changed')
    # Select only lists associated with this exact product heading. The page
    # also has a Yokohama show and DAY1 digest descriptions.
    lists = [node for node in soup.select('ol.hopstep') if node.find_previous('h3') is headings[0]]
    if len(lists) != 2:
        raise ValueError('Official Makuhari list structure changed')
    rows = []
    for node in lists:
        start = int(node.get('start', 1))
        for position, item in enumerate(node.find_all('li', recursive=False), start):
            heading = item.find('h4')
            full = item.get_text(' ', strip=True)
            if heading is None or full.count('歌：') != 1:
                raise ValueError('Official Makuhari title/credit missing')
            credit = full.split('歌：', 1)[1]
            row = song_row(heading.get_text(' ', strip=True), credit.split('、'))
            if position in MAKUHARI_MILLION_TITLES and row['title'] != MAKUHARI_MILLION_TITLES[position]:
                raise ValueError('Official Makuhari Million song identity changed')
            # These four 765-cast solos originate in Million Live. Identify the
            # song's series from Lantis, not from the actor's stage affiliation.
            row['series'] = (['cinderella'] if position in (10, 11, 13) else
                             ['million'] if position in (12, 14, 15, *MAKUHARI_MILLION_TITLES) else
                             ['other'] if position == 26 else ['765'])
            if position in MAKUHARI_MILLION_TITLES:
                row['series_source_url'] = MILLION_SONGS
            if position == 29:
                # The official source abbreviates 下田 here. Preserve that
                # original text instead of silently completing a person's name.
                row['note'] = '공식 원문에서 가창자 한 명은 下田로만 표기됩니다.'
            rows.append((position, row))
    songs = expect(rows, 30)
    if songs[0]['title'] != 'READY!!(M@STER VERSION)' or songs[-1]['title'] != 'THE IDOLM@STER(M@STER VERSION)':
        raise ValueError('Official Makuhari setlist boundary changed')
    value = event('765-8th-makuhari-day2',
                  'THE IDOLM@STER 8th ANNIVERSARY HOP!STEP!!FESTIV@L!!! @MAKUHARI0922',
                  '2013-09-22', '765', MAKUHARI, songs, venue='幕張イベントホール')
    value.update(series=['765', 'cinderella', 'million', 'other'], source_kind='official',
                 date_source_url=MAKUHARI_DATE, source_name='일본 컬럼비아 공식 공연 영상 곡목',
                 official_sources=[{'name': 'Lantis 공식 밀리언라이브 곡 분류', 'url': MILLION_SONGS}],
                 setlist_coverage='official_video_tracks',
                 note='9월 22일 공연 영상의 30곡입니다. 9월 21일 공연은 이 목록으로 추정하지 않습니다.')
    return value


def collect(fetch):
    song_evidence = title_key(fetch(MILLION_SONGS).get_text(' ', strip=True))
    if ('アイドルマスターミリオンライブ!' not in song_evidence
            or any(title_key(title) not in song_evidence for title in MAKUHARI_MILLION_TITLES.values())):
        raise ValueError('Official Million song evidence changed')
    events = [cinderella_event(fetch(CINDERELLA + spec[0].replace('-', '') + '_1'), spec)
              for spec in SHOWS]
    events.append(makuhari_event(fetch(MAKUHARI), fetch(MAKUHARI_DATE)))
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return dict(schema_version=1, coverage='partial', verified_at=now, generated_at=now,
                note='기존 보류 문서의 공연을 독립 공식 출처에서 개별 확인한 보충 자료입니다.', events=events)


def merge(base, supplement):
    value = deepcopy(base)
    events = {item['id']: item for item in value['events']}
    events.update({item['id']: deepcopy(item) for item in supplement['events']})
    value['events'] = sorted(events.values(), key=lambda item: (item['date'], item['id']), reverse=True)
    value['verified_at'] = value['generated_at'] = supplement['verified_at']
    report = value.setdefault('collection_report', {})
    report.update(events=len(events), setlist_entries=sum(len(e['songs']) for e in events.values()))
    # Existing fan-source success/failure counters still describe that import.
    report['official_gap_supplement'] = {
        'events': len(supplement['events']),
        'setlist_entries': sum(len(e['songs']) for e in supplement['events']),
        'source_urls': list(SOURCE_URLS), 'verified_at': supplement['verified_at'],
    }
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', type=Path, help='Optional existing snapshot; output remains a separate candidate')
    args = parser.parse_args()
    if args.base and args.base.resolve() == args.output.resolve():
        raise ValueError('Candidate output must not overwrite the supplied base')
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    def fetch(url):
        if url not in SOURCE_URLS:
            raise ValueError('Unreviewed source URL')
        path = args.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + '.html')
        if not path.exists():
            request = Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay-source-review/1.0'})
            with urlopen(request, timeout=25) as response:
                if response.url != url:
                    raise ValueError('Official source redirected; review the new destination')
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('Official source exceeded size limit')
            path.write_bytes(raw)
            time.sleep(1)
        return BeautifulSoup(path.read_bytes(), 'html.parser')
    value = collect(fetch)
    if args.base:
        base = read_catalog(args.base)
        if base is None:
            raise ValueError('Invalid base catalog')
        value = merge(base, value)
    write_candidate(value, args.output)
    print(json.dumps({'events': len(value['events']), 'setlist_entries': sum(len(e['songs']) for e in value['events']),
                      'output': str(args.output)}))


if __name__ == '__main__':
    main()
