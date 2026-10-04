"""Collect factual character/CV/color evidence from the unofficial imas-db.

Never copies biographies, lyrics or images. Existing character identities and
official colors remain authoritative; this file supplies optional evidence.
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
from storage import atomic_json

CV_URL = 'https://imas-db.jp/misc/cv.html'
COLOR_URL = 'https://imas-db.jp/misc/color.html'
USAGE_URL = 'https://info.imas-db.jp/about/reffer/'


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def fetch(url, cancel=None):
    if _cancelled(cancel):raise InterruptedError('인물 자료 갱신을 취소했습니다.')
    request = urllib.request.Request(url, headers={'User-Agent': 'SpotifyLyricsOverlay-source-review/1.0'})
    with urllib.request.urlopen(request, timeout=20) as response:
        if urlsplit(response.url).hostname != 'imas-db.jp':raise ValueError('Unexpected source redirect')
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:raise ValueError('Source is larger than expected')
    if _cancelled(cancel):raise InterruptedError('인물 자료 갱신을 취소했습니다.')
    return body


def parse_people(cv_html, color_html):
    entries = {}
    def person(node):
        identifier = node.get('data-character-id')
        name = node.get_text(' ', strip=True)
        if not identifier or not name:return None
        return entries.setdefault(identifier, {'name': name, 'source_character_id': identifier,
            'source_brand_id': node.get('data-brand-id', ''), 'voice_actor_history': [],
            'color_candidates': [], 'source_urls': []})
    cv = BeautifulSoup(cv_html, 'html.parser')
    for row in cv.find_all('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:continue
        actor = cells[0].find('rb')
        if actor is None:continue
        actor_name = actor.get_text(' ', strip=True)
        for node in cells[1].select('.idol-name[data-character-id]'):
            entry = person(node)
            if entry is None:continue
            # data-person-id/title describe the character's current default CV,
            # even on old-CV and XENOGLOSSIA rows. The row heading is the credit.
            credit = {'name': actor_name, 'source_url': CV_URL}
            following = node.find_next_sibling()
            if following is not None and following.get('data-brand-id') == '2' and not following.get('data-character-id'):
                credit['role_context'] = following.get_text(' ', strip=True)
            if following is not None and following.name == 'small' and 'date' in following.get('class', []):
                period = following.get_text('', strip=True)
                match = re.fullmatch(r'\((\d{4}-\d{2}-\d{2})?[〜～~](\d{4}-\d{2}-\d{2})?\)', period)
                if not match:raise ValueError('Unrecognized voice-actor date range: ' + period)
                for label, stamp in [('from', match[1]), ('until', match[2])]:
                    if stamp:date.fromisoformat(stamp);credit[label] = stamp
            if credit not in entry['voice_actor_history']:entry['voice_actor_history'].append(credit)
            if CV_URL not in entry['source_urls']:entry['source_urls'].append(CV_URL)
    colors = BeautifulSoup(color_html, 'html.parser')
    by_name = {}
    for entry in entries.values():by_name.setdefault(entry['name'], []).append(entry)
    for table in colors.find_all('table'):
        heading = table.find('thead')
        if heading is None or 'アイドル' not in heading.get_text():continue
        for row in table.select('tbody tr'):
            # Unit headings use th, whereas character names use td.
            cells = row.find_all('td', recursive=False)
            if len(cells) < 2 or row.find('th', recursive=False):continue
            label = cells[0].get_text(' ', strip=True)
            name = re.sub(r'\([^()]*\)$', '', label).strip()
            # The source explicitly lists the twins in one shared color row.
            names = ['双海亜美', '双海真美'] if name == '双海亜美/真美' else [name]
            candidates = []
            for column, cell in enumerate(cells[1:], 1):
                for code in dict.fromkeys(re.findall(r'#[0-9a-fA-F]{6}', cell.get_text(' ', strip=True))):
                    candidate = {'color': code.lower(), 'column_index': column, 'source_url': COLOR_URL}
                    if candidate not in candidates:candidates.append(candidate)
            if not candidates:continue
            primary_column = min(item['column_index'] for item in candidates)
            primary = {item['color'] for item in candidates if item['column_index'] == primary_column}
            for name in names:
                owners = by_name.get(name, [])
                if len(owners) > 1:continue
                if owners:entry = owners[0]
                else:
                    entry = {'name': name, 'source_character_id': '', 'source_brand_id': '',
                             'voice_actor_history': [], 'color_candidates': [], 'source_urls': []}
                    entries['color:' + name] = entry
                    by_name[name] = [entry]
                entry['color_source_label'] = label
                for candidate in candidates:
                    if candidate not in entry['color_candidates']:entry['color_candidates'].append(candidate)
                if len(primary) == 1:
                    value = next(iter(primary))
                    previous = entry.get('color')
                    if previous and previous != value:
                        entry.pop('color', None);entry['color_conflict'] = True
                    elif not entry.get('color_conflict'):entry['color'] = value
                if COLOR_URL not in entry['source_urls']:entry['source_urls'].append(COLOR_URL)
    # The CD page explicitly expands this abbreviated name, independently
    # corroborated by the official idol directory. No general nickname guess.
    emily = entries.get('235')
    if emily and emily['name'] == 'エミリー' and emily['source_brand_id'] == '5':
        emily['match_name'] = 'エミリー スチュアート'
        emily['identity_source_urls'] = ['https://imas-db.jp/song/cd/255.html',
                                       'https://idollist.idolmaster-official.jp/detail/30002']
    return sorted(entries.values(), key=lambda item: item['name'])


def _snapshot(people):
    if len(people) < 300:raise ValueError('Character source appears incomplete; keeping the previous snapshot')
    if sum(bool(p.get('color')) for p in people) < 300:
        raise ValueError('Color source appears incomplete; keeping the previous snapshot')
    if sum(bool(p.get('voice_actor_history')) for p in people) < 250:
        raise ValueError('Voice-actor source appears incomplete; keeping the previous snapshot')
    return {'schema_version': 1, 'verified_at': date.today().isoformat(),
                'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'source_name': '아이마스DB (비공식 팬 데이터베이스)', 'source_kind': 'unofficial_fan_database',
                'source_urls': [CV_URL, COLOR_URL], 'usage_url': USAGE_URL,
                'note': '공개 표의 인물·성우 이력·색상 사실 필드. 기존 공식 자료를 덮어쓰지 않습니다.',
                'characters': people}


def refresh_people(destination, progress=None, cancel=None, *, fetcher=None):
    """Refresh the two small public tables atomically; never change user choices."""
    destination = Path(destination)
    fetcher = fetch if fetcher is None else fetcher
    try:
        old = json.loads(destination.read_text(encoding='utf-8'))
        previous_count = len(old.get('characters', [])) if isinstance(old, dict) else 0
    except (OSError, ValueError, TypeError):previous_count = 0
    try:
        if _cancelled(cancel):raise InterruptedError('인물 자료 갱신을 취소했습니다.')
        pages = []
        for number, url in enumerate((CV_URL, COLOR_URL), 1):
            if progress:progress({'message': f'인물 자료 확인 · {number}/2', 'source_url': url})
            pages.append(fetcher(url, cancel=cancel))
            if _cancelled(cancel):raise InterruptedError('인물 자료 갱신을 취소했습니다.')
        snapshot = _snapshot(parse_people(*pages))
        if _cancelled(cancel):raise InterruptedError('인물 자료 갱신을 취소했습니다.')
        atomic_json(destination, snapshot)
        return {'updated': True, 'cancelled': False, 'characters': len(snapshot['characters']),
                'errors': [], 'message': '성우 이력과 색상 자료를 갱신했습니다.'}
    except InterruptedError:
        return {'updated': False, 'cancelled': True, 'characters': previous_count, 'errors': [],
                'message': '인물 자료 갱신을 취소하고 기존 자료를 유지했습니다.'}
    except (OSError, ValueError) as error:
        return {'updated': False, 'cancelled': False, 'characters': previous_count,
                'errors': [str(error)], 'message': '인물 자료 확인에 실패하여 기존 자료를 유지했습니다.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'resources' / 'imas_db_people.json')
    args = parser.parse_args()
    result = refresh_people(args.output)
    print(json.dumps(result, ensure_ascii=False))
    if not result['updated']:raise SystemExit(1)


if __name__ == '__main__':main()
