"""Learn observed Spotify line transitions; persist numeric onsets only."""
import hashlib
import math
import weakref
from storage import atomic_json,read_json,validate_times
from live_timing import original_signature


def _valid_duration(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):return False
    try:return math.isfinite(value) and value>=0
    except OverflowError:return False


class NativeTiming:
    def __init__(self,directory):
        self.directory=directory/'native_timings';self.key=None;self.times=[];self.previous=None
        self.song_ref=None

    def select(self,song,duration):
        if not _valid_duration(duration):raise ValueError('Spotify 진행 기록의 재생 길이가 올바르지 않습니다.')
        rounded_duration=round(duration,1)
        if self.song_ref and self.song_ref() is song and self.key and self.key[2]==rounded_duration:return self.times
        signature=original_signature(song)
        key=(song.id,signature,rounded_duration)
        if self.key==key:
            self.song_ref=weakref.ref(song)
            return self.times
        # A failed read must neither retain another recording's observation nor
        # cache an empty success that prevents a repaired file from reloading.
        self.key=None;self.song_ref=None;self.previous=None;self.times=[None]*len(song.lines)
        path=self.directory/(hashlib.sha256((song.id+signature).encode()).hexdigest()+'.json')
        value=read_json(path,{})
        if not isinstance(value,dict):raise ValueError('저장된 Spotify 진행 기록 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        if not value:
            legacy=self.directory/(hashlib.sha256((song.id+song.fingerprint).encode()).hexdigest()+'.json')
            value=read_json(legacy,{})
            if not isinstance(value,dict):raise ValueError('저장된 Spotify 진행 기록 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        if value:
            if not _valid_duration(value.get('duration')):raise ValueError('저장된 Spotify 진행 기록의 재생 길이가 올바르지 않습니다. 원본은 보존했습니다.')
            if abs(value['duration']-duration)<1:
                validate_times(value.get('times'),len(song.lines));self.times=list(value['times'])
        self.path=path;self.key=key;self.song_ref=weakref.ref(song)
        return self.times

    def reset_observation(self):self.previous=None

    def observe(self,song,duration,source,target,position,now,eligible):
        self.select(song,duration)
        previous=self.previous
        self.previous=(source,target,position,now) if eligible else None
        if not eligible or previous is None:return False
        old_source,old_target,old_position,old_now=previous
        elapsed=now-old_now
        # First observation, seeks, missed rows and frozen backgrounds cannot
        # tell us when a line actually began. Only a witnessed transition can.
        if not (source==old_source+1 and target==old_target+1 and 0<elapsed<=1.5
                and abs(position-old_position-elapsed)<=.6 and 0<=position<duration):return False
        if self.times[target] is not None:return False
        before=next((x for x in reversed(self.times[:target]) if x is not None),-1)
        after=next((x for x in self.times[target+1:] if x is not None),duration)
        stamp=round(position,3)
        if not before<stamp<after:return False
        updated=list(self.times);updated[target]=stamp
        atomic_json(self.path,{'times':updated,'duration':duration,'method':'observed-spotify-transition-v1'})
        self.times=updated
        return True
