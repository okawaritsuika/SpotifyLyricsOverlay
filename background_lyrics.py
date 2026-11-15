"""Background lyric providers; credentials stay inside the Spotify browser worker."""
import json
from pathlib import Path
import secrets
import stat
import subprocess
import threading
import time

from lyrics import Line,normalized
from lyric_follow import native_line_mapping
from lyric_match import match_lyrics
from public_lyrics import fetch_public_lyrics
from storage import validate_times
from artist_metadata import ArtistMetadata,playback_request,request_identity,validated_track
from artist_credit_overrides import ArtistCreditOverrides,orchestra_singer_credits
from spotify_api import select_track,text_key
from spotify_catalog import catalog_diagnostic_record
from app_paths import resource_dir, worker_command, worker_environment


CATALOG_TIMEOUT=35


def _file_stamp(path):
    try:info=path.stat()
    except OSError:return None
    return (info.st_mtime_ns,info.st_size) if stat.S_ISREG(info.st_mode) else None


class CatalogSearchError(Exception):
    def __init__(self,message,code=''):
        super().__init__(message)
        self.code=code


class SpotifyJob:
    def __init__(self,base,profile,messages,visible=False):
        self.messages=messages;self.closed=False;self.lock=threading.Lock()
        environment=worker_environment(base,browser=True,reuse_frozen_bundle=True)
        self.process=subprocess.Popen(worker_command(base,'spotify_worker'),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,encoding='utf-8',env=environment,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
        self.send({'profile':str(profile),'visible':visible})
        self.thread=threading.Thread(target=self._read,daemon=True,name='spotify-api-reader');self.thread.start()

    def send(self,command):
        with self.lock:
            if self.closed:return
            try:self.process.stdin.write(json.dumps(command,ensure_ascii=False)+'\n');self.process.stdin.flush()
            except (OSError,ValueError):pass

    def _read(self):
        try:
            for line in self.process.stdout:
                try:event=json.loads(line)
                except ValueError:continue
                if not isinstance(event,dict):continue
                if not self.closed:self.messages.put(('background_lyrics',(self,event)))
        finally:
            self.process.stdout.close()
            try:self.process.stdin.close()
            except OSError:pass
            try:exit_code=self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate();exit_code=self.process.wait(timeout=3)
            if not self.closed:self.messages.put(('background_lyrics',(self,{'kind':'finished','exit_code':exit_code})))

    def close(self):
        if self.closed:return
        self.send({'action':'close'});self.closed=True
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=3)
        try:self.process.stdin.close()
        except OSError:pass
        self.thread.join(timeout=1)


class BackgroundLyrics:
    def __init__(self,base,directory,messages,job_factory=SpotifyJob,fetch=fetch_public_lyrics,reports_status=None,
                 *,defer_catalog=False):
        self.base=Path(base);self.profile=Path(directory)/'spotify_profile';self.messages=messages
        self.job_factory=job_factory;self.fetch=fetch;self.job=None;self.playback=None
        self.merge_metadata=fetch is fetch_public_lyrics
        self.reports_status=(fetch is fetch_public_lyrics) if reports_status is None else reports_status
        self.key='';self.request_key='';self.generation=0;self.enabled=False;self.closed=False
        self.cached_sync=False;self.spotify_pending=False
        self.spotify_sync_present=False;self._spotify_sync_hint=False;self._spotify_timed_result=False
        self.spotify_has_complete_sync=False
        self.spotify_missing_lyrics=''
        self.public_enabled=True
        self.public_running=False;self.public_done=False;self.public_retry=0;self.retry_at=0
        self.status='공개 가사 대기 · Spotify 직접 연결은 로그인 필요'
        self.spotify_status='Spotify 로그인 연결 필요';self.spotify_state='login_required'
        self.spotify_authenticated=False
        self.spotify_requested=False;self.spotify_retry_count=0;self.spotify_retry_at=0
        self.spotify_last_error='';self.spotify_exit_code=None
        self.spotify_catalog_diagnostics=[]
        self.artist_metadata=ArtistMetadata(directory)
        self.artist_credit_overrides=ArtistCreditOverrides(directory)
        self.artist_pool_path=Path(directory)/'spotify_public_tracks.json'
        self.artist_pool_stamp=None;self.artist_pool={}
        self.catalog_credits=None;self.catalog_credits_stamp=None
        self.defer_catalog=defer_catalog;self.artist_catalog_loading=None
        self.artist_catalog_held=False
        self.artist_catalog_paths=(resource_dir()/'resources'/'song_catalog.json',Path(directory)/'song_catalog.json')
        self.metadata_attempted=None;self.metadata_pending=False
        self._catalog_lock=threading.Lock();self._catalog_waiters={}

    @property
    def awaiting_spotify(self):
        return bool(self.enabled and self.spotify_requested and (self.spotify_pending or self.spotify_retry_at))

    def catalog_search(self,title,artist='',album='',cancel=None):
        """Read search facts through the existing signed-in WebView, off the UI thread."""
        if not isinstance(title,str) or not title.strip() or len(title)>4000:
            raise CatalogSearchError('검색할 곡명을 확인해 주세요.','query')
        if self.closed:
            raise CatalogSearchError('가사 앱을 닫아 곡 검색을 취소했습니다.','cancelled')
        job=self.job
        if job is None or not self.spotify_authenticated:
            raise CatalogSearchError('기존 Spotify 연결 창에서 먼저 로그인해 주세요.','login_required')
        request_id=secrets.token_hex(16)
        waiter={'job':job,'event':threading.Event(),'result':None}
        with self._catalog_lock:self._catalog_waiters[request_id]=waiter
        deadline=time.monotonic()+CATALOG_TIMEOUT
        try:
            if cancel is not None and cancel.is_set():
                raise CatalogSearchError('Spotify 곡 검색을 취소했습니다.','cancelled')
            # Search with the title only; performer/version comparison belongs
            # to the explicit candidate chooser, not a guessed automatic match.
            job.send({'action':'catalog_search','request_id':request_id,'title':title.strip()})
            while not waiter['event'].wait(.1):
                if self.closed or cancel is not None and cancel.is_set():
                    raise CatalogSearchError('Spotify 곡 검색을 취소했습니다.','cancelled')
                if self.job is not job:
                    raise CatalogSearchError('Spotify 연결이 변경되어 곡 검색을 중단했습니다.','interrupted')
                if time.monotonic()>=deadline:
                    raise CatalogSearchError('Spotify 곡 검색이 지연되었습니다. 잠시 후 다시 시도해 주세요.','timeout')
            if cancel is not None and cancel.is_set():
                raise CatalogSearchError('Spotify 곡 검색을 취소했습니다.','cancelled')
            result=waiter['result']
            if result.get('error'):
                raise CatalogSearchError(result['error'],result.get('code','response'))
            tracks=result.get('tracks')
            if not isinstance(tracks,list):
                raise CatalogSearchError('Spotify 곡 검색 결과가 올바르지 않습니다.','response')
            return tracks
        finally:
            with self._catalog_lock:self._catalog_waiters.pop(request_id,None)
            if not waiter['event'].is_set():job.send({'action':'catalog_cancel','request_id':request_id})

    def _cancel_catalog(self,message,code='interrupted'):
        with self._catalog_lock:
            for waiter in self._catalog_waiters.values():
                waiter['result']={'error':message,'code':code};waiter['event'].set()

    def connect(self,visible=True):
        if self.closed:return
        self.spotify_requested=True;self.spotify_retry_count=0;self.spotify_retry_at=0
        self.metadata_attempted=None
        self.spotify_last_error='';self.spotify_exit_code=None
        if self.job:self.job.send({'action':'show'} if visible else {'action':'refresh'})
        elif not self._start_spotify(visible):return
        self.spotify_status='Spotify 로그인 창 연결 중' if visible else 'Spotify 로그인 세션 확인 중'
        self.spotify_state='connecting'
        if self.playback and self.enabled:self._lookup()

    def _start_spotify(self,visible=False):
        try:
            self.job=self.job_factory(self.base,self.profile,self.messages,visible=visible)
            self.spotify_catalog_diagnostics=[]
        except Exception as error:
            self.spotify_last_error=f'Spotify 연결 프로세스 시작 실패 ({type(error).__name__})'
            self.spotify_exit_code=None;self._schedule_spotify_retry()
            return False
        return True

    def _schedule_spotify_retry(self):
        self.spotify_retry_at=0;self.spotify_state='error'
        detail=self.spotify_last_error or 'Spotify 연결 프로세스가 예상치 않게 종료되었습니다.'
        if self.spotify_exit_code is not None:detail+=f' (종료 코드 {self.spotify_exit_code})'
        if self.spotify_requested and self.enabled and not self.cached_sync and not self.closed and self.spotify_retry_count<2:
            delay=(5,15)[self.spotify_retry_count]
            self.spotify_retry_at=time.monotonic()+delay
            self.spotify_state='reconnecting'
            detail+=f' · {delay}초 후 기존 로그인으로 자동 재연결'
        elif self.spotify_requested and self.enabled and self.spotify_retry_count>=2:
            detail+=' · 자동 재연결 2회 실패 · Spotify 연결에서 다시 시도하세요.'
        self.spotify_status=detail

    def _retry_spotify(self):
        if (self.job or not self.spotify_requested or not self.enabled or self.cached_sync or not self.spotify_retry_at
                or time.monotonic()<self.spotify_retry_at):return
        self.spotify_retry_at=0;self.spotify_retry_count+=1
        if self._start_spotify(visible=False):
            self.spotify_state='reconnecting'
            self.spotify_status=f'기존 Spotify 로그인으로 자동 재연결 중 ({self.spotify_retry_count}/2)'
            self._lookup()

    def _lookup(self):
        if self.job and self.playback and self.playback.has_timeline and self.playback.artist:
            p=self.playback
            command={'action':'lookup','key':self.request_key,'title':p.title,'artist':p.artist,
                     'album':p.album,'duration':p.duration}
            if self.cached_sync:
                identity=request_identity(playback_request(p))
                if (not self.enabled or not self.spotify_requested or identity is None or self.artist_catalog_held
                        or self.display_artists(p) or self.artist_catalog_loading is not None
                        or identity==self.metadata_attempted):return
                self.metadata_attempted=identity;self.metadata_pending=True
                command['metadata_only']=True
            else:
                self.spotify_pending=True
                request=playback_request(p)
                entry=self.artist_metadata.entries.get(request_identity(request))
                if entry and p.album.strip() and (track:=validated_track(request,entry.get('track'))):
                    command['verified_track']=track
            self.job.send(command)

    def display_artists(self,playback):
        """Complete verified credits, or () until known; never changes playback identity."""
        names=self.artist_credit_overrides.get(playback) or orchestra_singer_credits(playback)
        if names:return names
        names=self._catalog_display_artists(playback)
        if names:return names
        names=self.artist_metadata.get(playback)
        if names:return names
        request=playback_request(playback)
        if request_identity(request) is None or not request['album'].strip():return ()
        # The catalog may already hold the full credit list even when the web
        # search failed. Reuse only a uniquely matching recording, offline too.
        if not self.defer_catalog:
            stamp=_file_stamp(self.artist_pool_path)
            if stamp is None:return ()
            if stamp!=self.artist_pool_stamp:
                self.artist_pool=self._read_artist_pool();self.artist_pool_stamp=stamp
        track=select_track(request,self.artist_pool.get(text_key(playback.title),[]))
        if track and self.artist_metadata.put(playback,track):return tuple(track['artists'])
        return ()

    def _catalog_display_artists(self,playback):
        if not playback.title:return ()
        if self.defer_catalog:
            self._defer_artist_catalog()
        else:
            stamp=tuple(_file_stamp(path) for path in self.artist_catalog_paths)
            if self.catalog_credits is None or stamp!=self.catalog_credits_stamp:
                self.catalog_credits=self._read_catalog_credits();self.catalog_credits_stamp=stamp
        return self.catalog_credits.get(playback) if self.catalog_credits is not None else ()

    def _read_catalog_credits(self):
        from song_catalog import SongCatalog
        from catalog_display import CatalogDisplayCredits
        catalog=SongCatalog(*self.artist_catalog_paths)
        return CatalogDisplayCredits(catalog.songs,catalog.performers)

    def _read_artist_pool(self):
        from catalog_autolink import cached_pool
        pool={}
        for track in cached_pool(self.artist_pool_path.parent):
            pool.setdefault(text_key(track['title']),[]).append(track)
        return pool

    def _artist_catalog_stamp(self):
        return tuple(_file_stamp(path) for path in self.artist_catalog_paths),_file_stamp(self.artist_pool_path)

    def _defer_artist_catalog(self):
        if self.closed or self.artist_catalog_held or self.artist_catalog_loading is not None:return
        stamp=self._artist_catalog_stamp()
        current=self.catalog_credits_stamp,self.artist_pool_stamp
        if stamp==current:return
        self.artist_catalog_loading=stamp
        credits,pool=self.catalog_credits,self.artist_pool
        def load():
            new_credits,new_pool=credits,pool
            try:
                if stamp[0]!=current[0]:new_credits=self._read_catalog_credits()
                if stamp[1]!=current[1]:new_pool=self._read_artist_pool() if stamp[1] is not None else {}
            finally:
                if not self.closed:self.messages.put(('artist_catalog_ready',(self,stamp,new_credits,new_pool)))
        threading.Thread(target=load,daemon=True,name='artist-catalog').start()

    def accept_artist_catalog(self,value):
        owner,stamp,credits,pool=value
        if self.closed or owner is not self or stamp!=self.artist_catalog_loading:return False
        self.artist_catalog_loading=None
        if stamp!=self._artist_catalog_stamp():return False
        self.catalog_credits_stamp,self.artist_pool_stamp=stamp
        self.catalog_credits,self.artist_pool=credits,pool
        return True

    def observe(self,playback,enabled=True,public_enabled=True,cached_sync=False,spotify_sync_present=False):
        if self.closed:return
        changed_recording=(self.playback is None or playback.key!=self.key
                           or abs(playback.duration-self.playback.duration)>.5)
        if changed_recording:
            self._spotify_timed_result=False
            self.spotify_has_complete_sync=False
        self._spotify_sync_hint=bool(spotify_sync_present)
        self.spotify_sync_present=self._spotify_sync_hint or self._spotify_timed_result
        was_cached=self.cached_sync;self.cached_sync=bool(cached_sync)
        self.public_enabled=public_enabled
        if not public_enabled:self.status='공개 가사 보조 조회 꺼짐'
        was_enabled=self.enabled;self.enabled=enabled
        if not enabled:
            if self.spotify_retry_at:self._schedule_spotify_retry()
            if was_enabled:
                self.generation+=1;self.request_key='';self.public_done=False
                if self.job:self.job.send({'action':'lookup','key':'','title':'','artist':'','album':'','duration':0})
            self.spotify_pending=False;self.metadata_pending=False
            return
        if (self.playback is None or playback.key!=self.key or not was_enabled
                or abs(playback.duration-self.playback.duration)>.5 or was_cached!=self.cached_sync):
            pending=self.spotify_pending or self.metadata_pending
            self.spotify_pending=False;self.metadata_pending=False
            if self.playback is None or playback.key!=self.key or not was_enabled:
                self.metadata_attempted=None
            self.generation+=1;self.key=playback.key;self.request_key=str(self.generation)+':'+self.key
            self.public_done=False;self.public_retry=0;self.retry_at=0
            self.playback=playback
            if self.cached_sync:
                if self.job and pending:self.job.send({'action':'lookup','key':'','title':'','artist':'','album':'','duration':0})
            else:self._lookup()
        else:self.playback=playback
        if self.cached_sync:
            self._lookup()
            self.spotify_retry_at=0;self.status='저장한 싱크 사용 · 외부 재조회 생략'
            if self.job is None and self.spotify_state=='reconnecting':self.spotify_state='error'
            return
        if ((not was_enabled or was_cached) and self.spotify_requested and self.job is None
                and self.spotify_state=='error' and self.spotify_retry_count<2):
            self._schedule_spotify_retry()
        self._retry_spotify()
        if not public_enabled or not playback.has_timeline or not playback.artist:return
        if self.spotify_has_complete_sync:
            self.status='Spotify 가사·싱크 연결됨'
            return
        if self.awaiting_spotify:
            self.status='Spotify 응답 대기 · 이후 LRCLIB 보조 조회'
            return
        if not self.public_done and not self.public_running and time.monotonic()>=self.retry_at:
            self.public_running=True;request_key=self.request_key
            self.status='화면 없이 공개 가사·싱크 조회 중'
            def run():
                statuses=[]
                try:
                    options={'report':statuses.append} if self.reports_status else {}
                    if self.merge_metadata:options['merge_metadata']=True
                    result=self.fetch(playback,**options);error=False
                except Exception:
                    result=None;error=True
                self.messages.put(('background_lyrics',('public',{'kind':'public','key':request_key,
                    'result':result,'error':error,
                    'provider_status':statuses[-1] if statuses else ''})))
            threading.Thread(target=run,daemon=True,name='public-background-lyrics').start()

    def receive(self,sender,event):
        if self.closed or not isinstance(event,dict):return None
        if sender=='public':
            self.public_running=False
            if (not self.enabled or not self.public_enabled or self.cached_sync or self.spotify_has_complete_sync
                    or event.get('key')!=self.request_key):return None
            if event.get('error'):
                if self.awaiting_spotify:return None
                self.public_retry+=1;self.retry_at=time.monotonic()+15
                self.public_done=self.public_retry>=2
                self.status=('LRCLIB 조회 실패 · 싱크 존재 여부 미확인' if self.public_done
                             else 'LRCLIB 다시 확인 대기')
                return None
            self.public_done=True;self.public_retry=0;self.retry_at=0;result=event.get('result')
            reported=event.get('provider_status')
            self.status=reported if isinstance(reported,str) and 0<len(reported)<=160 else (
                ('LRCLIB 가사·싱크 연결됨' if any(t is not None for t in result['times']) else
                 'LRCLIB 원문 연결됨 · 받은 자료에 싱크 없음') if result else 'LRCLIB 검색 결과에 검증된 가사 없음')
            return (self.key,result) if result else None
        if self.job is None or sender is not self.job:return None
        kind=event.get('kind')
        if kind=='catalog_diagnostic':
            record=catalog_diagnostic_record(event.get('value'))
            if record:self.spotify_catalog_diagnostics=(self.spotify_catalog_diagnostics+[record])[-20:]
            return None
        # Account state belongs to this worker, even when cached playback has
        # moved on without asking it to look up another track.
        if kind in ('status','metadata_status') and isinstance(event.get('authenticated'),bool):
            self.spotify_authenticated=event['authenticated']
            if self.spotify_authenticated and self.spotify_state in ('connecting','reconnecting','login_required'):
                self.spotify_state='ready';self.spotify_status='Spotify 계정 연결됨'
            elif not self.spotify_authenticated and event.get('state')=='login_required':
                self.spotify_state='login_required';self.spotify_status='Spotify 연결 창에서 로그인 상태를 확인하세요.'
                self.spotify_retry_at=0
        if kind=='catalog_result':
            with self._catalog_lock:
                waiter=self._catalog_waiters.get(event.get('request_id'))
                if waiter and waiter['job'] is sender:
                    waiter['result']=event;waiter['event'].set()
            return None
        if kind=='metadata':
            if (self.enabled and self.playback and event.get('key')==self.request_key
                    and not isinstance(event.get('request_duration'),bool)
                    and event.get('request_duration')==self.playback.duration):
                self.artist_metadata.put(self.playback,event.get('track'))
                self.metadata_pending=False
        elif kind=='metadata_status':
            if self.enabled and event.get('key')==self.request_key:
                if event.get('state')!='searching':self.metadata_pending=False
        elif kind=='finished':
            self._cancel_catalog('Spotify 연결이 종료되어 곡 검색을 중단했습니다.')
            self.job=None;self.spotify_pending=False;self.metadata_pending=False;self.spotify_exit_code=event.get('exit_code')
            # A closed worker is not evidence that its saved account session
            # expired. Keep the last authentication report and specific error.
            if self.spotify_state=='login_required':
                self.spotify_retry_at=0
                if self.spotify_exit_code is not None:self.spotify_status+=f' (종료 코드 {self.spotify_exit_code})'
            else:self._schedule_spotify_retry()
        elif kind=='status':
            key=event.get('key')
            if key and key!=self.request_key:return None
            self.spotify_state=event.get('state','error');self.spotify_status=event.get('text','Spotify 연결 확인 중')
            if key and self.spotify_state=='not_found' and event.get('lyrics_missing') is True:
                self.spotify_missing_lyrics=key
            if (self.spotify_state in ('not_found','unsynced','error','login_required','closed')
                    and (key==self.request_key or not key and self.spotify_state in ('error','login_required','closed'))):
                self.spotify_pending=False
            if self.spotify_state=='error':self.spotify_last_error=self.spotify_status
            elif self.spotify_state=='login_required':
                self.spotify_retry_at=0
                if event.get('login_cancelled') is True:self.spotify_requested=False
            elif self.spotify_state=='closed' and not self.spotify_last_error:self.spotify_last_error=self.spotify_status
        elif kind=='lyrics' and self.enabled and not self.cached_sync and event.get('key')==self.request_key:
            self.spotify_pending=False
            rows=event.get('lines',[])
            if not rows or len(rows)>1500:return None
            lines=tuple(Line(row['text'],'','') for row in rows)
            times=[row.get('time') for row in rows]
            validate_times(times,len(lines))
            if any(t is not None and t>self.playback.duration+2 for t in times):return None
            self._spotify_timed_result=any(t is not None for t in times)
            self.spotify_has_complete_sync=all(t is not None for t in times)
            if self.spotify_has_complete_sync:
                self.public_retry=0;self.retry_at=0
                if self.public_enabled:self.status='Spotify 가사·싱크 연결됨'
            self.spotify_missing_lyrics=''
            self.spotify_sync_present=self._spotify_sync_hint or self._spotify_timed_result
            self.spotify_retry_count=0;self.spotify_retry_at=0
            return self.key,{'source':'Spotify','lines':lines,'times':times,'provider_id':event.get('track_id','')}
        return None

    def disconnect(self):
        self._cancel_catalog('Spotify 연결을 해제하여 곡 검색을 취소했습니다.','cancelled')
        self.spotify_requested=False;self.spotify_retry_at=0;self.spotify_pending=False;self.metadata_pending=False
        self.spotify_has_complete_sync=False
        if self.job:self.job.close();self.job=None
        self.spotify_authenticated=False;self.spotify_state='login_required';self.spotify_status='Spotify 연결 안 됨'

    def close(self):
        self.closed=True;self.disconnect()


class PlaybackLyricMerge:
    """Coalesce source changes and deliver only the latest recording merge."""
    def __init__(self, messages):
        self.messages = messages
        self.lock = threading.Lock()
        self.serial = 0
        self.generation = 0
        self.pending = None
        self.running = False
        self.sources = {}

    def reset(self):
        self.sources.clear()
        with self.lock:
            self.serial += 1
            self.generation += 1
            self.pending = None

    def remember(self, snapshot):
        return self._remember(self.sources, snapshot)

    @staticmethod
    def _remember(sources, snapshot):
        key = snapshot['source'].casefold(), snapshot.get('provider_id')
        previous = sources.get(key)
        if snapshot == previous:
            return False
        if previous:
            if (tuple(line.original for line in snapshot['lines'])==
                    tuple(line.original for line in previous['lines'])):
                lines=tuple(Line(line.original,line.reading or old.reading,line.korean or old.korean)
                            for old,line in zip(previous['lines'],snapshot['lines']))
                weaker=(previous['recording_verified'] and not snapshot['recording_verified']
                        or not previous['annotation_only'] and snapshot['annotation_only'])
                times=(previous['times'] if weaker else tuple(old if stamp is None else stamp
                       for old,stamp in zip(previous['times'],snapshot['times'])))
                try:validate_times(list(times),len(lines))
                except ValueError:times=previous['times']
                snapshot={**(previous if weaker else snapshot),'lines':lines,'times':times}
            def evidence(value):
                return (sum(stamp is not None for stamp in value['times']),
                        sum(bool(line.reading) + bool(line.korean) for line in value['lines']),
                        len(value['lines']))
            if snapshot == previous or evidence(snapshot) < evidence(previous):
                return False
        sources[key] = snapshot
        return True

    def has_source(self, source):
        return any(name == source.casefold() for name, _ in self.sources)

    def submit(self, playback, context, local_snapshots, reader_snapshots=()):
        # Reader documents become sources only after their current receipt applies.
        sources = self.sources.copy()
        for snapshot in reader_snapshots:
            self._remember(sources, snapshot)
        with self.lock:
            self.serial += 1
            self.pending = self.serial, playback, context, (*sources.values(), *local_snapshots)
            if self.running:
                return
            self.running = True
        threading.Thread(target=self._run, daemon=True, name='playback-lyric-merge').start()

    def _run(self):
        from lyric_merge import merge_lyrics
        while True:
            with self.lock:
                work = self.pending
                self.pending = None
                if work is None:
                    self.running = False
                    return
            serial, playback, context, snapshots = work
            try:
                result = merge_lyrics(playback, snapshots)
                old = context[1]
                if old and result['lines']:
                    owned={row['original_index']:index for index,row in enumerate(result['rows'])
                           if row['original_source'].casefold()=='manual'
                           and row['original_index']<len(old.lines)
                           and old.lines[row['original_index']].original==result['lines'][index].original}
                    result['previous_rows'] = (owned if len(owned)==len(old.lines)
                                               else previous_row_mapping(old.lines, result['lines']))
                error = None
            except (ValueError, TypeError, KeyError) as failure:
                result, error = None, failure
            with self.lock:
                if serial == self.serial:
                    self.messages.put(('merged_lyrics', (serial, playback, context, result, error)))


def previous_row_mapping(old, new):
    originals = tuple(line.original for line in old)
    if originals == tuple(line.original for line in new):
        return {index:index for index in range(len(old))}
    mapping, _ = native_line_mapping(originals, new, onsets_only=True)
    return mapping


def _mapped_provider_times(result,lines):
    """Only aligned phrase starts get a time; split translations get no invented onset."""
    source=result['lines'];times=result['times']
    validate_times(times,len(source))
    # Unsynced providers sometimes wrap the entire song in one zero-second
    # row. That does not establish the first phrase's onset.
    if (len(source)==1 and len(lines)>1 and times==[0]
            and normalized(source[0].original)==''.join(normalized(line.original) for line in lines)):
        return [None]*len(lines)
    if tuple(x.original for x in source)==tuple(x.original for x in lines):return list(times)
    if result.get('source') in ('ALSong','LyricsPlus'):
        match=match_lyrics([x.original for x in source],[x.original for x in lines])
        if match is not None:
            starts={start:index for index,start in enumerate(match.target_offsets)}
            mapped=[None]*len(lines);seen=set()
            for index,start in enumerate(match.source_offsets):
                target=starts.get(match.source_to_target[start])
                if target is not None and target not in seen:
                    mapped[target]=times[index];seen.add(target)
            validate_times(mapped,len(lines))
            return mapped
    mapping,_=native_line_mapping([x.original for x in source],lines,onsets_only=True)
    mapped=[None]*len(lines);seen=set()
    for index,target in sorted(mapping.items()):
        if target not in seen:mapped[target]=times[index]
        seen.add(target)
    validate_times(mapped,len(lines))
    return mapped


def _fill_timing_gaps(primary,supplement,bounds=()):
    """Keep every authoritative onset and reject crossing supplied onsets."""
    if not any(stamp is not None for stamp in primary):return list(primary)
    result=list(primary);following=[None]*len(primary);right=None
    for index in range(len(primary)-1,-1,-1):
        following[index]=right
        if primary[index] is not None:right=primary[index]
    left=-1
    for index,stamp in enumerate(primary):
        if stamp is not None:
            left=stamp;continue
        candidate=supplement[index]
        if (candidate is not None and left<candidate
                and (following[index] is None or candidate<following[index])
                and all(candidate>stamp if row<index else candidate<stamp
                        for row,stamp in bounds)):
            result[index]=candidate;left=candidate
    validate_times(result,len(result))
    return result


def mapped_times(result,lines):
    """Keep Spotify onsets and fill verified gaps without crossing their times."""
    primary=_mapped_provider_times(result,lines)
    fallback=result.get('fallback')
    if result.get('source')!='Spotify' or fallback is None:return primary
    if (not isinstance(fallback,dict) or fallback.get('source')=='Spotify'
            or 'fallback' in fallback):
        raise ValueError('보조 싱크 출처 형식이 올바르지 않습니다.')
    if not _same_clock_lyrics(result,fallback):return primary
    # A Spotify onset inside a differently split display row is still a time
    # boundary: that row must start before it, and following rows after it.
    mapping,_=native_line_mapping([line.original for line in result['lines']],lines)
    bounds=[(target,result['times'][index]) for index,target in mapping.items()
            if result['times'][index] is not None]
    return _fill_timing_gaps(primary,_mapped_provider_times(fallback,lines),bounds)


def _same_clock_lyrics(primary,fallback):
    """A common phrase alone cannot verify an auxiliary recording's clock."""
    return match_lyrics([line.original for line in primary['lines']],
                        [line.original for line in fallback['lines']]) is not None


def prefer_spotify_timing(current,incoming):
    """Select Spotify's own clock; None leaves other providers to the caller."""
    def timed(value):
        if not value:return False
        validate_times(value['times'],len(value['lines']))
        return any(stamp is not None for stamp in value['times'])
    spotify=[value for value in (current,incoming)
             if value and value.get('source')=='Spotify' and timed(value)]
    if not spotify:return None
    selected=spotify[-1]
    if (len(spotify)==2
            and tuple(line.original for line in spotify[0]['lines'])==
                tuple(line.original for line in spotify[1]['lines'])
            and sum(stamp is not None for stamp in spotify[0]['times'])>
                sum(stamp is not None for stamp in spotify[1]['times'])):
        selected=spotify[0]
    primary={key:value for key,value in selected.items() if key!='fallback'}
    primary['times']=list(primary['times'])
    return primary
