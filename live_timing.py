"""Persist user timestamps, never the transient lyric text they refer to."""
import hashlib
import json
from lyrics import normalized
from storage import atomic_json,read_json,validate_times,validate_offset


def original_signature(song):
    return hashlib.sha256(json.dumps([normalized(line.original) for line in song.lines],ensure_ascii=False).encode()).hexdigest()

class LiveTimingStore:
    def __init__(self,directory):self.directory=directory/'live_timings'
    def path(self,song):return self.directory/(hashlib.sha256(song.id.encode()).hexdigest()+'.json')
    def signature(self,song):
        return original_signature(song)
    def load(self,song):
        value=read_json(self.path(song),{})
        if not isinstance(value,dict):raise ValueError('저장된 실시간 가사 시간표 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        if value.get('signature')!=self.signature(song):return None
        return self.clean(value,song)
    def clean(self,value,song):
        if not isinstance(value,dict):raise ValueError('저장할 가사 시간표 형식을 확인하세요.')
        validate_times(value.get('times'),len(song.lines))
        result={'times':list(value['times']),'source':'직접 저장한 싱크'}
        from timing_quality import timing_origins,automatic_times,cleared_rows
        result['origins']=timing_origins(value)
        if value.get('automatic_times') is not None:result['automatic_times']=automatic_times(value)
        cleared=cleared_rows(value)
        if cleared:result['cleared_rows']=sorted(cleared)
        removed=value.get('removed_alsong_rows')
        added=[]
        for entry in value.get('added_alsong_rows',[]) if isinstance(value.get('added_alsong_rows'),list) else []:
            if not isinstance(entry,dict):
                break
            before,text=entry.get('before'),entry.get('text')
            if (type(before) is not int or not 0<=before<len(song.lines)+len(added)
                    or not isinstance(text,str) or not text.strip() or any(c in text for c in '\r\n\0')):
                break
            stamp=entry.get('time')
            validate_times([stamp],1)
            added.append({'before':before,'text':text,'time':stamp,'cleared':entry.get('cleared') is True})
        if added:result['added_alsong_rows']=added
        if isinstance(removed,list):
            count=len(song.lines)+len(added)
            rows={i for i in removed if type(i) is int and 0<=i<count}
            if 0<len(rows)<count:result['removed_alsong_rows']=sorted(rows)
        if value.get('manual_priority') is True:result['manual_priority']=True
        if value.get('audio_finalized') is True:result['audio_finalized']=True
        if value.get('completion_pending') is True:result['completion_pending']=True
        if 'analyzed_ranges' in value:
            from audio_alignment import normalize_ranges
            duration=value.get('analysis_duration')
            result['analyzed_ranges']=normalize_ranges(value['analyzed_ranges'],duration)
            result['analysis_duration']=duration
        if value.get('audio_method') in ('supplied-lyrics-alignment','bounded-gap-alignment-v1','bounded-gap-alignment-v2','incremental-lyrics-alignment-v1'):
            result['audio_method']=value['audio_method']
        if 'audio' in result['origins']:result['source']='구절별 수동 / 음원 분석' if 'manual' in result['origins'] else '음원 분석'
        for key,default in [('offset',0.),('estimate_intro',12.),('estimate_outro',8.)]:
            number=value.get(key,default)
            validate_offset(number)
            if key!='offset' and number<0:raise ValueError('싱크 보정값을 확인하세요.')
            result[key]=number
        return result
    def save(self,song,value):
        atomic_json(self.path(song),{**self.clean(value,song),'signature':self.signature(song)})
