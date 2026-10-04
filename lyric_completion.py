"""Fill omitted rows only when the complete saved sequence is present, in order."""
from lyrics import Line,normalized
from lyric_annotations import match_annotations
from timing_quality import timing_origins,automatic_times


def complete_lines(saved,supplied,minimum=2):
    if not minimum<=len(saved)<len(supplied)<=600:return None
    keys=[normalized(line.original) for line in supplied]
    if not all(keys):return None
    spans=[];cursor=0
    for line in saved:
        key=normalized(line.original);found=None
        if not key:return None
        for start in range(cursor,len(keys)):
            joined=''
            for end in range(start,min(start+6,len(keys))):
                joined+=keys[end]
                if joined==key:found=(start,end+1);break
                if len(joined)>=len(key):break
            if found:break
        if found is None:return None
        spans.append(found);cursor=found[1]
    # Merely splitting a long saved line is not missing content.
    if sum(end-start for start,end in spans)==len(supplied):return None
    extras=match_annotations(supplied,saved)
    incoming=tuple(Line(line.original,line.reading or (old.reading if old else ''),
                        line.korean or (old.korean if old else '')) for line,old in zip(supplied,extras))
    result=[];indices=[];cursor=0
    for old,(start,end) in enumerate(spans):
        result.extend(incoming[cursor:start]);indices.extend([None]*(start-cursor))
        result.append(saved[old]);indices.append(old);cursor=end
    result.extend(incoming[cursor:]);indices.extend([None]*(len(incoming)-cursor))
    return tuple(result),tuple(indices)


def expand_timing(timing,indices):
    result={**timing}
    for key,values in (('times',timing['times']),('origins',timing_origins(timing)),
                       ('automatic_times',automatic_times(timing))):
        result[key]=[values[i] if i is not None else None for i in indices]
    result.pop('fingerprint',None)
    result.pop('audio_finalized',None)
    result.pop('analyzed_ranges',None)
    result.pop('analysis_duration',None)
    result['completion_pending']=True
    return result


def restore_spotify_rows(saved,result,timing):
    """Recover timed provider phrases lost to cached/document wording differences."""
    from lyric_follow import native_line_mapping
    source=result['lines'];times=result['times']
    if result.get('source')!='Spotify' or not source or not saved:return None
    if sum(t is not None for t in times)/len(source)<.8:return None
    forward,_=native_line_mapping([line.original for line in source],saved)
    if all(i in forward for i,t in enumerate(times) if t is not None):return None
    reverse,_=native_line_mapping([line.original for line in saved],source)
    if max(len(forward),len(reverse))/max(len(source),len(saved))<.8:return None
    onsets,_=native_line_mapping([line.original for line in saved],source,onsets_only=True)
    indices=[None]*len(source)
    for old,new in sorted(onsets.items()):
        if indices[new] is None:indices[new]=old
    # Never silently discard a user's correction on an unverified or merged row.
    kept=set(indices)
    if any(origin=='manual' and i not in kept for i,origin in enumerate(timing_origins(timing))):return None
    extras=match_annotations(source,saved)
    # A typo elsewhere must not erase a correctly translated split sentence.
    # Use only mutually aligned, exact, small blocks in this recording.
    for old,start in sorted(reverse.items()):
        if forward.get(start)!=old:continue
        found=False;text=''
        for end in range(old,min(old+4,len(saved))):
            text+=normalized(saved[end].original)
            if len(text)>160:break
            incoming=''
            for stop in range(start,min(start+4,len(source))):
                incoming+=normalized(source[stop].original)
                if incoming==text:
                    block=match_annotations(source[start:stop+1],saved[old:end+1])
                    for row,extra in enumerate(block,start):
                        if extra and not (extras[row] and extras[row].korean.strip()):extras[row]=extra
                    found=True;break
                if len(incoming)>=len(text):break
            if found:break
    lines=tuple(Line(line.original,
                     (extra.reading if extra else '') or line.reading,
                     (extra.korean if extra else '') or line.korean)
                for line,extra in zip(source,extras))
    return lines,tuple(indices)
