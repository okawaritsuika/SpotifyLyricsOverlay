"""Persistent queue and recording-specific results for user-supplied audio."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timezone

from local_library import _safe_file
from storage import Store, validate_times


class WorkerBusy(ValueError):
    """Another worker already owns the local queue."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def automatic_alignment_engine(lines):
    """Use Japanese singing CTC only when the lyrics supply kana evidence."""
    from japanese_alignment import CTC, LEGACY
    kana_rows = sum(bool(re.search(r'[ぁ-ゖァ-ヺｦ-ﾟ]', line)) for line in lines)
    return CTC if kana_rows and kana_rows * 5 >= len(lines) else LEGACY


def local_alignment_config(settings, spec):
    from japanese_alignment import LEGACY
    if spec.get('alignment_recipe'):
        return dict(spec['alignment'])
    config = {**(settings.get('audio_alignment') or {}), **(spec.get('alignment') or {})}
    if config.get('engine') in (None, 'auto'):
        config['engine'] = (automatic_alignment_engine(spec.get('lines', ()))
                            if spec.get('kind') in ('lyrics', 'group') else LEGACY)
    if settings.get('audio_device') in ('auto', 'cpu', 'cuda'):
        config.setdefault('device', settings['audio_device'])
    return config


def alignment_quality(detail, start):
    if start is None or detail.get('status') in ('missing', 'conflict'):
        return 'low'
    return detail.get('quality') or ('low' if detail.get('reason') else 'medium')


def playlist_auto_times(job):
    """Return usable playlist candidates while retaining the full review result."""
    result = job['result']
    alignment = result.get('alignment', {})
    if (job['status'] != 'done' or not result.get('playlist_clock', {}).get('usable', True)
            or alignment.get('fallback_reason') or (alignment.get('requested_engine')
                and alignment['requested_engine'] != alignment.get('engine'))):
        return []
    times = alignment.get('times', [])
    details = alignment.get('line_alignment', [])
    if len(times) == len(details):
        times = [stamp if row.get('quality') != 'low' else None for stamp, row in zip(times, details)]
    matched = sum(stamp is not None for stamp in times)
    return times if (times and len(times) == len(job['spec']['lines'])
                     and matched >= min(3, len(times)) and matched / len(times) >= .6) else []


def lyric_digest(lines):
    return hashlib.sha256(_json(list(lines)).encode()).hexdigest()


def audio_digest(path):
    with Path(path).open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def alignment_recipe(spec, runtime):
    """Identify the computation independently of its recording link or file name."""
    from japanese_alignment import ALIGNMENT_VERSION, alignment_options

    config = spec['alignment']
    options = alignment_options(config, runtime)
    models = [runtime / 'models/whisper-small/model.bin']
    if config.get('device') in ('auto', 'cuda'):
        models.append(runtime / 'models/whisper-turbo/model.bin')
    if 'romaji_model_dir' in options:
        directory = Path(options['romaji_model_dir'])
        model = directory / 'model-logits.onnx'
        models.extend([model if model.is_file() else directory / 'model.onnx',
                       directory / 'phoneme_vocab.json'])
    models.extend(map(Path, options.get('pyshiro', {}).values()))
    artifacts = []
    for path in models:
        try:
            stat = path.stat()
            artifacts.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            artifacts.append((str(path.resolve()), None, None))
    body = [ALIGNMENT_VERSION, config, artifacts, spec['lines'], spec.get('line_readings')]
    return hashlib.sha256(_json(body).encode()).hexdigest()


def input_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('직접 선택한 일반 로컬 음원 파일을 지정하세요.')
    path = Path(os.path.abspath(path))
    if not _safe_file(path, [Path(path.anchor)]):
        raise ValueError('직접 선택한 일반 로컬 음원 파일을 지정하세요.')
    parts = tuple(part.casefold() for part in path.parts)
    if (any(part.startswith('spotifyab.spotifymusic_') for part in parts)
        or any(parts[index:index+2] in (('spotify', 'storage'), ('spotify', 'data'))
               for index in range(len(parts) - 1))):
        raise ValueError('Spotify 오프라인 캐시 대신 별도로 확보한 일반 음원을 선택하세요.')
    return path


@contextmanager
def worker_lock(directory, *, filename='local-analysis.lock'):
    """Release the queue lock even when a worker is killed."""
    with (Path(directory) / filename).open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as error:
            message = ('이 데이터 폴더에서 이미 로컬 음원 분석을 실행 중입니다.' if filename == 'local-analysis.lock'
                       else '같은 출력 경로에 쓰는 작업이 이미 실행 중입니다.')
            raise WorkerBusy(message) from error
        try:
            yield
        finally:
            unlock()


class AnalysisStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'local_analysis.sqlite3'
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    spec TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    stage TEXT NOT NULL DEFAULT 'registered', error TEXT,
                    result TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_audio_sha256 ON jobs (sha256);
                CREATE TABLE IF NOT EXISTS playback_links (
                    recording TEXT NOT NULL, lyrics TEXT NOT NULL, id TEXT NOT NULL,
                    PRIMARY KEY (recording, lyrics)
                );
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    character TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
                    sha256 TEXT NOT NULL, spec TEXT NOT NULL, samples TEXT NOT NULL,
                    PRIMARY KEY (character, kind, model, sha256)
                );
                CREATE TABLE IF NOT EXISTS singer_reviews (
                    recording TEXT NOT NULL, line INTEGER NOT NULL,
                    singers TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY (recording, line)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, path, spec, *, runtime=None):
        path = input_file(path)
        digest = audio_digest(path)
        kind = spec.get('kind')
        if kind not in ('lyrics', 'group', 'solo', 'speech'):
            raise ValueError('작업 종류는 lyrics, group, solo 또는 speech입니다.')
        if kind in ('solo', 'speech') and (
            not isinstance(spec.get('character'), dict) or
            not all(isinstance(spec['character'].get(key), str) and spec['character'][key].strip()
                    for key in ('id', 'name')) or
            not isinstance(spec.get('source'), str) or not spec['source'].strip() or
            spec.get('evidence') not in ('official', 'user_verified', 'verified_solo')
        ):
            raise ValueError('음성 등록에 캐릭터 ID·이름, 출처, 확인 방식이 필요합니다.')
        if spec.get('ranges') is not None:
            ranges = spec['ranges']
            if (kind not in ('solo', 'speech') or not isinstance(ranges, list) or not ranges
                or not all(isinstance(item, list) and len(item) == 2
                           and all(type(ms) is int for ms in item) and 0 <= item[0] < item[1]
                           for item in ranges)):
                raise ValueError('확인한 음성 구간은 시작·종료 밀리초 쌍의 배열로 지정하세요.')
        if kind in ('lyrics', 'group'):
            lines = spec.get('lines')
            if not isinstance(lines, list) or not lines or not all(isinstance(line, str) and line.strip() for line in lines):
                raise ValueError('가사 원문을 줄별로 지정하세요.')
            if spec.get('times') is not None:
                validate_times(spec['times'], len(lines))
        if kind == 'group':
            ids = spec.get('participants')
            if (not isinstance(ids, list) or not ids or
                not all(isinstance(item, str) and item.strip() for item in ids)):
                raise ValueError('이 곡에 참여한 캐릭터 ID를 입력하세요.')
            spec = {**spec, 'participants': sorted(set(ids))}
        if spec.get('track_id') and (not isinstance(spec['track_id'], str)
                                     or not re.fullmatch(r'[0-9A-Za-z]{22}', spec['track_id'])):
            raise ValueError('Spotify Track ID는 22자리입니다.')
        if spec.get('recording') and (type(spec.get('expected_duration')) not in (float, int)
                                      or spec['expected_duration'] <= 0):
            raise ValueError('녹음을 연결하려면 재생 길이(초)가 필요합니다.')
        if kind == 'lyrics' or kind == 'group' and (spec.get('times') is None or spec.get('recompute_alignment')):
            from app_paths import runtime_dir

            config = local_alignment_config(Store(self.directory).settings(), spec)
            spec = {**spec, 'alignment': config}
            spec['alignment_recipe'] = alignment_recipe(spec, Path(runtime or runtime_dir()))
        body = _json(spec)
        job_id = hashlib.sha256((digest + body).encode()).hexdigest()
        with self.connect() as db:
            db.execute("""
                INSERT INTO jobs (id, path, sha256, spec, updated) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET path=excluded.path
            """, (job_id, str(path), digest, body, time.time()))
            if spec.get('recording') and spec.get('lines'):
                db.execute("""
                    INSERT INTO playback_links VALUES (?, ?, ?)
                    ON CONFLICT(recording, lyrics) DO UPDATE SET id=excluded.id
                """, (spec['recording'], lyric_digest(spec['lines']), job_id))
        return job_id

    def cached_alignment(self, job):
        recipe = job['spec'].get('alignment_recipe')
        if not recipe:
            return None
        with self.connect() as db:
            matches = db.execute("""
                SELECT id, result FROM jobs WHERE sha256=? AND id<>?
                    AND json_extract(spec, '$.alignment_recipe')=?
                    AND json_extract(result, '$.alignment.engine')=?
                    AND json_extract(result, '$.alignment.fallback_reason') IS NULL
                    AND coalesce(json_extract(result, '$.alignment.runtime.fallback'), 0)=0
                ORDER BY updated DESC LIMIT 1
            """, (job['sha256'], job['id'], recipe, job['spec']['alignment']['engine'])).fetchone()
        if matches:
            result = json.loads(matches['result'])
            return matches['id'], result['duration'], result['alignment']
        return None

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        for key in ('spec', 'result'):
            job[key] = json.loads(job[key])
        return job

    def get(self, job_id):
        with self.connect() as db:
            return self._with_reviews(db, self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()))

    def linked(self, recording, digest, duration):
        with self.connect() as db:
            job = self._job(db.execute("""
                SELECT jobs.* FROM jobs JOIN playback_links USING (id)
                WHERE recording=? AND lyrics=?
            """, (recording, digest)).fetchone())
            if job and abs(job['spec'].get('expected_duration', 0) - duration) <= 2:
                return self._with_reviews(db, job)
        return None

    @staticmethod
    def _review_key(job):
        spec = job['spec']
        return hashlib.sha256(_json([spec.get('recording') or spec.get('track_id'), job['sha256'],
                                    spec['lines']]).encode()).hexdigest()

    def _with_reviews(self, db, job):
        if job and job['spec']['kind'] in ('group', 'lyrics'):
            job['singer_reviews'] = {str(row['line']): json.loads(row['singers']) for row in db.execute(
                'SELECT line, singers FROM singer_reviews WHERE recording=?', (self._review_key(job),))}
        return job

    def review_singers(self, job_id, index, singers):
        job = self.get(job_id)
        if (not job or job['spec']['kind'] != 'group' or type(index) is not int
                or not 0 <= index < len(job['spec']['lines'])):
            raise ValueError('가창자를 확인할 가사 줄을 선택하세요.')
        if singers is not None and (not isinstance(singers, (list, tuple))
                                    or any(item not in job['spec']['participants'] for item in singers)):
            raise ValueError('곡 참여 후보에 있는 캐릭터 ID를 입력하세요.')
        with self.connect() as db:
            key = self._review_key(job)
            if singers is None:
                db.execute('DELETE FROM singer_reviews WHERE recording=? AND line=?', (key, index))
            else:
                db.execute('INSERT OR REPLACE INTO singer_reviews VALUES (?, ?, ?, ?)',
                           (key, index, _json(list(dict.fromkeys(singers))), time.time()))
            db.execute('UPDATE jobs SET updated=? WHERE id=?', (time.time(), job_id))

    def reviewed_labels(self, job_id):
        """Export explicit user labels without treating model guesses as evaluation truth."""
        job = self.get(job_id)
        if not job or job['spec']['kind'] != 'group':
            raise ValueError('검증용 가창 레이블을 내보낼 그룹 분석을 선택하세요.')
        alignment = job['result'].get('alignment', {})
        times = alignment.get('times') or job['spec'].get('times') or []
        rows = [{'index': index, 'lyric': job['spec']['lines'][index],
                 'start_s': times[index] if index < len(times) else None, 'singers': singers}
                for key, singers in job['singer_reviews'].items() for index in (int(key),)]
        return {'version': 1, 'audio_sha256': job['sha256'], 'track_id': job['spec'].get('track_id'),
                'title': job['spec'].get('title'), 'participants': job['spec']['participants'],
                'lyrics_sha256': lyric_digest(job['spec']['lines']), 'rows': sorted(rows, key=lambda row: row['index'])}

    def playlist_job(self, track_id, lines, duration, *, verified_only=False):
        from sync_reanalysis import DATABASE, applied_job, has_applied_job_times

        has_reanalysis = (self.directory / DATABASE).is_file()
        with self.connect() as db:
            rows = db.execute("""
                SELECT * FROM jobs WHERE json_extract(spec, '$.track_id')=?
                AND json_extract(spec, '$.kind') IN ('lyrics', 'group')
                AND json_extract(spec, '$.recording') IS NULL
                AND coalesce(json_extract(spec, '$.analysis_only'), 0)=0
                ORDER BY status='done' DESC, json_extract(spec, '$.kind')='group' DESC, updated DESC
            """, (track_id,))
            fallback = None
            for row in rows:
                job = self._job(row)
                measured = job['result'].get('duration', job['spec'].get('expected_duration'))
                if job['spec'].get('lines') == lines and type(measured) in (int, float) and abs(measured - duration) <= 2:
                    job = applied_job(self.directory, job)
                    if has_applied_job_times(job):
                        return self._with_reviews(db, job)
                    if verified_only and not playlist_auto_times(job):
                        continue
                    if fallback is None:
                        fallback = job
                    if not has_reanalysis:
                        break
            return self._with_reviews(db, fallback)

    def pending(self):
        with self.connect() as db:
            return [row['id'] for row in db.execute("""
                SELECT id FROM jobs WHERE status IN ('queued', 'running')
                ORDER BY CASE WHEN json_extract(spec, '$.kind') IN ('solo', 'speech') THEN 0 ELSE 1 END, updated
            """)]

    def update(self, job, status, stage, result, error=None):
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,stage=?,result=?,error=?,updated=? WHERE id=?',
                       (status, stage, _json(result), error, time.time(), job['id']))

    def save_profile(self, job, model, samples):
        spec = job['spec']
        char = spec['character']
        kind = 'speech' if spec['kind'] == 'speech' else 'singing'
        with self.connect() as db:
            db.execute("""
                INSERT INTO characters VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, actor=CASE WHEN excluded.actor='' THEN actor ELSE excluded.actor END
            """, (char['id'], char['name'], char.get('actor', '')))
            key = char['id'], kind, model, job['sha256']
            existing = db.execute("""
                SELECT samples FROM profiles WHERE character=? AND kind=? AND model=? AND sha256=?
            """, key).fetchone()
            saved = json.loads(existing['samples']) if existing else []
            combined = {(item['start_ms'], item['end_ms'], item.get('preparation')): item for item in saved}
            created_at = datetime.now(timezone.utc).isoformat()
            for sample in samples:
                item = {**sample, 'source': spec['source'], 'evidence': spec['evidence'],
                        'audio_sha256': job['sha256'], 'created_at': created_at}
                sample_key = item['start_ms'], item['end_ms'], item.get('preparation')
                if {key: value for key, value in combined.get(sample_key, {}).items()
                    if key != 'created_at'} != {key: value for key, value in item.items() if key != 'created_at'}:
                    combined[sample_key] = item
            if list(combined.values()) == saved:
                return
            db.execute('INSERT OR REPLACE INTO profiles VALUES (?, ?, ?, ?, ?, ?)',
                       (*key, _json(spec), _json(list(combined.values()))))
            if kind == 'singing':
                db.execute("""
                    UPDATE jobs SET status='queued', stage='aligned', error=NULL,
                    result=json_remove(json_set(result, '$.voice_last_completed', json_extract(result, '$.voice')),
                                       '$.voice', '$.seconds.voice')
                    WHERE status='done' AND json_extract(result, '$.voice.model')=?
                      AND EXISTS (SELECT 1 FROM json_each(jobs.spec, '$.participants') WHERE value=?)
                """, (model, char['id']))

    def profiles(self, participants, model, *, exclude_sha256='', exclude_track_id=''):
        samples, names = {item: [] for item in participants}, {}
        with self.connect() as db:
            for item in participants:
                rows = db.execute("""
                SELECT samples, name, sha256 FROM profiles JOIN characters ON character=characters.id
                    WHERE character=? AND kind='singing' AND model=? AND sha256!=?
                      AND coalesce(json_extract(spec, '$.track_id'), '') != CASE WHEN ?='' THEN '__none__' ELSE ? END
                    ORDER BY sha256
                """, (item, model, exclude_sha256, exclude_track_id, exclude_track_id))
                for row in rows:
                    names[item] = row['name']
                    samples[item].extend({**sample, 'audio_sha256': row['sha256']}
                                         for sample in json.loads(row['samples']))
        return samples, names
