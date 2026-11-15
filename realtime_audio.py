"""Short acoustic matches for the first listen; never saves provisional timings."""
from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import time

from audio_capture_cache import REALTIME_PAUSE_NAME, load_capture_samples, write_capture_audio
from lyrics import normalized
from lyric_follow import is_vocalise
from storage import atomic_json,read_json
from japanese_alignment import JapaneseRuntimeError, ctc_emissions, prepare_ctc, release_ctc

WINDOW_SECONDS=3.0
STEP_SECONDS=1.0
LIVE_AUDIO_FILES = ('live-0.sloa', 'live-1.sloa')


class LiveWindowWriter:
    def __init__(self,directory,rate,channels,window_seconds=WINDOW_SECONDS):
        self.directory=Path(directory);self.rate=rate;self.channels=channels
        self.window_seconds=max(WINDOW_SECONDS,min(10.0,float(window_seconds)))
        self.parts=deque();self.frames=0;self.next_frame=int(rate*STEP_SECONDS);self.sequence=0
        self.pause_marker=self.directory/REALTIME_PAUSE_NAME
        self.next_control_frame=0
        self.paused=False

    def feed(self,data,frames,total,start_position,started):
        if total>=self.next_control_frame:
            self.next_control_frame=total+self.rate//4
            self.paused=self.pause_marker.exists()
            if self.paused:
                self.parts.clear()
                self.frames=0
        if self.paused:return
        self.parts.append(data);self.frames+=frames
        limit=int(self.rate*self.window_seconds)
        while self.frames>limit:
            first=self.parts.popleft();count=len(first)//(2*self.channels)
            remove=min(count,self.frames-limit)
            if remove<count:self.parts.appendleft(first[remove*2*self.channels:])
            self.frames-=remove
        if total<self.next_frame:return
        self.next_frame=total+int(self.rate*STEP_SECONDS)
        self.sequence+=1
        name=LIVE_AUDIO_FILES[self.sequence%2];destination=self.directory/name
        try:
            with write_capture_audio(destination.with_suffix('.tmp'),self.rate,self.channels) as output:
                output.write(b''.join(self.parts))
            os.replace(destination.with_suffix('.tmp'),destination)
            atomic_json(self.directory/'live-window.json',{'sequence':self.sequence,'file':name,
                'start_position':start_position+(total-self.frames)/self.rate,
                'end_position':start_position+total/self.rate,'observed_at':started+total/self.rate})
        except PermissionError:
            # A reader can briefly hold the alternate slot open on Windows.
            # Skipping this preview must not interrupt full-song recording.
            pass


class PartialFollower:
    def __init__(self,lines,start_index=0):
        self.lines=lines;self.index=None;self.pending=None;self.last_window=-1
        self.start_index=max(0,min(len(lines)-1,start_index))

    def select(self,groups,left,duration,window_end,acquiring=False):
        if window_end<=self.last_window:return None
        self.last_window=window_end;candidates=[]
        for relative,words in enumerate(groups):
            index=left+relative
            if self.index is not None and index<self.index:continue
            positive=[w for w in words if normalized(w['word']) and
                all(math.isfinite(float(w[k])) for k in ('start','end','probability')) and
                0<=w['start']<w['end']<=duration+.03 and w['end']-w['start']>=.04 and w['probability']>=.15]
            count=sum(len(normalized(w['word'])) for w in positive)
            if count<2:continue
            confidence=sum(len(normalized(w['word']))*w['probability'] for w in positive)/count
            end=positive[-1]['end'];extent=end-positive[0]['start']
            if count<3 and confidence<.65:continue
            minimum=.25 if self.index is not None and index==self.index+1 else .35
            if confidence<minimum or extent<.18 or end<duration-1.2:continue
            first=next((w for w in words if normalized(w['word'])),None)
            onset=window_end-duration+positive[0]['start'] if positive[0] is first and positive[0]['start']>=.08 and positive[0]['probability']>=.45 else None
            candidates.append((index,confidence,count,onset,window_end-duration+positive[0]['start'],
                               window_end-duration+end))
        if not candidates:self.pending=None;return None
        # The latest supported line in the short window is the current one.
        index,confidence,count,onset,evidence_start,evidence_end=candidates[-1]
        previous=self.index if self.index is not None else -1
        if self.index is not None and index>self.index and is_vocalise(self.lines[self.index]):
            if index>self.index+3 or confidence<.65 or count<4:self.pending=None;return None
            if self.pending!=index:self.pending=index;return None
            self.pending=None;self.index=index
            return {'index':index,'confidence':round(confidence,3),'characters':count,'onset':onset,'evidence_start':evidence_start,'evidence_end':evidence_end}
        if self.index is None and (index>2 or self.start_index>0):
            # Missing the opening phrase must not permanently trap acquisition.
            # Broader initial matches need strong, unique evidence twice.
            lower=max(0,self.start_index-4);upper=min(len(self.lines)-1,self.start_index+6 if self.start_index else 5)
            repeated=any(i!=index and normalized(self.lines[i])==normalized(self.lines[index]) for i in range(lower,upper+1))
            if not acquiring or not lower<=index<=upper or confidence<.55 or count<5 or repeated:return None
            if self.pending!=index:self.pending=index;return None
            self.pending=None;self.index=index
            return {'index':index,'confidence':round(confidence,3),'characters':count,'onset':onset,'evidence_start':evidence_start,'evidence_end':evidence_end}
        if confidence<.35:
            if self.pending!=index:self.pending=index;return None
        if index>previous+1:
            if index>previous+3 or confidence<.45:return None
            repeated=any(normalized(self.lines[i])==normalized(self.lines[index]) for i in range(max(0,previous),index))
            strong=index<=previous+2 and confidence>=.65 and count>=5 and not repeated
            if not strong and self.pending!=index:self.pending=index;return None
        self.pending=None;self.index=index
        return {'index':index,'confidence':round(confidence,3),'characters':count,'onset':onset,'evidence_start':evidence_start,'evidence_end':evidence_end}


class PartialAligner:
    def __init__(self,model,lines,start_index=0,window_seconds=WINDOW_SECONDS):
        from faster_whisper.tokenizer import Tokenizer
        self.model=model;self.follower=PartialFollower(lines,start_index);self.stalled=0;self.search_step=0
        self.window_seconds=window_seconds
        self.search_lefts=tuple(max(0,min(len(lines)-1,start_index+delta)) for delta in (-2,0,-4,2,4)) if start_index else (0,2,0,4)
        self.tokenizer=Tokenizer(model.hf_tokenizer,model.model.is_multilingual,task='transcribe',language='ja')
        self.tokens=[self.tokenizer.encode(line+'\n') for line in lines]

    def analyze(self,samples,start_position):
        if len(samples)<8000:return None
        duration=len(samples)/16000
        if duration>self.window_seconds+.05:raise ValueError('실시간 분석 구간이 너무 깁니다.')
        left=self.follower.index if self.follower.index is not None else self.search_lefts[self.search_step]
        acquiring=self.follower.index is None and (left>0 or self.follower.start_index>0)
        span=4 if self.follower.index is not None and self.stalled>=6 else 3
        if self.follower.index is None and not self.follower.start_index:span=min(span,6-left)
        tokens=[ids if index==0 else ids[:16] for index,ids in enumerate(self.tokens[left:left+span])]
        if sum(map(len,tokens))>400:
            tokens=tokens[:2]
        if not tokens or sum(map(len,tokens))>400:return None
        features=self.model.feature_extractor(samples,padding=0)
        encoded=self.model.encode(features)
        words=self.model.find_alignment(self.tokenizer,[sum(tokens,[])],encoded,[features.shape[-1]])[0]
        groups=[];cursor=0;boundary=0;token_position=0
        for ids in tokens:
            boundary+=len(ids);group=[]
            while cursor<len(words) and token_position<boundary:
                word=words[cursor];token_position+=len(word['tokens']);cursor+=1
                if token_position<=boundary:group.append(word)
            groups.append(group)
        previous=self.follower.index
        result=self.follower.select(groups,left,duration,start_position+duration,acquiring=acquiring)
        self.stalled=0 if self.follower.index!=previous else self.stalled+1
        if self.follower.index is None and self.stalled>=3 and self.follower.pending is None:
            self.search_step=(self.search_step+1)%len(self.search_lefts);self.stalled=0
        return result


class StreamingAligner:
    """Recognize a context window before matching it to the supplied lyrics."""
    def __init__(self,model,lines,start_index=0):
        from realtime_lyrics import RealtimeLyricMatcher
        self.model=model;self.follower=RealtimeLyricMatcher(lines,start_index)

    def analyze(self,samples,start_position):
        duration=len(samples)/16000
        if duration<3:return None
        if duration>10.05:raise ValueError('실시간 분석 구간이 너무 깁니다.')
        # Lyrics as a prompt can manufacture plausible words over instrumental
        # music. Match only independently decoded audio; retain singing pauses.
        segments,_=self.model.transcribe(samples,language='ja',beam_size=3,best_of=1,max_new_tokens=96,
            temperature=0,word_timestamps=True,condition_on_previous_text=False,
            vad_filter=False,compression_ratio_threshold=2.4,log_prob_threshold=-1.0,
            no_speech_threshold=.6)
        words=[]
        for segment in segments:
            # Singing can have high no_speech_prob alongside well-supported
            # words. Whisper already combines that score with low log probability;
            # treating it alone as silence discards real Japanese vocal phrases.
            if segment.compression_ratio>2.4 or segment.avg_logprob < -1.0:
                # Keep a boundary; words on opposite sides of rejected speech
                # must never be joined into a fabricated lyric phrase.
                words.append(None)
                continue
            words.extend({'word':word.word,'start':word.start,'end':word.end,
                          'probability':word.probability} for word in (segment.words or ()))
        return self.follower.select(words,start_position,duration)


class ContextAligner(StreamingAligner):
    """Follow known words in an eight-second context; decode freely to recover."""
    def __init__(self,model,lines,start_index=0):
        super().__init__(model,lines,start_index)
        self.acoustic=PartialAligner(model,lines,start_index,window_seconds=8)

    def acoustic_checkpoint(self):
        return {'follower':{name:copy.deepcopy(getattr(self.acoustic.follower,name))
                            for name in ('index','pending','last_window')},
                'stalled':self.acoustic.stalled,'search_step':self.acoustic.search_step}

    def restore_acoustic(self,state):
        for name,value in state['follower'].items():setattr(self.acoustic.follower,name,value)
        self.acoustic.stalled=state['stalled'];self.acoustic.search_step=state['search_step']

    def analyze(self,samples,start_position):
        # Short forced windows can confidently fit words over non-vocal sound.
        # Wait for the full context instead of treating startup as a three-second clip.
        if len(samples)<8*16000:return None
        previous=self.follower.index
        result=self.acoustic.analyze(samples,start_position)
        if result:
            self.follower.index=result['index'];self.follower.confirmed_end=result['evidence_end']
            if result['index']!=previous:self.follower.pending=None
            result={**result,'match_method':'supplied-lyrics'}
        advanced=result is not None and result['index']!=previous
        if not advanced and (self.follower.index is None or self.acoustic.stalled>=6):
            recovered=super().analyze(samples,start_position)
            if recovered:
                self.acoustic.follower.index=recovered['index'];self.acoustic.follower.pending=None
                self.acoustic.stalled=0
                return recovered
        return result


class RomajiFollower:
    """Track the nearby supplied lyric rows in successive CTC windows."""
    def __init__(self,request):
        from collections import Counter
        self.model, rows = prepare_ctc(request, 'cpu')
        self.phones = [row.ctc_phones for row in rows]
        self.repeats = Counter(phones for phones in self.phones if phones)
        self.follower = PartialFollower(request['lines'],request.get('start_index',0))
        self.stalled = 0
        self.search_step = 0

    def analyze(self,samples,start_position):
        from romaji_ctc import align_lines
        duration = len(samples)/16000
        if duration < 3:
            return None
        follower = self.follower
        window_end = start_position + duration
        if window_end <= follower.last_window or start_position > follower.last_window + 2:
            if follower.last_window >= 0:
                follower.index = follower.pending = None
                self.stalled = 0
        follower.last_window = window_end
        current = follower.index
        first = follower.start_index if current is None else current
        left = max(0, first - 3) if current is None else first
        right = min(len(self.phones), first + (9 if self.stalled >= 4 and current is None else 4))
        matches = align_lines(ctc_emissions(self.model,samples),self.phones[left:right])
        eligible = [(index, candidate) for index,candidate in enumerate(matches,left)
                    if candidate is not None and not candidate.ambiguous
                    and candidate.end_ms >= (duration - 1.2)*1000
                    and (self.repeats[self.phones[index]] == 1 or current is not None and index <= current + 1)]
        if not eligible:
            follower.pending = None
            self.stalled += 1
            return None
        index, candidate = max(eligible,key=lambda item:item[1].end_ms)
        if current is None or index > current + 1:
            if follower.pending != index:
                follower.pending = index
                return None
        follower.index, follower.pending = index, None
        self.stalled = 0 if index != current else self.stalled + 1
        begin = start_position + candidate.start_ms/1000
        end = start_position + candidate.end_ms/1000
        return {'index':index,'characters':len(normalized(follower.lines[index])),
                'onset':None,'evidence_start':begin,'evidence_end':end,'match_method':'romaji_ctc'}


def run(request,directory,emit):
    from analysis_limits import apply_analysis_limits
    accelerated=request.get('realtime_device','cpu') in ('auto','cuda')
    if request.get('romaji_realtime'):apply_analysis_limits(cpu_percent=5,memory_mb=3072)
    elif accelerated:apply_analysis_limits(cpu_percent=20,memory_mb=4096,gpu=True)
    else:apply_analysis_limits(cpu_percent=2)
    import numpy as np
    from audio_runtime import load_realtime_model
    emit('realtime_status',state='loading',text='첫 재생 음원 추적 준비 중')
    def legacy():
        # The experimental CPU worker can recover with the CPU Whisper model
        # without claiming GPU memory from the previous song's final analysis.
        options={**request,'realtime_device':'cpu'} if request.get('romaji_realtime') else request
        model,runtime=load_realtime_model(options,emit)
        gpu=runtime['device']=='cuda'
        aligner=(ContextAligner if gpu else PartialAligner)(model,request['lines'],request.get('start_index',0))
        return aligner,{**runtime,'method':'supplied-context-with-asr-recovery' if gpu else 'partial-alignment'},gpu
    romaji=False
    if request.get('romaji_realtime'):
        try:
            aligner=RomajiFollower(request)
            runtime={'device':'cpu','model':'romajiasr-v1.0.0','method':'romaji_ctc'}
            gpu,romaji=False,True
        except JapaneseRuntimeError:
            emit('realtime_status',state='retrying',text='RomajiASR 준비 실패 · 기존 실시간 추적으로 전환')
    if not romaji:aligner,runtime,gpu=legacy()
    emit('realtime_status',state='listening',runtime=runtime,text=('GPU 가사 직접 대조 · 인식 재탐색' if gpu else 'CPU 실시간 음원 대조')+
         (' · 전체 분석은 다음 처음 재생에서' if request.get('allow_partial') else ' · 종료 후 전체 분석'))
    directory=Path(directory);sequence=0;errors=0;deadline=time.monotonic()+request['duration']+190
    while time.monotonic()<deadline:
        try:window=read_json(directory/'live-window.json',{})
        except PermissionError:
            time.sleep(.05);continue
        current=window.get('sequence',0)
        if current<=sequence:time.sleep(.05);continue
        sequence=current
        if window.get('file') not in LIVE_AUDIO_FILES:continue
        try:payload=(directory/window['file']).read_bytes()
        except (FileNotFoundError,PermissionError):continue
        try:latest_sequence=read_json(directory/'live-window.json',{}).get('sequence')
        except PermissionError:continue
        if latest_sequence!=sequence:continue
        if time.monotonic()-window['observed_at']>2:continue
        checkpoint_names=['index','pending','last_window']
        if gpu and hasattr(aligner.follower,'confirmed_end'):checkpoint_names.append('confirmed_end')
        checkpoint={name:copy.deepcopy(getattr(aligner.follower,name)) for name in checkpoint_names}
        context_state=aligner.acoustic_checkpoint() if hasattr(aligner,'acoustic_checkpoint') else None
        search_checkpoint=(aligner.stalled,aligner.search_step) if not gpu else None
        switch_to_legacy=False
        try:
            samples=load_capture_samples(payload)
            start_position=window['start_position']
            if gpu and len(samples)<8*16000:
                emit('realtime_status',state='buffering',runtime=runtime,text='정확한 구절 대조를 위해 최근 8초 음원을 수집 중입니다.')
                continue
            if not gpu and not romaji and len(samples)>int(WINDOW_SECONDS*16000):
                trim=len(samples)-int(WINDOW_SECONDS*16000)
                samples=samples[trim:];start_position+=trim/16000
            result=None
            if len(samples) and float(np.sqrt(np.mean(samples*samples)))>=.001:
                result=aligner.analyze(samples,start_position)
        except JapaneseRuntimeError:
            if not romaji:
                raise
            switch_to_legacy=True
        except (ValueError,IndexError,RuntimeError) as error:
            for name,value in checkpoint.items():setattr(aligner.follower,name,value)
            if context_state is not None:aligner.restore_acoustic(context_state)
            if search_checkpoint:aligner.stalled,aligner.search_step=search_checkpoint
            errors+=1
            emit('realtime_status',state='failed' if errors>=3 else 'retrying',
                 text=f'음원 구간 대조 오류 ({type(error).__name__}) · '+
                 ('연속 3회 실패로 임시 추적 중단 · 종료 후 전체 분석은 계속' if errors>=3 else '다음 최신 구간에서 재시도'))
            if errors>=3:return
            continue
        if switch_to_legacy:
            emit('realtime_status',state='retrying',text='RomajiASR 추론 실패 · 기존 실시간 추적으로 전환')
            del aligner
            release_ctc()
            aligner,runtime,gpu=legacy()
            romaji=False
            continue
        errors=0
        # A slow machine drops old windows; it never builds an ever-later queue.
        if time.monotonic()-window['observed_at']>3:
            # Only publishable observations may advance the tracker's state.
            # Otherwise a dropped recovery would put the worker ahead of the UI.
            for name,value in checkpoint.items():setattr(aligner.follower,name,value)
            if context_state is not None:aligner.restore_acoustic(context_state)
            if search_checkpoint:aligner.stalled,aligner.search_step=search_checkpoint
            emit('realtime_status',state='late',text='음원 대조 지연 · 최신 구간으로 다시 확인 중')
            continue
        emit('realtime',track=request['key'],observed_at=window['observed_at'],
             position=window['end_position'],match=result,runtime=runtime)
