"""Refresh public performance facts while preserving failed and official sources."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from live_catalog import RESOURCE_PATH, read_catalog, title_key
from imas_live_sources import collect

LINKS_PATH = RESOURCE_PATH.with_name('live_event_links.json')


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def _links():
    try:
        value = json.loads(LINKS_PATH.read_text(encoding='utf-8'))
        return value.get('links', []) if value.get('schema_version') == 1 else []
    except (OSError, ValueError, TypeError):
        return []


def merge_collected(base, previous, collected, links=None):
    """Never remove a prior event on missing or failed source results.

    Only reviewed same-performance links coalesce independent sources. Dates
    alone cannot distinguish a matinee, evening show or concurrent concert.
    """
    events = {event['id']: deepcopy(event) for event in (base or {}).get('events', [])}
    events.update({event['id']: deepcopy(event) for event in (previous or {}).get('events', [])})
    reviewed_links = _links() if links is None else links
    curated_ids = {link.get('official_id') for link in reviewed_links}
    successful = set(collected.get('successful_urls', []))
    # A fully parsed page replaces its earlier raw tables. Table insertion or
    # removal can change source IDs; retaining those stale rows creates ghosts.
    events = {identifier: event for identifier, event in events.items()
              if identifier in curated_ids or event.get('source_kind') != 'fan_database'
              or event.get('source_url') not in successful}
    events.update({event['id']: deepcopy(event) for event in collected.get('events', [])})
    for event in events.values():
        event.setdefault('source_kind', 'official')
    for link in reviewed_links:
        official = events.get(link.get('official_id'))
        candidates = [event for event in events.values()
                      if event.get('source_kind') == 'fan_database'
                      and event['id'] != link.get('official_id')
                      and event.get('source_url') == link.get('database_url')
                      and event.get('date') == link.get('date')
                      and (not link.get('database_section')
                           or event.get('source_table_label') == link['database_section'])]
        if official is None or official['date'] != link.get('date') or len(candidates) != 1:
            continue
        database = candidates[0]
        if database['id'] == official['id']:
            continue
        # Preserve the curated event identity and source evidence while using
        # the database's complete, independently attributed performance rows.
        merged = deepcopy(database)
        merged['id'] = official['id']
        sources = list(official.get('official_sources', []))
        if official.get('source_kind') == 'official':
            for field in ('source_url', 'date_source_url', 'setlist_source_url'):
                url = official.get(field)
                if url and not any(item['url'] == url for item in sources):
                    sources.append({'name': official.get('source_name') or '공식 자료', 'url': url})
        merged['official_sources'] = sources
        merged['title'] = official['title']
        for row in merged['songs']:
            matches = [old for old in official['songs']
                       if title_key(row['title']) in {title_key(old.get(field))
                          for field in ('title', 'song_title', 'performance_title') if old.get(field)}]
            if len(matches) == 1:
                for field in ('song_title', 'performance_title'):
                    if matches[0].get(field):row.setdefault(field, matches[0][field])
        events.pop(database['id'])
        events[official['id']] = merged
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    failures = deepcopy(collected.get('failures', []))
    report = deepcopy(collected.get('stats', {}))
    report.update(index_urls=collected.get('index_urls', []), failures=failures,
                  successful_urls=collected.get('successful_urls', []),
                  skipped_urls=collected.get('skipped_urls', []),
                  index_url='https://imas-db.jp/song/event/', checked_at=now,
                  source_kind='fan_database')
    report['events'] = len(events)
    report['setlist_entries'] = sum(len(event['songs']) for event in events.values())
    return {'schema_version': 1, 'coverage': 'partial', 'verified_at': now,
            'generated_at': now,
            'note': '아이마스 DB의 2013년 이후 수록 공연과 공식 자료를 함께 정리했습니다. '
                    '출처에 없는 공연·미확인 항목은 포함되지 않으며, 전체 공연 이력은 아닙니다.',
            'collection_report': report,
            'events': sorted(events.values(), key=lambda event: (event['date'], event['id']), reverse=True)}


def write_catalog(destination, value, cancel=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='live-catalog-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        if read_catalog(temporary) is None:
            raise ValueError('새 라이브 자료 검증에 실패하여 기존 자료를 유지합니다.')
        if _cancelled(cancel):
            return False
        os.replace(temporary, destination)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_live_catalog(destination, progress=None, cancel=None, *, collector=collect, resource_path=None):
    destination = Path(destination)
    previous = read_catalog(destination)
    base = read_catalog(resource_path or RESOURCE_PATH)
    def report_progress(value):
        if progress:
            progress({**value, 'message': f"라이브 자료 확인 · {value.get('completed', 0)}/{value.get('total', 0)}문서"
                     f" · 공연 {value.get('events', 0):,}회 · 확인 실패 {value.get('failures', 0)}건"})
    collected = collector(destination.parent / 'live_source_cache', progress=report_progress,
                          cancel=cancel, refresh=True)
    cancelled = _cancelled(cancel) or bool(collected.get('cancelled'))
    if cancelled or not collected.get('events'):
        return {'updated': False, 'cancelled': cancelled,
                'events': len((previous or base or {}).get('events', [])),
                'setlist_entries': sum(len(event['songs']) for event in (previous or base or {}).get('events', [])),
                'errors': collected.get('failures', []),
                'message': '새 공연 자료를 확인하지 못해 기존 자료를 유지합니다.'}
    value = merge_collected(base, previous, collected)
    updated = write_catalog(destination, value, cancel)
    if not updated:
        value = previous or base or {'events': []}
    return {'updated': updated, 'cancelled': not updated,
            'events': len(value['events']),
            'setlist_entries': sum(len(event['songs']) for event in value['events']),
            'errors': collected.get('failures', []),
            'message': '라이브 자료를 갱신했습니다.' if updated else '업데이트를 취소하여 기존 자료를 유지합니다.'}
