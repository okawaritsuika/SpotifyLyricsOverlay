"""Conservative online matching of recognized Japanese words to known lyrics.

This module does not recognize audio, invent timestamps, or persist timing data.
Only words actually returned by ASR can move the confirmed lyric position.
"""
from functools import lru_cache
import math

from lyrics import normalized
from lyric_follow import is_vocalise


def _kana(text):
    return ''.join(chr(ord(char)-0x60) if '\u30a1'<=char<='\u30f6' else char
                   for char in normalized(text))


@lru_cache(maxsize=4096)
def _reading(text):
    try:
        from web_lyrics import reading
        return _kana(reading(text))
    except (ImportError,RuntimeError):
        # The optional local dictionary may be absent in a compact installation.
        return _kana(text)


def _suffix_alignment(source,target):
    """Local character alignment ending at the recent end of the ASR text.

    Earlier lines in the same audio window must not outscore a new short line.
    Retaining the character pairs also distinguishes a clipped line from a true
    observed onset; a substring match never supplies an invented line start.
    """
    if not source or not target:return None
    rows=[[0.]*(len(target)+1) for _ in range(len(source)+1)]
    moves=[[0]*(len(target)+1) for _ in range(len(source)+1)]
    best=(0.,0,0)
    for i,char in enumerate(source,1):
        for j,other in enumerate(target,1):
            choices=(0.,rows[i-1][j-1]+(2 if char==other else -2),
                     rows[i-1][j]-2.5,rows[i][j-1]-2.5)
            move=max(range(4),key=choices.__getitem__)
            rows[i][j]=choices[move];moves[i][j]=move
            if i>=max(1,len(source)-1) and choices[move]>best[0]:
                best=(choices[move],i,j)
    score,i,j=best;end_i=i;end_j=j;pairs=[]
    while i and j and rows[i][j]>0:
        move=moves[i][j]
        if move==1:
            if source[i-1]==target[j-1]:pairs.append((i-1,j-1))
            i-=1;j-=1
        elif move==2:i-=1
        elif move==3:j-=1
        else:break
    if not pairs:return None
    pairs.reverse()
    return {'pairs':pairs,'similarity':len(pairs)/max(end_i-i,end_j-j),
            'score':score,'source_start':i,'target_start':j}


class RealtimeLyricMatcher:
    def __init__(self,lines,start_index=0,reader=None):
        self.lines=list(lines);self.index=None;self.pending=None;self.last_window=-1.
        self.confirmed_end=-1.
        self.start_index=max(0,min(len(self.lines)-1,start_index))
        self.reader=reader or _reading
        self.targets=[(_kana(line),_kana(self.reader(line))) for line in self.lines]

    def _observations(self,words,duration):
        runs=[];observations=[];last_start=-1.
        for word in words:
            try:
                start=float(word['start']);end=float(word['end']);probability=float(word['probability'])
                text=word['word']
                valid=(isinstance(text,str) and all(map(math.isfinite,(start,end,probability)))
                       and 0<=start<end<=duration+.03 and end-start>=.04
                       and .35<=probability<=1 and start>=last_start)
            except (KeyError,TypeError,ValueError):valid=False
            if not valid:
                # Preserve useful recent speech before a noisy ASR suffix, but
                # never join fragments across the rejected word's boundary.
                if observations:runs.append(observations)
                observations=[];continue
            last_start=start
            if not _kana(text):continue
            if end<duration-6:continue
            observations.append({'text':text,'start':start,'end':min(duration,end),
                                 'probability':probability})
        if observations:runs.append(observations)
        return runs

    def _candidate(self,index,observations,start_position,duration):
        choices=[]
        for channel in range(2):
            target=self.targets[index][channel];source='';locations=[]
            for number,word in enumerate(observations):
                text=_kana(word['text']) if channel==0 else _kana(self.reader(word['text']))
                source+=text;locations.extend((number,char) for char in range(len(text)))
            # ASR repetition can otherwise make quadratic alignment needlessly big.
            if len(source)>180:source=source[-180:];locations=locations[-180:]
            aligned=_suffix_alignment(source,target)
            if not aligned:continue
            pairs=aligned['pairs'];count=len(pairs)
            if count<4 or aligned['similarity']<.78:continue
            text=''.join(source[left] for left,_ in pairs)
            if len(set(text))<3 or is_vocalise(text):continue
            first=observations[locations[pairs[0][0]][0]]
            last=observations[locations[pairs[-1][0]][0]]
            extent=last['end']-first['start']
            if last['end']<duration-2.2 or extent<.25 or count/extent>45:continue
            probability=sum(observations[locations[left][0]]['probability'] for left,_ in pairs)/count
            confidence=probability*aligned['similarity']
            if confidence<.58:continue
            prefix=(pairs[0][1]==0 and locations[pairs[0][0]][1]==0
                    and first['start']>=.08 and first['probability']>=.65)
            choices.append({'index':index,'confidence':confidence,'characters':count,
                'onset':start_position+first['start'] if prefix else None,
                'evidence_start':start_position+first['start'],
                'evidence_end':start_position+last['end'],'similarity':aligned['similarity'],
                'match_method':'text' if channel==0 else 'kana',
                '_text':text,'_channel':channel,'_rank':confidence+.012*min(count,20)})
        return max(choices,key=lambda value:value['_rank']) if choices else None

    def _global_recovery(self,runs,start_position,duration,lower,upper):
        """Reacquire with two different, ordered, unique lyric rows.

        A repeated observation of one distant phrase cannot choose a new song
        position. Both rows must be strong matches outside the normal search,
        and the second row must actually follow the first in the captured audio.
        """
        window_end=start_position+duration
        sources=[]
        for observations in runs:
            channels=[''.join(_kana(word['text']) for word in observations),
                      ''.join(_kana(self.reader(word['text'])) for word in observations)]
            sources.append((observations,[{text[i:i+3] for i in range(len(text)-2)}
                                          for text in channels]))
        candidates=[]
        floor=0 if self.index is None else self.index+1
        for index in range(floor,len(self.lines)):
            if lower<=index<=upper:continue
            matches=[]
            for observations,source_grams in sources:
                # Cheap lexical filtering bounds the extra work; elapsed song
                # time and a predicted row never supply recovery evidence.
                plausible=any(len(target)>=6 and len(source_grams[channel]&
                              {target[i:i+3] for i in range(len(target)-2)})>=3
                              for channel,target in enumerate(self.targets[index]))
                if not plausible:continue
                candidate=self._candidate(index,observations,start_position,duration)
                if candidate:matches.append(candidate)
            if matches:candidates.append(max(matches,key=lambda value:(value['evidence_end'],value['_rank'])))
        if not candidates:self.pending=None;return None
        candidates.sort(key=lambda value:(-value['evidence_end'],-value['_rank'],value['index']))
        best=candidates[0];evidence=best['_text'];channel=best['_channel']
        if best['confidence']<.8 or best['similarity']<.9 or best['characters']<6:
            self.pending=None;return None
        # Include all lyric rows, including previous refrains, in uniqueness.
        unique=not any(other!=best['index'] and evidence in target[channel]
                       for other,target in enumerate(self.targets))
        ambiguous=any(candidate['evidence_end']>=best['evidence_end']-.4
                      and candidate['_rank']>=best['_rank']-.12 for candidate in candidates[1:])
        if not unique or ambiguous:self.pending=None;return None
        pending=self.pending
        agrees=(pending is not None and pending.get('scope')=='global'
                and best['index']==pending['index']+1
                and start_position<pending['window_end']
                and window_end-pending['window_end']>=.25
                and best['characters']+pending['characters']>=13
                and pending['evidence_end']-.4<=best['evidence_start']<=pending['evidence_end']+6
                and best['evidence_end']>pending['evidence_end']+.15)
        if not agrees:
            self.pending={'scope':'global','index':best['index'],'window_end':window_end,
                          'evidence_start':best['evidence_start'],'evidence_end':best['evidence_end'],
                          'confidence':best['confidence'],'similarity':best['similarity'],
                          'characters':best['characters']}
            return None
        self.index=best['index'];self.pending=None;self.confirmed_end=best['evidence_end']
        combined_characters=best['characters']+pending['characters']
        best.update(confidence=min(best['confidence'],pending['confidence']),
                    similarity=min(best['similarity'],pending['similarity']),
                    characters=min(best['characters'],pending['characters']))
        return {key:(round(value,3) if key=='confidence' else value)
                for key,value in {**best,'confirmations':2,'unique':True,'recovery':True,
                    'global_recovery':True,'distinct_lines':2,'recovery_start_index':pending['index'],
                    'recovery_characters':combined_characters}.items()
                if not key.startswith('_')}

    def _anchored_successor(self,candidate,start_position,window_end):
        """A unique preceding row can locate its otherwise repeated successor."""
        pending=self.pending
        return bool(self.index is not None and pending and pending.get('scope')!='global'
                    and pending.get('unique') and pending.get('confidence',0)>=.72
                    and pending.get('similarity',0)>=.86 and pending.get('characters',0)>=7
                    and candidate['index']==pending['index']+1<=self.index+3
                    and candidate['confidence']>=.72 and candidate['similarity']>=.86
                    and candidate['characters']>=5
                    and start_position<pending['window_end']
                    and window_end-pending['window_end']>=.25
                    and pending['evidence_end']-.4<=candidate['evidence_start']<=pending['evidence_end']+6
                    and candidate['evidence_end']>pending['evidence_end']+.15)

    def _recent_successor(self,candidate,window_end):
        # At most one or two unheard rows may separate fresh acoustic anchors.
        # This time limit only expires context; time alone never advances a row.
        return bool(self.index is not None and self.confirmed_end>=0
                    and 0<=window_end-self.confirmed_end<=12
                    and self.index<candidate['index']<=self.index+3
                    and candidate['confidence']>=.8 and candidate['similarity']>=.9
                    and candidate['characters']>=4)

    @staticmethod
    def _retain_pending(pending,start_position,window_end):
        # A single poor decoder window must not erase a good measured anchor.
        # Its audio still has to overlap, and its evidence expires promptly.
        return (pending if pending is not None and start_position<pending['window_end']
                and window_end-pending['window_end']<=4
                and window_end-pending['evidence_end']<=6 else None)

    def select(self,words,start_position,duration,current_index=None):
        """Return a confirmed match, or None when acoustic evidence is uncertain.

        ``current_index`` is an optional prediction used only as a bounded search
        hint before acquisition. It is never accepted as an observed position.
        Duplicate/old capture windows cannot supply a second confirmation.
        Overlapping windows share audio: confirmation checks temporal agreement,
        and is not a claim that the two observations are statistically independent.
        """
        if not self.lines or not all(map(math.isfinite,(start_position,duration))):return None
        window_end=start_position+duration
        if start_position<0 or not .5<=duration<=12 or window_end<=self.last_window+.05:return None
        self.last_window=window_end
        runs=self._observations(words,duration)
        if not runs:
            self.pending=self._retain_pending(self.pending,start_position,window_end)
            return None
        if self.index is None:
            seed=self.start_index
            if current_index is not None:
                seed=max(self.start_index-4,min(self.start_index+6,int(current_index)))
            lower=max(0,self.start_index-4,seed-4)
            upper=min(len(self.lines)-1,self.start_index+6,seed+6)
        else:lower=self.index;upper=min(len(self.lines)-1,self.index+10)
        previous_pending=self.pending
        def recover():
            self.pending=previous_pending if previous_pending and previous_pending.get('scope')=='global' else None
            result=self._global_recovery(runs,start_position,duration,lower,upper)
            if result is None and self.pending is None:
                self.pending=self._retain_pending(previous_pending,start_position,window_end)
            return result
        candidates=[]
        for index in range(lower,upper+1):
            matches=[candidate for observations in runs
                     if (candidate:=self._candidate(index,observations,start_position,duration))]
            if matches:
                candidates.append(max(matches,key=lambda value:(value['evidence_end'],value['_rank'])))
        if not candidates:return recover()
        candidates.sort(key=lambda value:(-value['evidence_end'],-value['_rank'],value['index']))
        best=candidates[0]
        rivals=[candidate for candidate in candidates[1:]
                if candidate['evidence_end']>=best['evidence_end']-.4
                and candidate['_rank']>=best['_rank']-.1]
        if rivals:
            near=min([best,*rivals],key=lambda value:value['index'])
            anchored=next((candidate for candidate in [best,*rivals]
                           if self._anchored_successor(candidate,start_position,window_end)),None)
            # Existing continuity can keep the current refrain or its adjacent
            # successor. A tie must never select a later occurrence or recover.
            if anchored is not None:near=anchored
            elif self.index is None or near['index']>self.index+1:
                reachable=[candidate for candidate in [best,*rivals]
                           if self.index is not None and self.index<candidate['index']<=self.index+3]
                if len(reachable)!=1 or not self._recent_successor(near,window_end):return recover()
            best=near
        index=best['index'];previous=self.index
        recovery=previous is None or index>previous+1
        evidence=best['_text'];channel=best['_channel']
        unique=not any(other!=index and evidence in target[channel]
                       for other,target in enumerate(self.targets))
        anchored=self._anchored_successor(best,start_position,window_end)
        nearby=self._recent_successor(best,window_end)
        strong=(best['confidence']>=.72 and best['similarity']>=.86
                and best['characters']>=(7 if previous is not None else 5) and unique)
        vocalise=previous is not None and index>previous and is_vocalise(self.lines[previous])
        requires_confirmation=recovery or vocalise
        if requires_confirmation and not (strong or anchored or nearby):return recover()
        if not requires_confirmation and (best['characters']<4 or best['confidence']<.65):
            return recover()
        confirmations=1
        if requires_confirmation:
            pending=self.pending if not self.pending or self.pending.get('scope')!='global' else None
            temporal_agreement=False
            if pending is not None:
                if index==pending['index']:
                    temporal_agreement=(max(best['evidence_start'],pending['evidence_start'])
                                        <=min(best['evidence_end'],pending['evidence_end'])+.75)
                elif index==pending['index']+1:
                    temporal_agreement=(best['evidence_start']>=pending['evidence_end']-.4
                                        and best['evidence_end']>pending['evidence_end']+.15)
            agrees=(pending is not None and pending['index']<=index<=pending['index']+1
                    and start_position<pending['window_end']
                    and window_end-pending['window_end']>=.25
                    and best['evidence_end']>=pending['evidence_end']-.4
                    and temporal_agreement)
            if not agrees:
                self.pending={'index':index,'window_end':window_end,
                              'evidence_start':best['evidence_start'],
                              'evidence_end':best['evidence_end'],'unique':unique,
                              'confidence':best['confidence'],'similarity':best['similarity'],
                              'characters':best['characters']}
                return None
            confirmations=2
            if anchored:
                best['confidence']=min(best['confidence'],pending['confidence'])
                best['similarity']=min(best['similarity'],pending['similarity'])
        self.pending=None;self.index=index;self.confirmed_end=best['evidence_end']
        return {key:(round(value,3) if key=='confidence' else value)
                for key,value in {**best,'confirmations':confirmations,'unique':unique,
                                  'recovery':recovery,'sequence_confirmed':anchored}.items() if not key.startswith('_')}
