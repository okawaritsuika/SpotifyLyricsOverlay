"""Bounded automatic capture with serial ASR and preservation of manual timings."""
from pathlib import Path
import math
import threading
import time

from audio_job import AudioJob, ready
from audio_progress import AudioProgress
from audio_diagnostics import record_failure
from audio_alignment import ALIGNMENT_METHOD,INCREMENTAL_METHOD,normalize_ranges,uncovered_ranges
from audio_sync import merge_candidates
from storage import atomic_json,read_json,validate_times
from timing_quality import timing_origins,automatic_times
from live_timing import LiveTimingStore
from sync import estimate_times,active_line


class AutoMailbox:
    def __init__(self,messages): self.messages=messages
    def put(self,item): self.messages.put(('auto_audio',item[1]))


class AutoAnalyzer:
    def __init__(self,base,store,messages,job_factory=AudioJob,realtime_device='cpu'):
        self.base,self.store=Path(base),store
        self.live_timing=LiveTimingStore(store.directory)
        self.mailbox=AutoMailbox(messages)
        self.job_factory=job_factory
        self.realtime_device=realtime_device
        self.jobs={}
        self.progress={}
        self.last_progress=None
        self.recent_progress=[]
        self.live_signatures={}
        self.recording_durations={}
        self.seen=set()
        self.retryable=set();self.attempts={};self.last_playback=None
        self.transport_generation=0
        self.prefetch_generation=-1
        self.enabled=False
        self.finishing=False
        self.status='자동 분석 꺼짐'

    def set_enabled(self,enabled):
        if enabled:
            ready(self.base)
        self.enabled=enabled
        self.prefetch_generation=-1
        if not enabled:
            for job in tuple(self.jobs):
                if not job.cancelled.is_set():
                    job.cancelled.set()
                    threading.Thread(target=job.cancel,daemon=True,name='audio-disabled-cancel').start()
                self.progress[job].update({'kind':'progress'},cancelled=True)
                self.seen.discard(job.analysis_key)
                self.attempts.pop(job.analysis_key,None)
                self.retryable.discard(job.analysis_key)
        self.status='새 곡 자동 분석 대기' if enabled else '자동 분석 꺼짐'

    def timing(self,song,memory_only=False):
        if memory_only:return self.live_timing.load(song) or {'times':[None]*len(song.lines),'offset':0.,'source':''}
        return self.store.timing(song)

    def draft_path(self,song,memory_only=False):
        if memory_only:return self.store.directory/'live_audio_drafts'/self.live_timing.path(song).name
        return self.store.directory/'audio_drafts'/(song.id+'.json')

    def signature(self,song,memory_only=False):
        return self.live_timing.signature(song) if memory_only else song.fingerprint

    @staticmethod
    def retained_ranges(timing,duration):
        """Accept normal duration jitter without changing saved coordinates."""
        if not timing.get('analyzed_ranges'):return []
        saved_duration=timing.get('analysis_duration',0)
        if abs(saved_duration-duration)>2:return []
        ranges=normalize_ranges(timing['analyzed_ranges'],saved_duration)
        # Validate against the length that originally owned these ranges first.
        # A slightly shorter current timeline only clips its final boundary.
        return normalize_ranges([[start,min(end,duration)] for start,end in ranges if start<duration],duration)

    def job_status(self,job,text):
        if self.last_playback and job.analysis_key[0]==self.last_playback[0]:self.status=text

    @staticmethod
    def _progress_record(track_key,progress):
        return {'track_key':track_key,'title':progress.title,'mode':progress.mode,**progress.snapshot()}

    def activity_snapshot(self):
        """Keep other tracks' work visible without replacing the current status.

        Recent entries are bounded in-memory metadata, with no job, song,
        lyric or audio references. They are never persisted to the library.
        """
        return {'active':[{**self._progress_record(job.analysis_key[0],progress),
                           'awaiting_lyrics':bool(getattr(job,'waiting_for_lyrics',False))}
                          for job,progress in self.progress.items() if not getattr(job,'obsolete_waiting',False)],
                'recent':[{**record,'runtime':dict(record['runtime'])} for record in self.recent_progress]}

    def _use_external_sync(self,playback):
        # Cancelling a real audio worker can join another thread for seconds.
        # Reject queued results immediately, then stop only this track's workers
        # off the UI thread. Other recordings and saved timings are untouched.
        keys={key for key in self.seen|self.retryable|set(self.attempts) if key[0]==playback.key}
        self.seen.difference_update(keys);self.retryable.difference_update(keys)
        for key in keys:self.attempts.pop(key,None)
        for job in tuple(self.jobs):
            if job.analysis_key[0]!=playback.key or job.cancelled.is_set():continue
            job.cancelled.set()
            self.progress[job].update({'kind':'progress'},cancelled=True)
            threading.Thread(target=job.cancel,daemon=True,name='audio-external-sync-cancel').start()
        if self.enabled:self.status='확인된 싱크 사용 · 자동 분석 생략 · '+playback.title

    def _observe_duration(self,playback):
        # Missing length is not evidence of a different recording. Keep the
        # known context across those snapshots, and allow normal provider jitter.
        duration=playback.duration
        if isinstance(duration,bool) or not isinstance(duration,(int,float)):return False
        try:
            if not math.isfinite(duration) or duration<=0:return False
        except OverflowError:return False
        previous=self.recording_durations.get(playback.key)
        changed=previous is not None and abs(duration-previous)>2
        if previous is None or changed:self.recording_durations[playback.key]=duration
        for job in tuple(self.jobs):
            if job.analysis_key[0]!=playback.key or job.cancelled.is_set():continue
            if abs(job.recording_duration-duration)<=2:continue
            changed=True
            job.cancelled.set()
            self.progress[job].update({'kind':'progress'},cancelled=True)
            threading.Thread(target=job.cancel,daemon=True,name='audio-duration-cancel').start()
        if changed:
            keys={key for key in self.seen|self.retryable|set(self.attempts) if key[0]==playback.key}
            self.seen.difference_update(keys);self.retryable.difference_update(keys)
            for key in keys:self.attempts.pop(key,None)
        return changed

    def _pending_capture(self,playback):
        return next((job for job in self.jobs if job.analysis_key[0]==playback.key
                     and getattr(job,'waiting_for_lyrics',False) and not job.cancelled.is_set()
                     and self.jobs[job] not in ('error','discarded')),None)

    def _cancel_pending_capture(self,job):
        if job.cancelled.is_set():return
        job.cancelled.set()
        self.progress[job].update({'kind':'progress'},cancelled=True)
        threading.Thread(target=job.cancel,daemon=True,name='audio-prefetch-cancel').start()

    def _prefetch(self,playback,position):
        if (self._pending_capture(playback) or self.prefetch_generation==self.transport_generation
                or any(job.analysis_key[0]==playback.key and not job.cancelled.is_set() for job in self.jobs)):
            return
        if len(self.jobs)>=3:
            self.status='분석 대기가 가득 차 이번 곡은 건너뜁니다.'
            return
        self.prefetch_generation=self.transport_generation
        options={'defer_lyrics':True,'max_start_position':10,'incremental':True}
        if position>10:options['start_index']=0
        if self.realtime_device in ('auto','cuda'):options['realtime_device']=self.realtime_device
        try:job=self.job_factory(self.base,None,playback,self.mailbox,**options)
        except Exception as error:
            self.status=f'미리 수집 시작 실패 ({type(error).__name__}) · 가사를 찾으면 다시 시도합니다.'
            return
        job.analysis_key=(playback.key,None)
        job.recording_duration=playback.duration
        job.transport_generation=self.transport_generation
        job.capture_start_position=position
        job.waiting_for_lyrics=True
        self.jobs[job]='waiting'
        progress=self.progress[job]=AudioProgress(playback.title,final_analysis=not getattr(job,'skip_final',False))
        progress.text='가사 검색 중 · 재생음을 먼저 수집하고 있습니다.'
        self.status=progress.text+' · '+playback.title

    def observe(self,song,playback,memory_only=False,external_sync=False,awaiting_sync=False,tracking_times=None):
        if self.finishing:return
        if song and memory_only:self.live_signatures[song.id]=self.signature(song,True)
        position=playback.position()
        previous=self.last_playback
        now=time.monotonic()
        self.last_playback=(playback.key,position,playback.playing,now)
        changed=previous is None or previous[0]!=playback.key
        duration_changed=self._observe_duration(playback)
        resumed=bool(previous and not previous[2] and playback.playing)
        seeked=bool(previous and not changed and abs(position-previous[1]-(now-previous[3] if previous[2] else 0))>2)
        new_start=changed or duration_changed or resumed or seeked
        if new_start:self.transport_generation+=1
        for job in tuple(self.jobs):
            if (job.analysis_key[0]!=playback.key and self.jobs[job]=='waiting' and not job.cancelled.is_set()
                    and not getattr(job,'capture_started',False) and not getattr(job,'capture_complete',False)):
                # A skipped automatic request must not keep waiting three
                # minutes for that old song. Retain ownership until finished
                # so cleanup still counts against the real worker limit.
                job.obsolete_waiting=True
                self.seen.discard(job.analysis_key)
                self.retryable.discard(job.analysis_key)
                self.attempts.pop(job.analysis_key,None)
                self._cancel_pending_capture(job)
            elif (getattr(job,'waiting_for_lyrics',False) and
                    (job.analysis_key[0]!=playback.key or not playback.playing or seeked or duration_changed)):
                self._cancel_pending_capture(job)
        if changed and self.enabled:self.status='현재 곡 가사·분석 준비 확인 중 · '+playback.title
        if external_sync:
            self._use_external_sync(playback)
            return
        if (not self.enabled or not playback.has_timeline or not playback.playing
                or position>playback.duration-10 and self._pending_capture(playback) is None):
            return
        if not 10<=playback.duration<=900:
            self.status='음원 분석 범위 밖(10초~15분) · 현재는 추정 싱크 · '+playback.title
            return
        if not song or awaiting_sync and not self.timing(song,memory_only).get('analyzed_ranges'):
            self._prefetch(playback,position)
            return
        pending=self._pending_capture(playback)
        signature=self.signature(song,memory_only)
        key=(playback.key,signature)
        if any(job.analysis_key==key and not job.cancelled.is_set() for job in self.jobs):return
        if new_start and key in self.retryable and self.attempts.get(key,0)<2:
            self.seen.discard(key);self.retryable.discard(key)
        if key in self.seen and not changed: return
        timing=self.timing(song,memory_only)
        origins=timing_origins(timing)
        count=sum(stamp is not None for stamp in timing['times'])
        analyzed=not timing.get('completion_pending') and bool(timing.get('audio_finalized') or 'audio' in origins or
                      any(t is not None for t in timing.get('automatic_times',[])) or
                      '음원 분석' in timing.get('source','') and any(t is not None for t in timing['times']))
        draft={};candidate_only=False
        if not timing.get('audio_finalized'):
            draft=read_json(self.draft_path(song,memory_only),{})
            if draft.get('fingerprint')==signature:
                validate_times(draft.get('times'),len(song.lines))
                candidate_only=not analyzed and not timing.get('completion_pending')
                analyzed=analyzed or not timing.get('completion_pending')
            else:draft={}
        partial=bool(draft.get('partial') and count<len(song.lines) and not timing.get('completion_pending'))
        ranges=self.retained_ranges(timing,playback.duration)
        # Older partial captures saved no explicit interval. Retain only the
        # span bracketed by their confirmed audio onsets, never guess its tail.
        if not ranges and partial and draft.get('captured_seconds',0)>=10:
            prior=automatic_times(timing)
            anchors=[(i,t+timing.get('offset',0)) for i,t in enumerate(prior) if t is not None]
            if len(anchors)>=2:
                start=0 if anchors[0][0]==0 else anchors[0][1]
                ranges=normalize_ranges([[start,anchors[-1][1]]],playback.duration)
        remaining=uncovered_ranges(0,playback.duration,ranges) if ranges else []
        continuing=bool(ranges and any(end-start>=10 for start,end in remaining))
        if ranges:
            analyzed=not continuing
            if continuing:
                next_range=next(((start,end) for start,end in remaining if end-max(start,position)>=10),None)
                if next_range is None:
                    if pending:self._cancel_pending_capture(pending)
                    self.status=f'확인 {count}/{len(song.lines)}구절 · 저장 구간 유지 · 다음 재생에서 미분석 구간 이어 분석'
                    return
                if pending is None and position<next_range[0]-2:
                    stamp=int(next_range[0])
                    self.status=f'확인 {count}/{len(song.lines)}구절 · 저장 구간 사용 · {stamp//60:02}:{stamp%60:02}부터 이어 분석'
                    return
        elif partial:
            # An old partial capture already consumed the initial attempt. Keep
            # its usable anchors and allow one supplement at the next beginning.
            self.attempts.setdefault(key,1)
            exhausted=self.attempts[key]>=2
            capture_start=getattr(pending,'capture_start_position',position) if pending else position
            if capture_start>3 or exhausted:
                if pending:self._cancel_pending_capture(pending)
                self.seen.add(key)
                if not exhausted:self.retryable.add(key)
                self.status=f'확인 {count}/{len(song.lines)}구절 · 부분 분석 · '+(
                    '자동 보완 1회 종료 · 직접 보정 가능' if exhausted else '처음부터 재생 시 한 번 보완')+' · '+song.title
                return
            analyzed=False
        if analyzed or all(origin=='manual' for origin in origins):
            if pending:self._cancel_pending_capture(pending)
            self.seen.add(key)
            if candidate_only:
                detail='저장된 분석 후보 · '+('기존 시간표 유지' if memory_only else '분석창에서 확인')
            else:
                detail='저장한 싱크 사용' if count==len(song.lines) else '부분 싱크 · 빈 구절은 추정 또는 직접 보정'
            self.status=f'확인 {count}/{len(song.lines)}구절 · '+detail+' · '+song.title
            return
        if key in self.seen:return
        if len(self.jobs)>=3 and pending is None:
            self.status='분석 대기가 가득 차 이번 곡은 건너뜁니다.'
            return
        self.seen.add(key);self.attempts[key]=self.attempts.get(key,0)+1
        confirmed=[round(t+timing.get('offset',0),3) if t is not None and 0<=t+timing.get('offset',0)<playback.duration else None
                   for t in timing['times']]
        options={'incremental':True,'confirmed_times':confirmed,'analyzed_ranges':ranges,
                 'memory_only':memory_only,'max_start_position':10}
        if continuing:options['capture_from']=max(0,next_range[0]-2)
        if self.realtime_device in ('auto','cuda'):options['realtime_device']=self.realtime_device
        if (memory_only or pending) and position>10:
            try:
                validate_times(tracking_times,len(song.lines))
                if any(stamp is None or stamp>=playback.duration for stamp in tracking_times):raise ValueError
            except ValueError:
                times=estimate_times(song.lines,playback.duration,timing['times'],timing.get('estimate_intro',12),timing.get('estimate_outro',8))
                offset=timing.get('offset',0)
            else:
                # Another singer's clock is only a search hint. These times
                # already include its offset and never enter timing storage.
                times=tracking_times;offset=0
            options['start_index']=active_line(times,position,offset) or 0
        try:
            if pending:
                # A late original still uses the beginning already captured.
                # Seed only the live follower; keep the full-analysis decision
                # from when this audio capture actually began.
                if not pending.bind_lyrics(song,memory_only,options.get('start_index'),analysis_context=options):
                    self.seen.discard(key);self.attempts.pop(key,None)
                    return
                job=pending
            else:job=self.job_factory(self.base,song,playback,self.mailbox,**options)
        except Exception as error:
            self.retryable.add(key)
            self.status=f'자동 분석 시작 실패 ({type(error).__name__}) · 다음 곡에서 다시 시도합니다.'
            return
        if pending:
            self.progress[job].title=song.title
            self.progress[job].text='찾은 가사를 수집 중인 음원에 연결했습니다. 먼저 수집한 앞부분도 전체 분석에 포함합니다.'
        else:
            self.jobs[job]='waiting'
            self.progress[job]=AudioProgress(song.title,final_analysis=not getattr(job,'skip_final',False))
        job.analysis_key=key
        job.recording_duration=playback.duration
        job.transport_generation=self.transport_generation
        self.status=('먼저 수집한 음원에 가사 연결 · ' if pending else '자동 수집 시작 · ')+song.title

    def begin_shutdown(self):
        """Stop new work; finish bound automatic clips before releasing storage."""
        self.finishing=True
        for job in tuple(self.jobs):
            if job.cancelled.is_set():continue
            if (getattr(job,'incremental',False) and getattr(job,'song',None)
                    and not getattr(job,'waiting_for_lyrics',False)):
                try:job.finish_capture()
                except OSError:self._cancel_pending_capture(job)
            else:self._cancel_pending_capture(job)
        return bool(self.jobs)

    def receive(self,job,event,songs):
        if job not in self.jobs: return None
        kind=event.get('kind')
        progress=self.progress[job]
        if kind=='error' and not job.cancelled.is_set():
            record_failure(self.store.directory,progress.title,progress,event)
        # A worker result is not a committed candidate or timeline yet.
        if kind!='result' or job.cancelled.is_set():
            progress.update(event,cancelled=job.cancelled.is_set())
        if kind=='finished':
            completed=self.progress.pop(job)
            if not getattr(job,'obsolete_waiting',False):
                self.last_progress=completed
                self.recent_progress.insert(0,self._progress_record(job.analysis_key[0],self.last_progress))
                del self.recent_progress[3:]
            state=self.jobs.pop(job,None)
            if state=='partial' and not job.cancelled.is_set():
                self.retryable.add(job.analysis_key);self.attempts.pop(job.analysis_key,None)
            if (job.analysis_key in self.retryable and not job.cancelled.is_set() and self.last_playback
                    and job.analysis_key[0]==self.last_playback[0]
                    and job.transport_generation<self.transport_generation and self.attempts.get(job.analysis_key,0)<2):
                self.seen.discard(job.analysis_key)
            return None
        if job.cancelled.is_set() or not self.enabled: return None
        if kind=='progress':
            self.jobs[job]=event.get('state','analyzing')
            if self.jobs[job]=='unmatched':self.retryable.add(job.analysis_key)
            if self.jobs[job]=='discarded' and getattr(job,'incremental',False):
                self.retryable.add(job.analysis_key);self.attempts.pop(job.analysis_key,None)
            if getattr(job,'waiting_for_lyrics',False) and self.jobs[job] in ('waiting','recording'):
                progress.text='가사 검색 중 · 재생음 먼저 수집 · '+event['text']
            self.job_status(job,progress.title+' · '+progress.text)
        elif kind=='error':
            self.jobs[job]='error'
            self.retryable.add(job.analysis_key)
            self.job_status(job,progress.title+' · '+event['text']+' · 재생 재개·이동 시 최대 1회 재시도')
        elif kind=='result':
            if not job.song or getattr(job,'waiting_for_lyrics',False):
                progress.update({'kind':'progress','state':'discarded','text':'가사가 연결되지 않아 분석 결과를 제외했습니다.'})
                return None
            memory_only=getattr(job,'memory_only',False)
            song=next((s for s in songs if s.id==job.song.id),None)
            if song is None and memory_only:song=job.song
            if not song:
                progress.update({'kind':'progress','state':'discarded','text':'대상 가사가 없어 결과를 제외했습니다.'})
                return None
            signature=self.signature(job.song,memory_only)
            if (self.signature(song,memory_only)!=signature or
                    memory_only and self.live_signatures.get(song.id)!=signature):
                progress.update({'kind':'progress','state':'discarded','text':'분석 시작 후 가사 문서의 원문·줄 구성이 달라져 이전 행 기준의 결과를 적용하지 않았습니다.'})
                return None
            def persist(operation,candidate_saved=False):
                try:operation()
                except (OSError,ValueError):
                    text=('분석 후보 보관 · 시간표 적용 실패' if candidate_saved else '분석 후보 저장 실패')
                    text+=' · 기존 시간표는 보존했습니다.'
                    if candidate_saved and not memory_only:text+=' 분석창에서 후보를 확인해 주세요.'
                    progress.update({'kind':'error','text':text})
                    self.jobs[job]='error'
                    self.job_status(job,song.title+' · '+text)
                    raise
            result={**event['result'],'fingerprint':signature,
                    'model':event['result'].get('model','faster-whisper-small')}
            current=self.timing(song,memory_only)
            incremental=result.get('incremental') is True
            new_ranges=normalize_ranges(result.get('analyzed_ranges',[]),job.recording_duration) if incremental else []
            old_ranges=self.retained_ranges(current,job.recording_duration)
            combined=normalize_ranges(old_ranges+new_ranges,job.recording_duration) if incremental else []
            updated,added=merge_candidates(current,result['times'],replace_audio=not incremental and result.get('method') in ('bounded-gap-alignment-v1',ALIGNMENT_METHOD))
            if memory_only:
                result={key:result[key] for key in ('times','coverage','confidence','matched','duration','method',
                    'recovered_indices','captured_seconds','partial','fingerprint','model','incremental','analyzed_ranges') if key in result}
            persist(lambda:atomic_json(self.draft_path(song,memory_only),result))
            matched=sum(t is not None for t in result['times'])
            if result.get('partial'):
                self.retryable.add(job.analysis_key)
                self.jobs[job]='incomplete'
            acceptable=(bool(matched and new_ranges) if incremental and result.get('method')==INCREMENTAL_METHOD
                        else matched>=min(3,len(song.lines)) and matched/len(song.lines)>=.6)
            if not acceptable:
                progress.update(event)
                progress.text='일치 구절이 적어 후보만 보관했습니다. 자동 시간표는 적용하지 않았습니다.'
                count=sum(stamp is not None for stamp in current['times'])
                detail=''
                if result.get('partial'):
                    detail=' · 부분 분석 · '+('자동 보완 1회 종료 · 직접 보정 가능'
                        if self.attempts.get(job.analysis_key,0)>=2 else '처음부터 재생 시 한 번 보완')
                self.job_status(job,f'{song.title} · 확인 {count}/{len(song.lines)}구절 · 일치 구절이 적어 후보만 보관했습니다.'+
                    (' 자동 시간표는 적용하지 않았습니다.' if memory_only else ' 음원 분석 창에서 확인하세요.')+detail)
                return None
            if added or incremental and combined!=old_ranges:
                backup=self.store.directory/('live_audio_backups' if memory_only else 'audio_backups')/(
                    self.live_timing.path(song).name if memory_only else song.id+'.before-gap-v1.json')
                if not backup.exists():persist(lambda:atomic_json(backup,{
                    **(self.live_timing.clean(current,song) if memory_only else current),'fingerprint':signature}),True)
                updated['audio_method']=result.get('method','')
                if incremental:
                    updated['analyzed_ranges']=combined
                    updated['analysis_duration']=job.recording_duration
                    updated['audio_finalized']=not any(end-start>=10 for start,end in
                        uncovered_ranges(0,job.recording_duration,combined)) or all(t is not None for t in updated['times'])
                else:updated['audio_finalized']=not result.get('partial',False)
                if updated['audio_finalized']:updated.pop('completion_pending',None)
                if memory_only:persist(lambda:self.live_timing.save(song,updated),True)
                else:persist(lambda:self.store.save_timing(song,updated),True)
            count=sum(stamp is not None for stamp in updated['times'])
            detail=''
            if incremental and not updated.get('audio_finalized'):
                self.retryable.add(job.analysis_key)
                if combined!=old_ranges:self.attempts[job.analysis_key]=0
                detail=' · 들은 구간 저장 · 다음 재생에서 미분석 구간만 이어 분석'
            elif result.get('partial') and count<len(song.lines) and not incremental:
                detail=' · 부분 분석 · '+('자동 보완 1회 종료 · 직접 보정 가능'
                    if self.attempts.get(job.analysis_key,0)>=2 else '처음부터 재생 시 한 번 보완')
            elif count<len(song.lines):detail=' · 부분 싱크 · 빈 구절은 추정 또는 직접 보정'
            progress.update(event)
            progress.text=f'확인 {count}/{len(song.lines)}구절 · 자동 분석 {added}구절 저장 · 수동 기록 보존'+detail
            self.job_status(job,song.title+' · '+progress.text)
            return song
        return None

    def close(self):
        self.enabled=False
        for job in tuple(self.jobs): job.cancel()
        for job in tuple(self.jobs): job.close()
        self.jobs.clear()
        self.progress.clear()
