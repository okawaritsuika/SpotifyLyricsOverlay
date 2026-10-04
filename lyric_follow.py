"""Prefer adjacent lyric rows, including slightly different wording."""
from difflib import SequenceMatcher
from collections import deque
from statistics import median
import math
import re
from lyrics import normalized

def is_vocalise(text):
    value=normalized(text).replace('ー','')
    return bool(value and re.fullmatch(r'(?:(?:la|ra|na|lu|ru|oh|ah)|[らラなナるルあアおオ])+',value))


def limit_vocalise(candidate,repeats,known,position,confirmed=None,offset=0):
    """A written 'la' has no inferred end; wait for a later measured phrase."""
    if candidate is None:return None
    floor=-1 if confirmed is None else confirmed
    for index,stamp in enumerate(known):
        if stamp is not None and stamp<=position-offset:floor=max(floor,index)
    return next((index for index in repeats if floor<=index<candidate),candidate)

def native_line_mapping(originals,lines,*,onsets_only=False):
    """Align exact spans; timestamp reuse also requires identical row starts."""
    source=[normalized(text) for text in originals]
    target=[normalized(line.original) for line in lines]
    if not source or not target:return {},set()
    def spans(keys):
        result=[]
        for start in range(len(keys)):
            joined='';choices={}
            for end in range(start,min(start+6,len(keys))):
                if not keys[end]:break
                joined+=keys[end];choices[joined]=end-start+1
            result.append(choices)
        return result
    source_spans,target_spans=spans(source),spans(target)
    scores=[[0]*(len(target)+1) for _ in range(len(source)+1)]
    for left in range(len(source)-1,-1,-1):
        for right in range(len(target)-1,-1,-1):
            best=max(scores[left+1][right],scores[left][right+1])
            for text,count in source_spans[left].items():
                other=target_spans[right].get(text)
                if other:best=max(best,len(text)+scores[left+count][right+other])
            scores[left][right]=best
    mapping={};grouped=set();left=right=0
    while left<len(source) and right<len(target):
        match=None
        for text,count in source_spans[left].items():
            other=target_spans[right].get(text)
            if other and len(text)+scores[left+count][right+other]==scores[left][right]:
                match=count,other;break
        if match:
            count,other=match;offset=0;target_row=right;target_end=len(target[right])
            for source_row in range(left,left+count):
                while offset>=target_end and target_row<right+other-1:
                    target_row+=1;target_end+=len(target[target_row])
                if not onsets_only or offset==target_end-len(target[target_row]):
                    mapping[source_row]=target_row
                    if count>1 or other>1:grouped.add((source_row,target_row))
                offset+=len(source[source_row])
            left+=count;right+=other
        elif scores[left+1][right]>=scores[left][right+1]:left+=1
        else:right+=1
    return mapping,grouped


def nearby_line(text,lines,expected,lower=0,upper=None):
    if not lines:return None
    upper=len(lines)-1 if upper is None else min(upper,len(lines)-1)
    expected=max(lower,min(upper,expected));source=normalized(text)
    best=expected;score=-1.
    for index in range(max(lower,expected-3),min(upper,expected+3)+1):
        target=normalized(lines[index].original)
        similarity=SequenceMatcher(None,source,target,autojunk=False).ratio() if source and target else 0
        value=similarity-.12*abs(index-expected)
        if value>score:best=index;score=value
    return best if score>=.35 else None


class AudioPace:
    """Predict one row from recent acoustic phrase positions; never persist it."""
    def __init__(self,lines):
        self.units=[max(3,len(normalized(line.reading or line.original))) for line in lines]
        self.repeats={i for i,line in enumerate(lines) if is_vocalise(line.original)}
        self.index=None;self.onset=None;self.heard=-1.;self.rates=deque(maxlen=3)
        self.predicted=False;self.blocked=False

    def observe(self,index,onset,heard,confidence):
        if not 0<=index<len(self.units) or not math.isfinite(heard):return
        reliable=confidence>=.35 and onset is not None and math.isfinite(onset) and 0<=onset<=heard
        if self.index!=index:
            if reliable and self.index is not None and self.index not in self.repeats and self.onset is not None and index==self.index+1:
                duration=onset-self.onset;rate=duration/self.units[self.index]
                if .4<=duration<=12 and .035<=rate<=1.5 and (not self.rates or .4<=rate/median(self.rates)<=2.5):
                    self.rates.append(rate)
                else:self.rates.clear()
            else:self.rates.clear()
            self.index=index;self.onset=onset if reliable else None
            self.predicted=False;self.blocked=False
        elif self.onset is None and reliable:self.onset=onset
        if confidence>=.35:self.heard=max(self.heard,heard)
        target=self.next_start()
        if self.predicted and confidence>=.55 and target is not None and heard>=target+1:
            # Fresh sound still supports the old row: retract once, not repeatedly.
            self.predicted=False;self.blocked=True

    def next_start(self):
        if self.onset is None or self.index is None or self.index in self.repeats or len(self.rates)<2:return None
        duration=self.units[self.index]*median(self.rates)
        return self.onset+max(.6,duration) if duration<=12 else None

    def current(self,position):
        target=self.next_start()
        if self.index is None:return None
        if self.predicted and (position-self.heard>3 or target is None or position>target+3):
            self.predicted=False;self.blocked=True
        if (not self.blocked and target is not None and self.index+1<len(self.units) and
                target<=position<=target+3 and 0<=position-self.heard<=3):self.predicted=True
        return self.index+int(self.predicted)
