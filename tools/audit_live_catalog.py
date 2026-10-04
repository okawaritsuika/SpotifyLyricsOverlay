"""Measure work-level links against a local song snapshot; never infer completeness.

Example: python tools/audit_live_catalog.py --output data/live_catalog_audit.json
Unmatched entries are diagnostics, not proof that the song has never been sung.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_catalog import LiveCatalog, RESOURCE_PATH  # noqa: E402
from song_catalog import SongCatalog  # noqa: E402


def audit(live, songs):
    linked_songs = [song for song in songs if live.for_song(song)]
    series = {}
    unmatched = []
    for event in live.events:
        rows = live.songs_for_event(event, songs)
        linked = sum(bool(row['matches']) for row in rows)
        for brand in event['series']:
            counts = series.setdefault(brand, {'events': 0, 'setlist_entries': 0, 'linked_entries': 0})
            counts['events'] += 1
            counts['setlist_entries'] += len(rows)
            counts['linked_entries'] += linked
        for row in rows:
            if not row['matches']:
                unmatched.append({'event_id': event['id'], 'date': event['date'],
                                  'position': row['position'], 'title': row['performance']['title']})
    total = sum(len(event['songs']) for event in live.events)
    return {'coverage': live.coverage,
            'note': '수록 공연 내 곡목 연결률입니다. 전체 공연 이력의 완전성을 측정하지 않습니다.',
            'association_note': '곡명·시리즈 수준 후보이며 녹음·가창 버전 확인 결과가 아닙니다.',
            'events': len(live.events), 'setlist_entries': total,
            'source_urls': len({event[field] for event in live.events
                               for field in ('source_url', 'date_source_url', 'setlist_source_url') if event.get(field)}),
            'date_range': [min(event['date'] for event in live.events), max(event['date'] for event in live.events)],
            'dictionary_records': len(songs), 'dictionary_kinds': dict(Counter(song.get('kind', 'unknown') for song in songs)),
            'dictionary_records_with_history': len(linked_songs),
            'linked_setlist_entries': total - len(unmatched),
            'unlinked_setlist_entries': len(unmatched),
            'setlist_link_percent': round(100 * (total - len(unmatched)) / total, 2) if total else 0,
            'series': series, 'unmatched': unmatched}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--songs', type=Path, default=ROOT / 'resources' / 'song_catalog.json')
    parser.add_argument('--live', type=Path, default=RESOURCE_PATH)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    source = SongCatalog(args.songs)
    live = LiveCatalog(args.live)
    if source.status == 'empty' or live.status != 'ready':
        raise ValueError('Both snapshots must be valid before measuring coverage.')
    result = audit(live, source.songs)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'unmatched'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
