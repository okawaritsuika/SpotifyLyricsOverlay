"""Conservative word-to-lyric matching and immutable manual timing anchors."""
import math

from lyrics import normalized
from storage import validate_times
from timing_quality import timing_origins,automatic_times,cleared_rows


def align_characters(original,transcript):
    """Global sequence alignment; repeated choruses cannot discard the middle."""
    width=len(transcript)+1
    if (len(original)+1)*width>20_000_000:
        raise ValueError('인식 결과가 너무 길어 안전하게 대조하지 못했습니다. 수동 시간표를 사용하세요.')
    trace=bytearray((len(original)+1)*width)
    previous=[-i for i in range(width)]
    for i,a in enumerate(original,1):
        current=[-i]+[0]*len(transcript)
        for j,b in enumerate(transcript,1):
            diagonal=previous[j-1]+(2 if a==b else -1)
            up=previous[j]-1
            left=current[j-1]-1
            score=max(diagonal,up,left)
            current[j]=score
            trace[i*width+j]=0 if score==diagonal else (1 if score==up else 2)
        previous=current
    mapping={}
    i,j=len(original),len(transcript)
    while i and j:
        direction=trace[i*width+j]
        if direction==0:
            if original[i-1]==transcript[j-1]:mapping[i-1]=j-1
            i-=1;j-=1
        elif direction==1:i-=1
        else:j-=1
    return mapping


def match_words(lines, words, start_position=0.0, duration=900.0):
    originals = [normalized(line.original) for line in lines]
    transcript, moments = [], []
    previous = -1.0
    for word in words:
        start, end = float(word['start']), float(word['end'])
        confidence = float(word.get('probability', 1))
        if (not all(math.isfinite(x) for x in (start, end, confidence))
                or start < previous or start < 0 or end <= start or confidence < .35):
            continue
        text = normalized(word['word'])
        if not text:
            continue
        previous = start
        transcript.extend(text)
        # All characters of a recognized word share its measured onset. We do not
        # invent intra-word timings; duplicate line starts are rejected below.
        moments.extend([start + start_position] * len(text))
    combined = ''.join(originals)
    mapping = align_characters(combined, ''.join(transcript))
    times, coverage, cursor, last = [], [], 0, -1.0
    for original in originals:
        pairs = [(j, mapping[cursor+j]) for j in range(len(original)) if cursor+j in mapping]
        ratio = len(pairs) / len(original) if original else 0
        stamp = None
        # Very short/common snippets are too ambiguous to timestamp automatically.
        if (len(pairs) >= 3 and ratio >= .70 and pairs[0][0] <= min(1, len(original)*.15)):
            first, final = pairs[0][1], pairs[-1][1]
            contiguous = (final-first+1) <= len(original)*1.7 + 2
            candidate = moments[first]
            if contiguous and last < candidate < duration and moments[final]-candidate <= 25:
                stamp = round(candidate, 3)
                last = stamp
        times.append(stamp)
        coverage.append(round(ratio, 3))
        cursor += len(original)
    validate_times(times, len(lines))
    return {'times': times, 'coverage': coverage,
            'matched': sum(t is not None for t in times), 'duration': duration}


def _partial_runs(words,start_position,duration):
    """Rejected speech is a boundary, never a bridge between lyric fragments."""
    runs=[];run=[];previous=-1.
    for word in words:
        try:
            text=word['word'];start=float(word['start']);end=float(word['end'])
            probability=float(word.get('probability',1))
            valid=(isinstance(text,str) and all(math.isfinite(x) for x in (start,end,probability))
                   and 0<=start<end<=duration-start_position+.03 and start>=previous
                   and .35<=probability<=1)
        except (KeyError,TypeError,ValueError):valid=False
        if not valid:
            if run:runs.append(run)
            run=[];continue
        previous=start
        if not normalized(text):continue
        run.append({'text':text,'start':start,'end':min(end,duration-start_position),'probability':probability})
    if run:runs.append(run)
    return runs


def _partial_row_candidates(target,source,locations,words):
    """Local matches penalize every internal skip instead of unused song ends."""
    import heapq
    from lyric_follow import is_vocalise
    width=len(source)+1
    if not target or not source:return []
    if (len(target)+1)*width>20_000_000:
        raise ValueError('인식 결과가 너무 길어 안전하게 대조하지 못했습니다.')
    trace=bytearray((len(target)+1)*width);previous=[0.]*width;peaks=[]
    for i,char in enumerate(target,1):
        current=[0.]*width
        for j,other in enumerate(source,1):
            values=(0.,previous[j-1]+(3 if char==other else -3),previous[j]-4,current[j-1]-4)
            move=max(range(4),key=values.__getitem__)
            current[j]=values[move];trace[i*width+j]=move
            if char==other and i>=len(target)*.7 and current[j]>0:
                item=(current[j],i,j)
                if len(peaks)<32:heapq.heappush(peaks,item)
                elif item>peaks[0]:heapq.heapreplace(peaks,item)
        previous=current
    candidates={}
    for score,end_i,end_j in sorted(peaks,reverse=True):
        i,j=end_i,end_j;pairs=[]
        while i and j:
            move=trace[i*width+j]
            if not move:break
            if move==1:
                if target[i-1]==source[j-1]:pairs.append((i-1,j-1))
                i-=1;j-=1
            elif move==2:i-=1
            else:j-=1
        if not pairs:continue
        pairs.reverse();count=len(pairs);ratio=count/len(target)
        first_i,first_j=pairs[0];last_i,last_j=pairs[-1]
        similarity=count/max(last_i-first_i+1,last_j-first_j+1)
        if (count<3 or ratio<.7 or similarity<.8 or first_i>min(1,len(target)*.15)
                or locations[first_j][1]!=0):continue
        evidence=''.join(source[b] for _,b in pairs)
        if len(set(evidence))<3 or is_vocalise(evidence):continue
        first_word,last_word=locations[first_j][0],locations[last_j][0]
        first,last=words[first_word],words[last_word]
        if not .2<=last['end']-first['start']<=25:continue
        probability=sum(words[locations[b][0]]['probability'] for _,b in pairs)/count
        candidate={'start':first['start'],'end':last['end'],'coverage':ratio,
                   'rank':count*similarity*probability,'quality':ratio*similarity*probability,
                   'first_word':first_word,'last_word':last_word,
                   'evidence':evidence,'observed':source[first_j:last_j+1],'prefix':first_i==0}
        key=(first_word,last_word)
        if key not in candidates or candidate['rank']>candidates[key]['rank']:candidates[key]=candidate
    return sorted(candidates.values(),key=lambda c:c['rank'],reverse=True)


def _partial_choose_rows(all_candidates):
    selected=[]
    for candidates in all_candidates:
        if not candidates:continue
        candidates=sorted(candidates,key=lambda c:c['rank'],reverse=True);best=candidates[0]
        # Two separate occurrences with similar support cannot choose a clock.
        if any(other['rank']>=best['rank']*.9 and abs(other['start']-best['start'])>1.5
               for other in candidates[1:]):continue
        selected.append(best)
    return selected


def _partial_unambiguous(candidates):
    accepted=[]
    for candidate in candidates:
        ambiguous=False
        for other in candidates:
            if candidate is other or candidate['run']!=other['run']:continue
            overlap=min(candidate['last_word'],other['last_word'])-max(candidate['first_word'],other['first_word'])+1
            shorter=min(candidate['last_word']-candidate['first_word']+1,other['last_word']-other['first_word']+1)
            if overlap>0 and overlap/shorter>=.8:
                ambiguous=True;break
        if not ambiguous:accepted.append(candidate)
    return accepted


def _partial_chain(candidates):
    """Strong ordered observations win over a greedy distant first match."""
    scores=[];chains=[]
    for i,candidate in enumerate(candidates):
        previous=max((j for j in range(i) if candidates[j]['start']<candidate['start']
                      and candidates[j]['end']<=candidate['start']+.03),
                     key=lambda j:scores[j],default=None)
        scores.append(candidate['rank']+(scores[previous] if previous is not None else 0))
        chains.append((chains[previous] if previous is not None else [])+[i])
    chain=chains[max(range(len(scores)),key=scores.__getitem__)] if scores else []
    return [candidates[index] for index in chain]


def _partial_main_surface(text):
    """Ignore explicitly marked backing calls only for ambiguity checks."""
    from lyric_annotations import _outer_parentheses
    text,spans=_outer_parentheses(text)
    if not spans:return text
    cursor=0;pieces=[]
    for start,end in spans:
        pieces.append(text[cursor:start]);cursor=end
    pieces.append(text[cursor:])
    return ''.join(pieces)


def partial_alignment_text(text):
    """Omit explicit internal/backing calls while preserving the row's onset.

    A preceding parenthesized call may be the row's first audible word, so
    that row always keeps the full source. Unbalanced parentheses also stay.
    """
    from lyric_annotations import _outer_parentheses
    normalized_text,spans=_outer_parentheses(text)
    if not spans or not normalized(normalized_text[:spans[0][0]]):return text
    main=_partial_main_surface(text)
    return main if len(normalized(main))>=3 else text


def _partial_fits_anchors(candidate,index,anchors):
    for anchor in anchors:
        if index<anchor['index']:
            if candidate['end']>anchor['start']+.03:return False
        elif index>anchor['index']:
            if candidate['start']<anchor['end']-.03:return False
        elif candidate['end']<=anchor['start'] or candidate['start']>=anchor['end']:
            return False
    return True


def match_partial_words(lines,words,start_position=0.0,duration=900.0,*,minimum_coverage=None,minimum_quality=0):
    """Find heard lyric rows locally in text and Japanese reading channels.

    Character locations always point back to an observed ASR word. Reading
    expansion supplies no interpolated timestamps, including at line endings.
    """
    from realtime_lyrics import _kana,_reading
    if (not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
                for x in (start_position,duration)) or not 0<=start_position<duration):
        raise ValueError('부분 분석 재생 구간이 올바르지 않습니다.')
    targets=[(_kana(line.original),_kana(_reading(line.original))) for line in lines]
    main_texts=[_partial_main_surface(line.original) for line in lines]
    mains=[(_kana(text),_kana(_reading(text))) for text in main_texts]
    alignment_texts=[partial_alignment_text(line.original) for line in lines]
    alignment_targets=[(_kana(text),_kana(_reading(text))) for text in alignment_texts]
    runs=_partial_runs(words,start_position,duration)
    all_candidates=[[] for _ in lines];coverage=[0.]*len(lines)
    for run_id,run in enumerate(runs):
        channels=[]
        for channel in range(2):
            source='';locations=[]
            for number,word in enumerate(run):
                value=_kana(word['text']) if channel==0 else _kana(_reading(word['text']))
                source+=value;locations.extend((number,char) for char in range(len(value)))
            channels.append((source,locations))
        for index,target in enumerate(targets):
            if len(target[0])<3:continue
            for channel,(source,locations) in enumerate(channels):
                variants=dict.fromkeys((target[channel],alignment_targets[index][channel]))
                for variant in variants:
                    for candidate in _partial_row_candidates(variant,source,locations,run):
                        if variant!=target[channel] and not candidate['prefix']:continue
                        if candidate['quality']<minimum_quality:continue
                        if minimum_coverage is not None:
                            minimum=minimum_coverage[index]
                            if candidate['coverage']<minimum:continue
                            if minimum==1 and candidate['observed']!=candidate['evidence']:continue
                        coverage[index]=max(coverage[index],candidate['coverage'])
                        # A short phrase can be contained in another longer row,
                        # even though the complete row strings are all distinct.
                        alternatives={other for other,alternate in enumerate(targets) if other!=index
                            and (candidate['evidence'] in alternate[channel]
                                 or candidate['evidence'] in mains[other][channel]
                                 or target[1] and target[1]==alternate[1]
                                 or len(mains[index][channel])>=3 and mains[index][channel]==mains[other][channel])}
                        # Score every spelling/aside variant against the same
                        # main-row length; optional calls cannot inflate its vote.
                        all_candidates[index].append({**candidate,'rank':len(alignment_targets[index][0])*candidate['quality'],
                            'index':index,'run':run_id,'channel':channel,'ambiguous_text':bool(alternatives),
                            'alternatives':alternatives})
    unique=_partial_choose_rows([[candidate for candidate in row if not candidate['ambiguous_text']]
                                for row in all_candidates])
    anchors=_partial_chain(_partial_unambiguous(unique))
    if anchors:
        anchor_rows={anchor['index']:anchor for anchor in anchors}
        constrained=[]
        for index,row in enumerate(all_candidates):
            if index in anchor_rows:
                constrained.append([anchor_rows[index]]);continue
            constrained.append([candidate for candidate in row if _partial_fits_anchors(candidate,index,anchors)
                and not any(_partial_fits_anchors(candidate,other,anchors) for other in candidate['alternatives'])])
        resolved=_partial_unambiguous(_partial_choose_rows(constrained))
        chosen=_partial_chain(resolved)
    else:chosen=[]
    times=[None]*len(lines);ends=[None]*len(lines)
    for candidate in chosen:
        index=candidate['index']
        times[index]=round(start_position+candidate['start'],3)
        ends[index]=round(start_position+candidate['end'],3)
    validate_times(times,len(lines))
    return {'times':times,'ends':ends,'coverage':[round(value,3) for value in coverage],
            'matched':sum(t is not None for t in times),'duration':duration}


def merge_candidates(current, candidates, replace_audio=False):
    """Keep exact anchors and offset; convert raw audio time into offset coordinates."""
    times = list(current['times'])
    origins=timing_origins(current)
    if replace_audio:
        for index,origin in enumerate(origins):
            if origin=='audio':times[index]=None;origins[index]=None
    validate_times(candidates, len(times))
    offset = current.get('offset', 0.0)
    baseline=automatic_times(current)
    converted=[round(x-offset,3) if x is not None and x-offset>=0 else None for x in candidates]
    if replace_audio:baseline=converted
    else:
        for index,value in enumerate(converted):
            if value is None or baseline[index] is not None:continue
            before=next((x for x in reversed(baseline[:index]) if x is not None),-1)
            after=next((x for x in baseline[index+1:] if x is not None),math.inf)
            if before<value<after:baseline[index]=value
    added = 0
    cleared = cleared_rows(current)
    for index, candidate in enumerate(candidates):
        if times[index] is not None or candidate is None or index in cleared:
            continue
        candidate = round(candidate-offset, 3)
        before = next((x for x in reversed(times[:index]) if x is not None), -1)
        after = next((x for x in times[index+1:] if x is not None), math.inf)
        if candidate >= 0 and before < candidate < after:
            times[index] = candidate
            origins[index]='audio'
            added += 1
    validate_times(times, len(times))
    result = {**current, 'times': times, 'origins':origins,'automatic_times':baseline}
    if added:
        result['source'] = '직접 기록 / 음원 분석' if 'manual' in origins else '음원 분석'
    return result, added


def capture_problem(snapshot, expected_key, expected_position, now):
    if not snapshot.has_timeline or now-snapshot.captured > 3:
        return 'Spotify 재생 정보 연결이 끊겼습니다. 다시 분석해 주세요.'
    if snapshot.key != expected_key:
        return '곡이 바뀌어 분석을 취소했습니다.'
    if not snapshot.playing:
        return '일시정지되어 분석을 취소했습니다. 처음부터 다시 재생해 주세요.'
    if abs(snapshot.rate-1) > .01:
        return '보통 재생 속도에서 분석해 주세요.'
    if abs(snapshot.position(now)-expected_position) > 1.5:
        return '재생 위치가 바뀌어 분석을 취소했습니다. 처음부터 다시 재생해 주세요.'
    return ''
