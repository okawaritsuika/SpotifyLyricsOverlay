"""Persistent batch queue, candidate review and reversible automatic sync overlays."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
from threading import Event, Thread
import time

from local_analysis import WorkerBusy, alignment_quality, worker_lock
from storage import Store, validate_times


DATABASE = 'sync_reanalysis.sqlite3'
MODES = ('all', 'automatic', 'incomplete', 'selected', 'old')
ENGINES = ('default', 'experimental')


def pack(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def connect(directory, *, write=False):
    db = sqlite3.connect(Path(directory) / DATABASE, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
    finally:
        db.close()


def initialize(directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    with connect(directory) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS batch (
                id INTEGER PRIMARY KEY CHECK (id=1), state TEXT NOT NULL,
                mode TEXT NOT NULL, engine TEXT NOT NULL, force INTEGER NOT NULL, retry_only INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO batch VALUES (1, 'idle', 'all', 'default', 0, 0);
            CREATE TABLE IF NOT EXISTS queue (
                id TEXT PRIMARY KEY, item TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                candidate TEXT, improved INTEGER NOT NULL DEFAULT 0, regressed INTEGER NOT NULL DEFAULT 0,
                recovered INTEGER NOT NULL DEFAULT 0, new_missing INTEGER NOT NULL DEFAULT 0, more_timed INTEGER NOT NULL DEFAULT 0,
                in_batch INTEGER NOT NULL DEFAULT 1, mode TEXT NOT NULL, can_apply INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS alignment_cache (key TEXT PRIMARY KEY, candidate TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS overrides (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                fingerprint TEXT NOT NULL, recording TEXT NOT NULL, updated REAL NOT NULL, value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS overrides_target ON overrides (kind,target,fingerprint,recording);
            CREATE TABLE IF NOT EXISTS history (
                sequence INTEGER PRIMARY KEY, id TEXT NOT NULL, updated REAL NOT NULL,
                action TEXT NOT NULL, backup TEXT NOT NULL, replacement TEXT
            );
            CREATE INDEX IF NOT EXISTS history_item ON history (id,sequence);
        """)


def detail_at(alignment, index):
    details = alignment.get('details') or []
    return details[index] if index < len(details) else {}


def metrics(alignment):
    times = alignment['times']
    present = [stamp for stamp in times if stamp is not None]
    qualities = [alignment_quality(detail_at(alignment, i), stamp)
                 for i, stamp in enumerate(times) if stamp is not None and detail_at(alignment, i)]
    return {
        'timed': len(present), 'missing': len(times) - len(present),
        'quality': {grade: qualities.count(grade) for grade in ('high', 'medium', 'low')} if qualities else None,
        'reversals': sum(a >= b for a, b in zip(present, present[1:])),
        'estimated': (sum(stamp is not None and bool(detail_at(alignment, i).get('estimated'))
                          for i, stamp in enumerate(times)) if any(alignment.get('details') or ()) else None),
        'seconds': alignment.get('seconds'), 'engine': alignment.get('engine') or '—',
        'version': alignment.get('version'), 'profile': alignment.get('profile') or alignment.get('romaji_profile'),
    }


def comparison(item, candidate):
    baseline = item['before']
    before, after = metrics(baseline), metrics(candidate)
    recovered = new_missing = supported = 0
    rows = []
    for i, (line, old, new, protected) in enumerate(zip(
            item['lines'], baseline['times'], candidate['times'], item['protected'])):
        detail = detail_at(candidate, i)
        quality = alignment_quality(detail, new)
        recovered += old is None and new is not None
        new_missing += old is not None and new is None
        supported += old is None and new is not None and not protected and quality != 'low' and not detail.get('interpolated')
        rows.append({'index': i, 'text': line, 'before': old, 'after': new, 'protected': protected,
                     'quality': quality, 'status': detail.get('status', ''), 'reason': detail.get('reason', '')})
    blocked = bool(candidate.get('apply_blocked') or item.get('apply_blocked'))
    return {'before': before, 'after': after, 'rows': rows, 'apply_blocked': blocked,
            'improved': (supported > 0 and new_missing == 0 and after['reversals'] <= before['reversals']
                         and not blocked),
            'regressed': after['timed'] < before['timed'], 'recovered': recovered, 'new_missing': new_missing}


def _patch_times(times, changes):
    """Skip edits that would cross a retained anchor, including a rejected edit."""
    kept = dict(changes)
    while kept:
        conflict = set()
        last = None
        for i, original in enumerate(times):
            stamp = kept.get(i, original)
            if stamp is None:
                continue
            if stamp < 0:
                conflict.add(i)
                continue
            if last is not None and stamp <= last[1]:
                if last[0] in kept:
                    conflict.add(last[0])
                if i in kept:
                    conflict.add(i)
            last = i, stamp
        if not conflict:
            break
        for i in conflict:
            kept.pop(i, None)
    return [kept.get(i, old) for i, old in enumerate(times)], kept


def _applied(directory, kind, target, fingerprint='', recordings=('',), duration=None):
    if not (Path(directory) / DATABASE).is_file():
        return None
    with connect(directory) as db:
        for recording in recordings:
            for row in db.execute("""SELECT value FROM overrides WHERE kind=? AND target=?
                                     AND fingerprint=? AND recording=? ORDER BY updated DESC""",
                                  (kind, target, fingerprint, recording)):
                value = json.loads(row['value'])
                if duration is None or value.get('duration') is None or abs(value['duration'] - duration) <= 2:
                    return value
    return None


def applied_song(directory, song, timing, recordings, duration):
    from live_timing import original_signature
    from timing_quality import cleared_rows, timing_origins

    value = (_applied(directory, 'song', song.id, song.fingerprint, recordings, duration)
             or _applied(directory, 'song', song.id, original_signature(song), recordings, duration))
    if not value or timing.get('source') == 'LRCLIB':
        return timing['times']
    offset = timing.get('offset', 0)
    origins = timing_origins(timing)
    cleared = cleared_rows(timing)
    changes = {int(i): round(stamp - offset, 6) for i, stamp in value['changes'].items()
               if int(i) < len(origins) and origins[int(i)] != 'manual' and int(i) not in cleared and 0 <= stamp < duration}
    return _patch_times(timing['times'], changes)[0]


def applied_job(directory, job):
    if not job:
        return job
    old = job['result'].get('alignment', {})
    baseline = old.get('times')
    if baseline is None:
        baseline = job.get('spec', {}).get('times') or [None] * len(job.get('spec', {}).get('lines', ()))
    value = _applied(directory, 'job', job['id'])
    if not value or value.get('base_times') != baseline or not baseline:
        return job
    times, used = _patch_times(baseline, {int(i): t for i, t in value['changes'].items()})
    details = [{**(value['details'].get(str(i), {}) if i in used else detail_at(
        {'details': old.get('line_alignment')}, i)), 'start': stamp}
        for i, stamp in enumerate(times)]
    alignment = {**old, 'times': times, 'line_alignment': details, 'reanalysis_engine': value['engine'],
                 'reanalysis_rows': list(used)}
    return {**job, 'result': {**job['result'], 'alignment': alignment,
                              'reanalysis_clock': value.get('playlist_clock')}}


def has_applied_job_times(job):
    if not job:
        return False
    result = job['result']
    clock = result.get('reanalysis_clock') or result.get('playlist_clock') or {}
    return bool(result.get('alignment', {}).get('reanalysis_rows') and clock.get('usable', True))


def manual_review_candidate(directory, song, track_id, recording, duration):
    """Read a compatible batch result for manual review without applying it globally."""
    from live_timing import original_signature
    from local_analysis import playlist_auto_times

    if not (Path(directory) / DATABASE).is_file():
        return []
    originals = [line.original for line in song.lines]
    with connect(directory) as db:
        rows = db.execute("""
            SELECT item, candidate FROM queue WHERE can_apply=1 AND candidate IS NOT NULL
            AND status IN ('done','applied','restored') AND (
                (json_extract(item,'$.kind')='job' AND ?!='' AND json_extract(item,'$.track_id')=?)
                OR (json_extract(item,'$.kind')='song' AND json_extract(item,'$.recording')=?
                    AND (json_extract(item,'$.target')=? OR (?!='' AND json_extract(item,'$.track_id')=?))))
            ORDER BY rowid DESC
        """, (track_id, track_id, recording, song.id, track_id, track_id)).fetchall()
    best = []
    for row in rows:
        item, candidate = json.loads(row['item']), json.loads(row['candidate'])
        length = candidate.get('duration')
        if item['lines'] != originals or type(length) not in (int, float) or abs(length - duration) > 2:
            continue
        if item['kind'] == 'song' and item['fingerprint'] not in (song.fingerprint, original_signature(song)):
            continue
        times = playlist_auto_times({'status': 'done', 'spec': {'lines': originals},
            'result': {'alignment': {**candidate, 'line_alignment': candidate.get('details', [])},
                       'playlist_clock': candidate.get('playlist_clock') or {}}})
        if sum(stamp is not None for stamp in times) > sum(stamp is not None for stamp in best):
            best = times
    return best


def enqueue_spotify_capture(directory, base, track, song, recording, folder, duration):
    """Append the verified capture after existing batch items, without starting another model."""
    from app_paths import runtime_dir, worker_command, worker_environment
    from audio_capture_cache import capture_path, read_capture
    from live_timing import original_signature
    from local_analysis import lyric_digest
    from timing_quality import timing_origins

    directory = Path(directory)
    info = read_capture(folder,duration)
    if info is None:
        raise ValueError('저장한 Spotify 음원과 재생 시간을 확인할 수 없습니다.')
    timing = Store(directory).timing(song) if not song.id.startswith(('memory-','completed-')) else None
    if timing is None:
        from live_timing import LiveTimingStore
        timing = LiveTimingStore(directory).load(song)
    originals = [line.original for line in song.lines]
    times = timing['times'] if timing else [None] * len(originals)
    offset = timing.get('offset',0) if timing else 0
    origins = timing_origins(timing) if timing else [None] * len(times)
    protected = [origin == 'manual' and timing.get('source') != 'LRCLIB' for origin in origins]
    clock = [stamp+offset if keep and stamp is not None and stamp+offset>=0 else None
             for stamp,keep in zip(times,protected)]
    item_id = 'spotify-capture:' + track['id'] + ':' + recording + ':' + lyric_digest(originals)
    item = {'id':item_id,'kind':'song','target':song.id,'track_id':track['id'],'identifier':track['id'],
            'fingerprint':original_signature(song),'recording':recording,'duration':duration,
            'title':track['title'],'artist':', '.join(track['artists']),'album':track['album'],
            'lines':originals,'line_readings':[line.reading for line in song.lines],
            'path':str(capture_path(folder)),'capture':info,'audio_kind':'capture','eligible':True,
            'before':{'times':clock},'protected':protected,'base_times':times,'offset':offset}
    if not (directory / DATABASE).is_file():
        initialize(directory)
    with connect(directory,write=True) as db:
        batch = db.execute('SELECT state FROM batch').fetchone()[0]
        db.execute("""INSERT INTO queue (id,item,status,mode) VALUES (?,?,'queued','old')
                      ON CONFLICT(id) DO UPDATE SET item=excluded.item,status='queued',error='',in_batch=1
                      WHERE queue.status IN ('failed','skipped')""", (item_id,pack(item)))
        if batch not in ('idle','finished'):
            return
        if not db.execute("SELECT 1 FROM queue WHERE id=? AND status='queued'",(item_id,)).fetchone():
            return
        db.execute("UPDATE batch SET state='running',mode='old',engine='default',force=0,retry_only=0")
    try:
        command = worker_command(base,'sync_reanalysis',optional=True) + [str(directory),str(runtime_dir())]
        with (directory/'sync-reanalysis-worker.log').open('ab') as log:
            subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                             env=worker_environment(base,prefer_inherited=True),
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,ValueError):
        with connect(directory,write=True) as db:
            db.execute("UPDATE batch SET state='paused' WHERE state='running'")
        raise


def apply_job_times(job, timing, times, duration):
    from timing_quality import cleared_rows, timing_origins

    if timing.get('source') == 'LRCLIB' or not has_applied_job_times(job):
        return times
    raw = job['result']['alignment']['times']
    origins = timing_origins(timing)
    cleared = cleared_rows(timing)
    offset = timing.get('offset', 0)
    rows = job['result']['alignment'].get('reanalysis_rows', [])
    changes = {i: round(raw[i] - offset, 6) for i in rows
               if len(raw) == len(times) and raw[i] is not None and origins[i] != 'manual'
               and i not in cleared and 0 <= raw[i] < duration}
    return _patch_times(times, changes)[0]


def _finished(db):
    if db.execute("SELECT 1 FROM queue WHERE in_batch=1 AND status IN ('queued','running','retry')").fetchone():
        db.execute("UPDATE batch SET state='paused',retry_only=0 WHERE state='running' AND retry_only=1")
    else:
        db.execute("UPDATE batch SET state='finished',retry_only=0")


def run_batch(directory, runtime):
    from sync_reanalysis_sources import analyze

    settings = Store(directory).settings()
    model_cache = {}

    def cached(key):
        with connect(directory) as db:
            result = db.execute('SELECT candidate FROM alignment_cache WHERE key=?', (key,)).fetchone()
        return json.loads(result['candidate']) if result else None

    with worker_lock(directory):
        while True:
            with connect(directory, write=True) as db:
                batch = db.execute('SELECT * FROM batch').fetchone()
                if batch['state'] != 'running':
                    if not db.execute("SELECT 1 FROM queue WHERE status IN ('queued','retry','running')").fetchone():
                        _finished(db)
                    return
                row = db.execute("SELECT id,item FROM queue WHERE in_batch=1 AND status=? ORDER BY rowid LIMIT 1",
                                 ('retry' if batch['retry_only'] else 'queued',)).fetchone()
                if not row:
                    _finished(db)
                    return
                db.execute("UPDATE queue SET status='running' WHERE id=?", (row['id'],))
            item = json.loads(row['item'])
            try:
                anchors = None
                if batch['mode'] == 'incomplete':
                    baseline = item['before']
                    anchors = [stamp if item['protected'][i] or alignment_quality(detail_at(baseline, i), stamp) != 'low'
                               else None for i, stamp in enumerate(baseline['times'])]
                candidate = analyze(item, settings, runtime, engine=batch['engine'], force=bool(batch['force']),
                                    lookup=cached, model_cache=model_cache, anchors=anchors)
                validate_times(candidate['times'], len(item['lines']))
                full = candidate
                if batch['mode'] == 'incomplete':
                    baseline = item['before']
                    proposed = {i: stamp for i, stamp in enumerate(candidate['times'])
                                if stamp is not None and anchors[i] is None}
                    times, used = _patch_times(baseline['times'], proposed)
                    candidate = {**candidate, 'times': times, 'details': [
                        detail_at(candidate if i in used else baseline, i) for i in range(len(times))]}
                result = comparison(item, candidate)
                with connect(directory, write=True) as db:
                    if full.get('cache_key') and not full.get('cached'):
                        db.execute('INSERT OR REPLACE INTO alignment_cache VALUES (?,?)',
                                   (full['cache_key'], pack(full)))
                    db.execute("""UPDATE queue SET status='done',candidate=?,improved=?,regressed=?,
                                  recovered=?,new_missing=?,more_timed=?,can_apply=? WHERE id=?""",
                               (pack(candidate), result['improved'], result['regressed'], result['recovered'],
                                result['new_missing'], result['after']['timed'] > result['before']['timed'],
                                not result['apply_blocked'], row['id']))
            except Exception as error:
                with connect(directory, write=True) as db:
                    db.execute("UPDATE queue SET status='failed', error=? WHERE id=?",
                               (f'{type(error).__name__}: {error}', row['id']))


class SyncReanalysis:
    def __init__(self, directory, base, *, runtime=None):
        from app_paths import runtime_dir

        self.directory = Path(directory).resolve()
        self.base = Path(base).resolve()
        self.runtime = Path(runtime or runtime_dir()).resolve()
        self.worker = None
        self.worker_stop = Event()
        self.worker_error = ''
        self.loader = None
        self.inventory_task = None
        self.inventory_error = ''
        self.original_items = {}
        self.items = {}
        initialize(self.directory)
        # An older app may have shut down while the child still owned the queue.
        # Recover interrupted rows only once the worker lock can be acquired.
        self._recover()

    def _recover(self):
        with connect(self.directory) as db:
            busy = db.execute("""SELECT 1 FROM batch WHERE state='running'
                               UNION SELECT 1 FROM queue WHERE status='running' LIMIT 1""").fetchone()
        if not busy:
            return
        try:
            with worker_lock(self.directory), connect(self.directory, write=True) as db:
                db.execute("""UPDATE queue SET status=CASE WHEN (SELECT retry_only FROM batch)=1
                              THEN 'retry' ELSE 'queued' END WHERE status='running'""")
                db.execute("UPDATE batch SET state='paused' WHERE state='running'")
        except WorkerBusy:
            pass

    def refresh(self):
        from sync_reanalysis_sources import discover
        self._set_inventory(discover(self.directory))

    def _set_inventory(self, items):
        self.original_items = {item['id']: item for item in items}
        self._refresh_applied()

    def _refresh_applied(self):
        from japanese_alignment import ALIGNMENT_VERSION, CTC
        from sync_reanalysis_sources import BATCH_CTC_PROFILE

        self.items = dict(self.original_items)
        with connect(self.directory) as db:
            rows = db.execute('SELECT id,updated,value FROM overrides').fetchall()
        for row in rows:
            item = self.items.get(row['id'])
            if not item:
                continue
            value = json.loads(row['value'])
            before = item['before']
            base_times = item.get('base_times', before['times'])
            if item['kind'] == 'job' and base_times != value.get('base_times'):
                continue
            times, used = _patch_times(before['times'], {int(i): stamp for i, stamp in value['changes'].items()
                                       if not item['protected'][int(i)]})
            details = [value['details'].get(str(i), {}) if i in used else detail_at(before, i) for i in range(len(times))]
            active = {**before, 'times': times, 'details': details, 'engine': value['engine'],
                      'seconds': value.get('seconds'), 'version': value.get('version'), 'profile': value.get('profile')}
            self.items[row['id']] = {
                **item, 'before': active, 'base_times': base_times, 'updated': row['updated'], 'engine': active['engine'],
                'timed': sum(stamp is not None for stamp in times),
                'incomplete': any(alignment_quality(detail, stamp) == 'low' for detail, stamp in zip(details, times)),
                'outdated': (not value.get('full') or value.get('version') != ALIGNMENT_VERSION
                             or value['engine'] == CTC and value.get('profile') != BATCH_CTC_PROFILE),
            }

    def refresh_async(self):
        from sync_reanalysis_sources import discover

        if self.inventory_task is not None:
            return
        if self.loader is None:
            self.loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sync-inventory')
        self.inventory_error = ''
        self.inventory_task = self.loader.submit(discover, self.directory)

    def _collect_inventory(self):
        if self.inventory_task is None or not self.inventory_task.done():
            return
        try:
            self._set_inventory(self.inventory_task.result())
        except Exception as error:
            self.inventory_error = str(error)
        finally:
            self.inventory_task = None

    def list_items(self):
        with connect(self.directory) as db:
            progress = {row['id']: dict(row) for row in db.execute("""
                SELECT id,status,error,candidate IS NOT NULL AS has_candidate,can_apply FROM queue
            """)}
            applied = {row['id'] for row in db.execute('SELECT id FROM overrides')}
            missing = progress.keys() - self.items.keys()
            unavailable = [json.loads(db.execute('SELECT item FROM queue WHERE id=?', (item_id,)).fetchone()[0])
                           for item_id in sorted(missing)]
        return [{**{k: item.get(k) for k in ('id', 'title', 'artist', 'identifier', 'path', 'engine', 'updated',
                                            'timed', 'total', 'eligible', 'reason', 'manual', 'audio_kind')},
                 **progress.get(item['id'], {'status': 'applied' if item['id'] in applied else 'idle', 'error': ''}),
                 'can_restore': item['id'] in applied}
                for item in [*self.items.values(), *unavailable]]

    def _scope(self, mode, selected_ids):
        if mode not in MODES:
            raise ValueError('재분석 범위를 선택하세요.')
        selected = set(selected_ids)
        return [item for item in self.items.values() if (
            mode == 'all' or mode == 'automatic' and item.get('auto')
            or mode == 'incomplete' and item.get('incomplete')
            or mode == 'selected' and item['id'] in selected or mode == 'old' and item.get('outdated'))]

    def preview(self, mode, selected_ids=()):
        items = self._scope(mode, selected_ids)
        return {'target': len(items), 'eligible': sum(bool(item['eligible']) for item in items)}

    def _poll(self):
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self._recover()

    def snapshot(self):
        self._poll()
        self._collect_inventory()
        with connect(self.directory) as db:
            state = db.execute('SELECT state FROM batch').fetchone()['state']
            stats = db.execute("""
                SELECT count(*) AS total, sum(status IN ('done','applied','restored','discarded')) AS done,
                       sum(status='failed') AS failed, sum(status='skipped') AS skipped,
                       sum(status IN ('queued','running','retry')) AS remaining,
                       sum(improved) AS improved, sum(regressed) AS regressed, sum(more_timed) AS more_timed,
                       sum(recovered) AS recovered, sum(new_missing) AS new_missing
                FROM queue WHERE in_batch=1
            """).fetchone()
            active = db.execute("SELECT json_extract(item,'$.title') FROM queue WHERE status='running'").fetchone()
            applied = db.execute('SELECT count(*) FROM overrides').fetchone()[0]
            restored = db.execute("SELECT count(DISTINCT id) FROM history WHERE action='restore'").fetchone()[0]
        return {**{key: stats[key] or 0 for key in stats.keys()}, 'state': state, 'running': active[0] if active else '',
                'applied': applied, 'restored': restored, 'loading': self.inventory_task is not None,
                'inventory_error': self.inventory_error, 'worker_error': self.worker_error}

    @staticmethod
    def _assert_idle(db):
        if db.execute("""SELECT 1 FROM batch WHERE state='running'
                          UNION SELECT 1 FROM queue WHERE status='running' LIMIT 1""").fetchone():
            raise ValueError('현재 곡 처리가 끝난 뒤 다시 시도하세요.')

    def _launch(self):
        if self.worker is not None and self.worker.is_alive():
            return
        from app_paths import worker_command, worker_environment

        try:
            command = worker_command(self.base, 'sync_reanalysis', optional=True) + [
                str(self.directory), str(self.runtime)]
        except (OSError, ValueError):
            with connect(self.directory, write=True) as db:
                db.execute("UPDATE batch SET state='paused'")
            raise
        self.worker_error = ''
        self.worker_stop.clear()
        self.worker = Thread(target=self._supervise, args=(command, worker_environment(self.base, prefer_inherited=True)),
                             daemon=True, name='sync-reanalysis-worker')
        self.worker.start()

    def _supervise(self, command, environment):
        from audio_job import ANALYSIS_LOCK

        try:
            while not self.worker_stop.is_set():
                with connect(self.directory) as db:
                    if db.execute('SELECT state FROM batch').fetchone()['state'] != 'running':
                        return
                if not ANALYSIS_LOCK.acquire(timeout=.2):
                    continue
                try:
                    if self.worker_stop.is_set():
                        return
                    with (self.directory / 'sync-reanalysis-worker.log').open('ab') as log, subprocess.Popen(
                            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
                        while process.poll() is None and not self.worker_stop.wait(.1):
                            pass
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            return
                        if process.returncode:
                            raise ValueError(f'재분석 워커 종료 코드 {process.returncode} · sync-reanalysis-worker.log')
                finally:
                    ANALYSIS_LOCK.release()
        except (ValueError, OSError, sqlite3.Error) as error:
            self.worker_error = str(error)
            self._recover()

    def start(self, mode, selected_ids=(), force=False, engine='default'):
        self._poll()
        if engine not in ENGINES:
            raise ValueError('재분석 엔진을 선택하세요.')
        items = self._scope(mode, selected_ids)
        if not items:
            raise ValueError('재분석 대상 곡이 없습니다.')
        eligible = any(item['eligible'] for item in items)
        with connect(self.directory, write=True) as db:
            self._assert_idle(db)
            if db.execute("""SELECT 1 FROM queue,batch WHERE in_batch=1 AND status IN ('queued','retry')
                             AND state<>'cancelled'""").fetchone():
                raise ValueError('대기 중인 작업이 있습니다. 이어서 처리하거나 취소한 뒤 새 작업을 시작하세요.')
            db.execute("DELETE FROM queue WHERE status IN ('queued','retry')")
            db.execute('UPDATE queue SET in_batch=0')
            db.executemany('INSERT OR REPLACE INTO queue (id,item,status,error,mode) VALUES (?,?,?,?,?)',
                           ((item['id'], pack(item), 'queued' if item['eligible'] else 'skipped',
                             '' if item['eligible'] else item['reason'], mode) for item in items))
            db.execute('UPDATE batch SET state=?,mode=?,engine=?,force=?,retry_only=0',
                       ('running' if eligible else 'finished', mode, engine, int(force)))
        if eligible:
            self._launch()

    def pause(self):
        with connect(self.directory, write=True) as db:
            db.execute("UPDATE batch SET state='paused' WHERE state='running'")

    def cancel(self):
        with connect(self.directory, write=True) as db:
            db.execute("UPDATE batch SET state='cancelled' WHERE state IN ('running','paused')")

    def resume(self):
        self._poll()
        self._recover()
        with connect(self.directory, write=True) as db:
            if not db.execute("SELECT 1 FROM queue WHERE in_batch=1 AND status IN ('queued','running','retry')").fetchone():
                _finished(db)
                return
            if not db.execute('SELECT retry_only FROM batch').fetchone()[0]:
                db.execute("UPDATE queue SET status='queued' WHERE status='retry'")
            db.execute("UPDATE batch SET state='running'")
        self._launch()

    def retry_failed(self):
        self._poll()
        with connect(self.directory, write=True) as db:
            self._assert_idle(db)
            count = db.execute("UPDATE queue SET status='retry',error='' WHERE in_batch=1 AND status='failed'").rowcount
            if count:
                db.execute("UPDATE batch SET state='running',retry_only=1")
        if count:
            self._launch()
        return count

    def compare(self, item_id):
        with connect(self.directory) as db:
            row = db.execute('SELECT item,candidate FROM queue WHERE id=?', (item_id,)).fetchone()
        return comparison(json.loads(row['item']), json.loads(row['candidate'])) if row and row['candidate'] else None

    def apply(self, ids=None, improvements_only=False, line_indices=None):
        from audio_capture_cache import discard_private_capture

        selected = None if ids is None else set(ids)
        indices = None if line_indices is None else set(line_indices)
        count = 0
        release = {}
        with connect(self.directory, write=True) as db:
            self._assert_idle(db)
            rows = db.execute('SELECT * FROM queue WHERE candidate IS NOT NULL').fetchall()
            for row in rows:
                if (selected is not None and row['id'] not in selected) or improvements_only and not row['improved']:
                    continue
                item, candidate = json.loads(row['item']), json.loads(row['candidate'])
                before = item['before']
                clock = candidate.get('playlist_clock') or {}
                if (item.get('apply_blocked') or candidate.get('apply_blocked')
                        or clock.get('reason') == 'playlist_clock_mismatch'):
                    continue
                previous = db.execute('SELECT value FROM overrides WHERE id=?', (row['id'],)).fetchone()
                previous = json.loads(previous['value']) if previous else None
                changes = dict(previous['changes']) if previous else {}
                details = dict(previous['details']) if previous else {}
                proposed = {}
                for i, stamp in enumerate(candidate['times']):
                    if stamp is None or item['protected'][i] or indices is not None and i not in indices:
                        continue
                    old = before['times'][i]
                    if row['mode'] == 'incomplete' and old is not None and alignment_quality(detail_at(before, i), old) != 'low':
                        continue
                    if stamp != changes.get(str(i), old):
                        proposed[i] = stamp
                baseline, _ = _patch_times(before['times'], {int(i): t for i, t in changes.items()})
                resolved, accepted = _patch_times(baseline, proposed)
                if not accepted:
                    continue
                for i, stamp in accepted.items():
                    changes[str(i)] = stamp
                    details[str(i)] = detail_at(candidate, i)
                value = {'changes': changes, 'details': details, 'engine': candidate['engine'],
                         'version': candidate.get('version'), 'profile': candidate.get('romaji_profile'),
                         'seconds': candidate.get('seconds'), 'full': indices is None and row['mode'] != 'incomplete',
                         'duration': item.get('duration') or candidate.get('duration'),
                         'playlist_clock': candidate.get('playlist_clock'),
                         'base_times': item.get('base_times', before['times'])}
                now = time.time()
                revision = db.execute('INSERT INTO history (id,updated,action,backup) VALUES (?,?,?,?)',
                                      (row['id'], now, 'apply', pack({'item': item, 'override': previous}))).lastrowid
                value['revision'] = revision
                db.execute('UPDATE history SET replacement=? WHERE sequence=?', (pack(value), revision))
                db.execute('INSERT OR REPLACE INTO overrides VALUES (?,?,?,?,?,?,?)',
                           (row['id'], item['kind'], item['target'], item.get('fingerprint') or '',
                            (item.get('recording') or '') if item['kind'] == 'song' else '', now, pack(value)))
                db.execute("UPDATE queue SET status='applied' WHERE id=?", (row['id'],))
                if item.get('audio_kind') == 'capture' and all(stamp is not None for stamp in resolved):
                    release[item['path']] = item['capture']['duration']
                count += 1
            if release:
                for row in db.execute("SELECT item FROM queue WHERE status IN ('queued','retry','running','done','failed')"):
                    release.pop(json.loads(row['item']).get('path'), None)
        for path, duration in release.items():
            try:removed = discard_private_capture(path, self.directory / 'audio_captures', duration)
            except OSError:continue
            if removed:
                self.original_items = {
                    ident:({**item, 'eligible':False, 'reason':'분석이 끝나 재분석용 데이터를 정리했습니다.'}
                           if item.get('path') == path else item) for ident,item in self.original_items.items()}
        if count and self.original_items:
            self._refresh_applied()
        return count

    def discard(self, ids):
        with connect(self.directory, write=True) as db:
            self._assert_idle(db)
            return sum(db.execute("""UPDATE queue SET candidate=NULL,status='discarded',can_apply=0
                                    WHERE id=? AND candidate IS NOT NULL""", (item_id,)).rowcount
                       for item_id in dict.fromkeys(ids))

    def restore(self, ids=None):
        selected = None if ids is None else set(ids)
        count = 0
        with connect(self.directory, write=True) as db:
            self._assert_idle(db)
            for row in db.execute('SELECT * FROM overrides').fetchall():
                if selected is not None and row['id'] not in selected:
                    continue
                current = json.loads(row['value'])
                backup = db.execute('SELECT backup FROM history WHERE id=? AND sequence=?',
                                    (row['id'], current.get('revision'))).fetchone()
                if not backup:
                    continue
                saved = json.loads(backup['backup'])
                now = time.time()
                if saved['override'] is None:
                    db.execute('DELETE FROM overrides WHERE id=?', (row['id'],))
                else:
                    db.execute('UPDATE overrides SET updated=?,value=? WHERE id=?',
                               (now, pack(saved['override']), row['id']))
                db.execute('INSERT INTO history (id,updated,action,backup) VALUES (?,?,?,?)',
                           (row['id'], now, 'restore', pack({'item': saved['item'], 'override': current})))
                db.execute("UPDATE queue SET status='restored' WHERE id=?", (row['id'],))
                count += 1
        if count and self.original_items:
            self._refresh_applied()
        return count

    def close(self):
        self.worker_stop.set()
        self.pause()
        if self.loader is not None:
            self.loader.shutdown(wait=False, cancel_futures=True)
        if self.worker is not None:
            self.worker.join(timeout=2)
        self._poll()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('runtime', type=Path)
    args = parser.parse_args()
    # The existing local analysis process may still be completing a song.
    while True:
        try:
            run_batch(args.directory, args.runtime)
            return
        except WorkerBusy:
            with connect(args.directory) as db:
                if db.execute('SELECT state FROM batch').fetchone()['state'] != 'running':
                    return
            time.sleep(.5)


if __name__ == '__main__':
    main()
