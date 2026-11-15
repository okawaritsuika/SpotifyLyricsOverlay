"""Browse and edit user-supplied resources by their exact lyric layout."""
from dataclasses import replace
import hashlib
import math
import os
from threading import Lock

from lyric_annotations import LyricAnnotations, LyricTextEdits
from lyric_cache import MAX_ENTRY_BYTES, _rows
from lyrics import Line, Song
from live_timing import LiveTimingStore
from playlist_lyrics_store import _read, _safe
from storage import validate_offset, validate_times
from sync import edit_manual_time, restore_auto_time
from timing_quality import cleared_rows, timing_origins
from user_annotations import SingerEdits, SingerRoster, UserAnnotations


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _key(song):
    return '\0'.join((song.title, song.artist, song.album))


def _json(path):
    try:
        return _read(path, MAX_ENTRY_BYTES)
    except (ValueError, OSError, RecursionError):
        return None


def _timing_rows(timing):
    if not isinstance(timing, dict):return {}, set(), 0
    try:
        times = timing.get('times')
        validate_times(times, len(times))
        offset = timing.get('offset', 0)
        validate_offset(offset)
    except (ValueError, TypeError, OverflowError):
        return {}, set(), 0
    origins = timing.get('origins')
    if timing.get('source') == 'LRCLIB':
        origins = origins if isinstance(origins, list) and len(origins) == len(times) else ()
    else:origins = timing_origins(timing)
    manual = {i: max(0., stamp + offset) for i, (stamp, origin) in enumerate(zip(times, origins))
              if stamp is not None and origin == 'manual'}
    return manual, cleared_rows(timing), offset


class _Resource:
    def __init__(self, key, lines):
        self.key = key
        self.title, artist, self.album = key.split('\0')
        self.artists = [artist] if artist else []
        self.base_lines = self.lines = tuple(lines)
        self.originals, self.translated, self.performers = set(), set(), set()
        self.times, self.cleared, self.offset = {}, set(), 0
        self.text, self.singers = {}, {}
        self.explicit_text = {}
        self.explicit_singers = set()
        self.ai_rows = {}
        self.clock = None
        self.clock_has_user_values = False
        self.ambiguous_clock = False
        self.legacy = False
        self.roster = None
        self.sources = set()
        self.spotify = None
        self.confirmed = False

    def add_timing(self, timing, target=None):
        times, cleared, offset = _timing_rows(timing)
        manual = bool(times or cleared or offset)
        if target:
            if self.clock is None or not self.clock_has_user_values:
                self.clock = target
            elif manual and (self.clock[0], self.clock[1].id) != (target[0], target[1].id):
                self.ambiguous_clock = True
            self.clock_has_user_values = self.clock_has_user_values or manual
        self.times.update(times)
        self.cleared.update(cleared)
        self.offset = offset or self.offset
        if manual:self.sources.add('수동 싱크')
        if not isinstance(timing, dict):return
        added = timing.get('added_alsong_rows', ())
        if not isinstance(added, (list, tuple)):added = ()
        for entry in added:
            if not isinstance(entry, dict) or not isinstance(entry.get('text'), str) or not entry['text'].strip():continue
            index = len(self.lines)
            self.lines += (Line(entry['text'], '', ''),)
            self.originals.add(index)
            stamp = entry.get('time')
            try:validate_times([stamp], 1)
            except (ValueError, OverflowError):continue
            if stamp is not None:self.times[index] = max(0., stamp + offset)
        if added or timing.get('removed_alsong_rows'):
            self.confirmed = True
            self.sources.add('직접 수정')

    def add_text(self, rows, label='직접 수정', *, overwrite=True):
        if not rows:return
        self.sources.add(label)
        for index, saved in rows.items():
            fields = saved if isinstance(saved, dict) else {'original': saved}
            previous = self.text.get(index, {})
            self.text[index] = (previous | fields) if overwrite else (fields | previous)
            if overwrite:self.explicit_text[index] = set(fields)
            if fields.keys() & {'original', 'reading'}:self.originals.add(index)
            if 'korean' in fields:self.translated.add(index)

    def add_singers(self, rows, cleared=()):
        if rows or cleared:self.sources.add('직접 지정')
        self.performers.update(rows)
        self.performers.update(cleared)
        self.singers.update(rows)
        self.singers.update({index: () for index in cleared})

    def actions(self):
        if self.legacy:return set()
        fields = {'roster'}
        if self.lines:fields.update(('original', 'reading', 'korean', 'singers'))
        if self.clock and not self.ambiguous_clock:fields.add('timing')
        return fields

    def describe(self, ident):
        track = self.spotify
        return dict(id=ident, title=self.title, artists=track['artists'] if track else self.artists,
                    album=self.album, source=' · '.join(sorted(self.sources)),
                    total=len(self.lines), translated=len(self.translated), original=len(self.originals) or int(self.confirmed),
                    manual_sync=len(self.times.keys() | self.cleared) or int(bool(self.offset)),
                    performers=len(self.performers) + (max(1, len(self.roster)) if self.roster is not None else 0),
                    included=track['included'] if track else None, roster=self.roster or (),
                    roster_saved=self.roster is not None, offset=self.offset)

    def preview(self):
        for index in sorted(set(range(len(self.lines))) | self.text.keys() | self.performers | self.times.keys() | self.cleared):
            line = self.lines[index] if index < len(self.lines) else Line('', '', '')
            removable = set(self.explicit_text.get(index, ()))
            if index in self.ai_rows:removable.add('korean')
            if index in self.explicit_singers:removable.add('singers')
            if index in self.times or index in self.cleared:removable.add('timing')
            yield dict(index=index + 1, original=line.original, reading=line.reading, korean=line.korean,
                       time=self.times.get(index), performers=self.singers.get(index, ()),
                       removable=tuple(sorted(removable))) | self.text.get(index, {})


class PersonalResources:
    def __init__(self, app):
        self.app = app
        self.directory = app.store.directory
        self.items = {}
        self.playlist_records = {}
        self.refresh_lock = Lock()

    def _files(self, folder):
        path = self.directory / folder
        return {candidate for candidate in path.glob('*.json') if not candidate.is_symlink()} if _safe(path) else set()

    def refresh(self):
        with self.refresh_lock:return self._refresh()

    def _playlist_lyrics(self, store):
        directory = store.tracks_directory
        if not _safe(directory):return
        try:
            with os.scandir(directory) as entries:
                stamps = {entry.name: entry.stat(follow_symlinks=False).st_mtime_ns for entry in entries
                          if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False)}
        except FileNotFoundError:return
        updated = {}
        for track in store.metadata():
            name = track['id'] + '.json'
            if name not in stamps:continue
            stamp = stamps[name]
            previous = self.playlist_records.get(name)
            if previous and previous[0] == stamp:
                stamp, lines, reviewed = previous
            else:
                value = _json(directory / name)
                if not isinstance(value, dict):continue
                try:lines = _rows(value['lines'], decoded=True)
                except (KeyError, ValueError, TypeError):continue
                markers = value.get('sources')
                reviewed = isinstance(markers, list) and 'reviewed_translation' in markers
            updated[name] = (stamp, lines, reviewed)
            yield track, lines, reviewed
        self.playlist_records = updated

    def _refresh(self):
        app = self.app
        rows, seen = {}, set()
        files = {folder: self._files(folder) for folder in (
            'timings', 'live_timings', 'lyric_text_edits', 'singer_edits', 'singer_rosters', 'confirmed_alsong')}
        timing = LiveTimingStore(self.directory)
        text_edits = LyricTextEdits(app.store)
        singer_edits = SingerEdits(self.directory)
        roster = SingerRoster(self.directory)
        annotations = LyricAnnotations(app.store)
        sources = app.settings.get('lyric_sources', {}).copy()
        aliases = {}
        for key, song_id in app.settings.get('links', {}).copy().items():
            if key.count('\0') == 2:aliases.setdefault(song_id, set()).add(key)
        imported = {track['id']: track for track in app.spotify_translation_sources.records()}
        checked_alsong = set()

        def resource(key, lines, variant=None):
            identity = (key, tuple(line.original for line in lines))
            if variant:identity += (variant,)
            # Keys never contain unrelated settings, and identical layouts group together.
            if identity not in rows:rows[identity] = _Resource(key, lines)
            return rows[identity]

        def inspect_layout(key, song, *, alsong=False):
            if not key.split('\0')[0]:return
            layout = (key, tuple(line.original for line in song.lines))
            code = _hash(key)
            roster_path = roster.directory / (code + '.json')
            if roster_path in files['singer_rosters'] and roster_path not in seen:
                try:
                    roster.select(key)
                    if roster.names is not None:
                        item = resource(key, song.lines)
                        item.roster = roster.names
                        item.sources.add('참여 가창자')
                        seen.add(roster_path)
                except (ValueError, OSError):pass
            if song.lines:
                text_path, singer_path = text_edits.path_for(layout), singer_edits.path_for(layout)
                if text_path in files['lyric_text_edits'] and text_path not in seen:
                    text_edits.select(layout)
                    if not text_edits.error:
                        if text_edits.rows:resource(key, song.lines).add_text(text_edits.rows)
                        seen.add(text_path)
                if singer_path in files['singer_edits'] and singer_path not in seen:
                    singer_edits.select(layout)
                    if not singer_edits.error:
                        if singer_edits.rows or singer_edits.cleared:
                            item = resource(key, song.lines)
                            item.add_singers(singer_edits.rows, singer_edits.cleared)
                            item.explicit_singers.update(singer_edits.rows.keys() | singer_edits.cleared)
                        seen.add(singer_path)
                ids = (song.id,) if alsong else (f'memory-{code[:24]}', f'completed-{code[:24]}')
                for ident in ids:
                    candidate = replace(song, id=ident)
                    path = timing.path(candidate)
                    if path not in files['live_timings'] or path in seen:continue
                    try:value = timing.load(candidate)
                    except (OSError, ValueError):continue
                    if value is not None:
                        resource(key, song.lines).add_timing(value, ('live', candidate))
                        seen.add(path)
            path = self.directory / 'confirmed_alsong' / (code + '.json')
            if path in files['confirmed_alsong'] and path not in checked_alsong:
                checked_alsong.add(path)
                value = _json(path)
                if not isinstance(value, dict):return
                try:
                    lines = _rows(value['lines'], decoded=True)
                    provider = value['provider_id']
                    if not isinstance(provider, str) or not provider.isascii() or not provider.isdecimal():return
                except (KeyError, ValueError, TypeError):return
                item = resource(key, lines)
                item.sources.add('알송 직접 연결')
                item.confirmed = True
                seen.add(path)
                inspect_layout(key, Song(f'alsong-{code[:24]}-{provider}', *key.split('\0'), lines), alsong=True)

        # Exact library links know the stored labels even when playback credits differ.
        for song in list(app.songs):
            key = _key(song)
            song_aliases = aliases.get(song.id, ())
            linked_keys = sorted(song_aliases)
            primary_key = linked_keys[0] if len(linked_keys) == 1 else key
            for candidate in dict.fromkeys((primary_key, key, *linked_keys)):
                inspect_layout(candidate, song)
            item = resource(primary_key, song.lines)
            if item.clock is None:item.clock = ('library', song)
            path = self.directory / 'timings' / (song.id + '.json')
            if path in files['timings'] and path not in seen:
                try:
                    value = app.store.timing(song)
                    if value.get('fingerprint') == song.fingerprint:
                        item.add_timing(value, ('library', song))
                        seen.add(path)
                except (OSError, ValueError):pass
            if song.lines:
                meta = sources.get(song.id, {})
                item = resource(primary_key, song.lines)
                item.sources.add('직접 수정' if isinstance(meta, dict) and meta.get('user_override') else '저장한 원문')
                item.originals.update(range(len(song.lines)))
                item.translated.update(i for i,line in enumerate(song.lines) if line.korean.strip())
            try: extra = annotations.load(song)
            except (OSError, ValueError):continue
            supplemental = {i: {'korean': line.korean} for i,line in enumerate(extra)
                            if line and line.korean.strip() and not song.lines[i].korean.strip()}
            if supplemental:resource(primary_key, song.lines).add_text(supplemental, '저장한 번역', overwrite=False)

        active_key, active_song = app.playback.key, app.song
        if active_song and active_key == app.playback.key:inspect_layout(active_key, active_song)

        for playback, saved in app.lyric_cache.entries():
            inspect_layout(playback.key, Song('', playback.title, playback.artist, playback.album, saved['lines']))

        archive = app.batch_translations
        if archive:
            with archive.lock:
                translated = {ident: dict(entries) for ident, entries in archive.imports.items() if entries}
            for doc in archive.documents():
                song = Song('', doc['title'], doc['artist'], doc['album'], doc['lines'])
                key = _key(song)
                inspect_layout(key, song)
                if entries := translated.get(doc['id']):
                    indices = [int(index) - 1 for index in entries if 1 <= int(index) <= len(song.lines)]
                    text = {i: {'korean': song.lines[i].korean} for i in indices}
                    item = resource(key, song.lines)
                    item.add_text(text, '저장한 AI 번역', overwrite=False)
                    item.ai_rows.update({i: doc['id'] for i in indices})

        for track, lines, reviewed in self._playlist_lyrics(app.playlist_lyrics.store):
            ident = track['id']
            song = Song('', track['title'], track['artists'][0], track['album'], lines)
            key = _key(song)
            inspect_layout(key, song)
            if ident in imported:
                item = resource(key, lines)
                if item.spotify and item.spotify['id'] != ident:item = resource(key, lines, ident)
                item.spotify = imported[ident]
                item.sources.add('Spotify에서 가져옴')
                item.originals.update(range(len(lines)))
            if reviewed:
                translations = {i: {'korean': line.korean} for i,line in enumerate(lines) if line.korean.strip()}
                if translations:resource(key, lines).add_text(translations, '검토한 번역', overwrite=False)

        # Explicit import documents are browsed directly, never via a backup of settings.
        for folder in ('user_lyrics', 'manual_annotations'):
            for path in sorted(self._files(folder)):
                value = _json(path)
                if not isinstance(value, dict) or not isinstance(value.get('title'), str):continue
                try:
                    if folder == 'user_lyrics':UserAnnotations._document(value)
                    lines = _rows([{name: row.get(name, '') for name in ('original', 'reading', 'korean')}
                                   for row in value['lines']], decoded=True)
                except (KeyError, AttributeError, TypeError, ValueError):continue
                artists = value.get('artists', ())
                artist = artists[0] if isinstance(artists, list) and artists else value.get('artist', '')
                album = value.get('album', '')
                if not isinstance(artist, str) or not isinstance(album, str):continue
                key = _key(Song('', value['title'], artist, album, lines))
                if key.count('\0') != 2:continue
                item = resource(key, lines)
                item.sources.add('직접 가져온 가사')
                item.originals.update(range(len(lines)))
                item.translated.update(i for i,line in enumerate(lines) if line.korean.strip())
                item.add_singers({i: tuple(names) for i,row in enumerate(value['lines'])
                                  if isinstance(names := row.get('singers'), list) and names
                                  and all(isinstance(name, str) for name in names)})

        items = {_hash(repr(identity)): item for identity,item in rows.items() if item.sources}
        self._include_legacy(items, files, seen)
        self.items = items
        return sorted((item.describe(ident) for ident, item in items.items()),
                      key=lambda item: (item['title'].startswith('제목 미확인'), item['title'].casefold(), item['album']))

    def _include_legacy(self, items, files, seen):
        """Old hashed files may survive after their original layout gets evicted."""
        for folder, paths in files.items():
            for path in paths - seen:
                value = _json(path)
                item = _Resource('제목 미확인\0\0', ())
                item.legacy = True
                if folder in ('timings', 'live_timings'):
                    item.add_timing(value)
                elif folder == 'singer_rosters' and isinstance(value, list) and SingerRoster.valid_names(value):
                    item.roster = tuple(value)
                    item.sources.add('참여 가창자')
                elif folder in ('singer_edits', 'lyric_text_edits') and isinstance(value, dict):
                    rows = {int(i): row for i,row in value.items() if i.isascii() and i.isdecimal() and len(i) <= 4}
                    if folder == 'lyric_text_edits':
                        item.add_text({i: row for i,row in rows.items() if LyricTextEdits.valid_row(row)})
                    else:
                        assignments = {i: tuple(names) for i,row in rows.items()
                                       if isinstance(names := row.get('carry') if isinstance(row, dict) else row, list)
                                       and SingerRoster.valid_names(names)}
                        item.add_singers(assignments)
                elif folder == 'confirmed_alsong' and isinstance(value, dict):
                    try:item.lines = _rows(value['lines'], decoded=True)
                    except (KeyError, ValueError, TypeError):continue
                    item.confirmed = True
                    item.sources.add('알송 직접 연결')
                if item.sources:
                    items[folder + '/' + path.stem] = item

    def details(self, ident):
        if not isinstance(ident, str) or ident not in self.items:raise ValueError('저장한 자료를 다시 선택하세요.')
        return tuple(self.items[ident].preview())

    def actions(self, ident):
        item = self.items.get(ident)
        return item.actions() if item else set()

    def update(self, ident, index, field, value, parse_time):
        item = self.items.get(ident)
        if not item or field not in item.actions():raise ValueError('이 자료는 원본을 확인한 뒤 수정할 수 있습니다.')
        if field == 'roster':
            roster = SingerRoster(self.directory)
            if not _safe(roster.directory / (_hash(item.key) + '.json')):
                raise ValueError('가창자 명단의 저장 위치를 확인할 수 없습니다.')
            roster.select(item.key)
            if roster.error:raise roster.error
            if value is None:roster.remove()
            else:roster.save(self._names(value))
            return item, None
        if type(index) is not int or not 0 <= index < len(item.lines):
            raise ValueError('수정할 가사 줄을 다시 선택하세요.')
        if field == 'timing':
            return item, self._update_timing(item, index, value, parse_time)

        layout = (item.key, tuple(line.original for line in item.lines))
        path_key = (item.key, tuple(line.original for line in item.base_lines))
        if field == 'singers':
            edits = SingerEdits(self.directory)
            if not _safe(edits.path_for(path_key)):raise ValueError('가창자 저장 위치를 확인할 수 없습니다.')
            edits.select(layout, path_key=path_key)
            if edits.error:raise edits.error
            names = None if value is None else self._names(value)
            edits.save(index, names, keep_carry=names == ())
        else:
            edits = LyricTextEdits(self.app.store)
            if not _safe(edits.path_for(path_key)):raise ValueError('가사 수정 위치를 확인할 수 없습니다.')
            edits.select(layout, path_key=path_key)
            if edits.error:raise edits.error
            stored = edits.rows.get(index)
            ai_id = item.ai_rows.get(index) if field == 'korean' else None
            if value is None:
                if ai_id and not (isinstance(stored, dict) and 'korean' in stored):
                    # Retained lyric snapshots can contain the same old AI result.
                    edits.save(index, '', field='korean')
                    try:self.app.batch_translations.remove_translation(ai_id, index)
                    except (OSError, ValueError):
                        edits.remove(index, 'korean')
                        raise
                else:edits.remove(index, field)
            else:
                if not isinstance(value, str) or len(value) > 4000:
                    raise ValueError('가사는 한 줄에 4,000자까지 입력할 수 있습니다.')
                edits.save(index, value, field=field)
                if ai_id and value.strip():
                    try:self.app.batch_translations.update_translation(ai_id, index, value.strip())
                    except (OSError, ValueError):
                        if isinstance(stored, dict) and 'korean' in stored:
                            edits.save(index, stored['korean'], field='korean')
                        else:edits.remove(index, 'korean')
                        raise
        return item, None

    @staticmethod
    def _names(value):
        if not isinstance(value, list) or not SingerRoster.valid_names(value):
            raise ValueError('가창자는 서로 다른 이름으로 10명까지 지정할 수 있습니다.')
        return tuple(value)

    def _update_timing(self, item, index, value, parse_time):
        kind, song = item.clock
        live = LiveTimingStore(self.directory)
        store = self.app.store
        path = live.path(song) if kind == 'live' else self.directory / 'timings' / (song.id + '.json')
        if not _safe(path):raise ValueError('저장한 싱크 파일을 확인할 수 없습니다.')
        if kind == 'live':
            timing = live.load(song)
            if not timing:raise ValueError('가사 구성 또는 싱크가 바뀌었습니다. 새로고침하세요.')
        else:
            if not any(current.id == song.id and current.fingerprint == song.fingerprint for current in self.app.songs):
                raise ValueError('저장된 원문이 바뀌었습니다. 새로고침해 주세요.')
            timing = store.timing(song)
            if path.exists() and timing.get('fingerprint') != song.fingerprint:
                raise ValueError('가사 구성이 바뀌었습니다. 새로고침한 뒤 다시 수정하세요.')
        if index >= len(timing['times']):raise ValueError('추가한 알송 가사 줄의 싱크는 개발자 모드에서 수정하세요.')
        if timing.get('source') == 'LRCLIB' and not isinstance(timing.get('origins'), list):
            timing = {**timing, 'origins': ['audio' if stamp is not None else None for stamp in timing['times']]}
        manual, cleared, offset = _timing_rows(timing)
        if value is None:
            if index not in manual and index not in cleared:raise ValueError('이 줄에 직접 저장한 싱크가 없습니다.')
            updated = restore_auto_time(timing, index)
        else:
            if not isinstance(value, str):raise ValueError('저장할 싱크 시각을 입력하세요.')
            stamp = parse_time(value)
            if stamp is None or not math.isfinite(stamp) or stamp < 0 or stamp < offset:
                raise ValueError('저장할 싱크 시각과 곡의 오프셋을 확인하세요.')
            playback = self.app.playback
            if playback.key == item.key and playback.has_timeline and stamp > playback.duration:
                raise ValueError('재생 길이 안의 시각을 입력하세요.')
            updated = edit_manual_time(timing, index, round(stamp - offset, 3))
            updated['source'] = '직접 기록'
        if kind == 'live':live.save(song, updated)
        else:store.save_timing(song, updated)
        return updated

    def spotify_track(self, ident):
        item = self.items.get(ident)
        if item and item.spotify:return item.spotify['id']
        raise ValueError('Spotify에서 가져온 자료를 선택하세요.')
