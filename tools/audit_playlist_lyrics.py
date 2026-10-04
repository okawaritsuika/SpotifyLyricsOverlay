"""Read every playlist lyric result before completion or a requested shutdown."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playlist_lyrics_job import coverage
from playlist_lyrics_store import PlaylistLyricsStore
from storage import atomic_json, read_json


def quota_shutdown(directory, now=None):
    """Use only an explicitly enabled policy and a recent real usage snapshot."""
    result = {'quota_shutdown_enabled': False, 'quota_snapshot_fresh': False,
              'quota_remaining_percent': None, 'quota_shutdown_ready': False}
    now = time.time() if now is None else now
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    try:
        root = Path(directory)/'playlist_lyrics'
        policy = read_json(root/'shutdown_policy.json', {})
        if (not isinstance(policy, dict) or policy.get('policy') != 'all_complete_or_quota'
                or policy.get('enabled') is not True or policy.get('user_authorized') is not True):
            return result
        threshold = policy.get('remaining_percent_threshold')
        if not number(threshold) or not 0 <= threshold <= 100:return result
        result.update(quota_shutdown_enabled=True, quota_threshold_remaining_percent=threshold)
        snapshot = read_json(root/'quota_snapshot.json', {})
        if not isinstance(snapshot, dict) or snapshot.get('source') != 'codex_get_usage_limits':return result
        checked = snapshot.get('checked_at')
        if not number(checked) or not 0 <= now-checked <= 300:return result
        windows = snapshot.get('windows')
        if snapshot.get('limit_id') != 'codex' or not isinstance(windows, list):return result
        remaining = []
        for window in windows:
            if not isinstance(window, dict) or window.get('name') not in ('primary', 'secondary'):continue
            used = window.get('used_percent')
            reset = window.get('resets_at')
            if not number(used) or used < 0:continue
            if reset is not None and (not number(reset) or reset <= now):continue
            remaining.append(max(0, min(100, 100-used)))
        if remaining:
            result.update(quota_snapshot_fresh=True, quota_remaining_percent=min(remaining),
                          quota_checked_at=checked, quota_shutdown_ready=min(remaining) <= threshold)
    except (OSError, ValueError, TypeError):
        # Unavailable quota is never interpreted as exhausted quota.
        return result
    return result


def audit(directory):
    directory = Path(directory)
    state = read_json(directory/'playlist_lyrics_job.json', {})
    manifest = read_json(directory/'playlist_lyrics'/'manifest.json', {})
    catalog = read_json(directory/'spotify_catalog_sync.json', {})
    store = PlaylistLyricsStore(directory)
    rows = manifest.get('entries', [])
    integrity = []
    if manifest.get('playlist_id') != state.get('playlist_id'):integrity.append('playlist_id')
    if not manifest.get('complete') or len(rows) != manifest.get('total'):integrity.append('incomplete_manifest')
    if len(rows) != state.get('expected_entries'):integrity.append('unexpected_total')
    unique = {}
    for index, row in enumerate(rows):
        if row.get('index') != index:integrity.append('entry_position')
        identifier = row.get('track_id', '')
        track = row.get('track')
        if row.get('status') == 'track':
            if not track or track.get('id') != identifier:integrity.append('entry_track_id')
            elif identifier not in unique:unique[identifier] = track
    facts = {}
    for identifier, track in unique.items():
        saved = store.load(identifier)
        if saved and any(saved['track'].get(k) != track.get(k) for k in ('id', 'title', 'album', 'duration')):
            integrity.append('saved_metadata:'+identifier)
            saved = None
        facts[identifier] = coverage(saved)
    records = [{**{key:row.get(key) for key in ('index', 'track_id', 'status')},
                **facts.get(row.get('track_id'), coverage(None))} for row in rows]
    complete = sum(row['complete'] for row in records)
    summary = {'checked_at': datetime.now(timezone.utc).isoformat(),
               'playlist_entries': len(rows), 'expected_entries': state.get('expected_entries'),
               'unique_tracks': len(unique), 'stored_tracks': sum(row['lines'] > 0 for row in facts.values()),
               'complete_entries': complete,
               'synced_entries': sum(bool(row['lines'] and row['timed'] == row['lines']) for row in records),
               'translated_entries': sum(bool(row['lines'] and row['translated'] == row['lines']) for row in records),
               'entries_with_translation': sum(row['translated'] > 0 for row in records),
               'translated_lines': sum(row['translated'] for row in facts.values()),
               'original_lines': sum(row['lines'] for row in facts.values()),
               'missing_entries': len(rows)-complete, 'integrity_errors': sorted(set(integrity)),
               'spotify_checked_tracks': len(state.get('spotify', {})),
               'enrichment_checked_tracks': len(state.get('enrichment', {})),
               'enrichment_pending_tracks': len(state.get('enrichment_pending', {})),
               'catalog_pass_finished': catalog.get('search_pass_finished') is True and catalog.get('enabled') is False,
               'playlist_pass_finished': state.get('collection_pass_finished') is True,
               'all_complete': bool(rows and not integrity and complete == len(rows)),
               'shutdown_policy': state.get('shutdown_policy', 'all_complete')}
    both_passes = summary['catalog_pass_finished'] and summary['playlist_pass_finished']
    after_pass = (summary['shutdown_policy'] == 'after_reviewed_pass'
                  and state.get('reviewed_incomplete_report') is True)
    completed_ready = bool(both_passes and not integrity and (summary['all_complete'] or after_pass))
    summary.update(quota_shutdown(directory))
    if summary['quota_shutdown_enabled']:summary['shutdown_policy'] = 'all_complete_or_quota'
    summary['shutdown_ready'] = bool(state.get('shutdown_requested') is True
                                     and (completed_ready or summary['quota_shutdown_ready']))
    summary['shutdown_reason'] = ('quota_threshold' if summary['shutdown_ready'] and summary['quota_shutdown_ready']
                                  else 'completed_pass' if summary['shutdown_ready'] else None)
    return {'summary': summary, 'records': records}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data')
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()
    result = audit(args.data_dir)
    if args.save:atomic_json(args.data_dir/'playlist_lyrics'/'audit.json', result)
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
