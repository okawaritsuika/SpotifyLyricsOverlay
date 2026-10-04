"""Align supplied Japanese lines; reject collapsed or weak line candidates."""
import math
from lyrics import normalized
from storage import validate_times

ALIGNMENT_METHOD='bounded-gap-alignment-v2'
INCREMENTAL_METHOD='incremental-lyrics-alignment-v1'


class AlignmentUnmatched(ValueError):
    """The model ran, but did not establish reliable lyric timestamps."""


def normalize_ranges(ranges,duration):
    """Validate song-coordinate coverage and merge touching intervals."""
    def number(value):
        return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
    if not number(duration) or duration<=0 or not isinstance(ranges,list):
        raise ValueError('분석 구간 형식이 올바르지 않습니다.')
    cleaned=[]
    for item in ranges:
        if (not isinstance(item,(list,tuple)) or len(item)!=2 or not all(number(x) for x in item)
                or not 0<=item[0]<item[1]<=duration):
            raise ValueError('분석 구간이 곡 길이를 벗어났습니다.')
        cleaned.append([float(item[0]),float(item[1])])
    merged=[]
    for start,end in sorted(cleaned):
        if merged and start<=merged[-1][1]:merged[-1][1]=max(end,merged[-1][1])
        else:merged.append([start,end])
    return merged


def uncovered_ranges(start,end,covered):
    """Subtract validated coverage without adding inferred timeline positions."""
    if (isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,(int,float))
            or not isinstance(end,(int,float)) or not math.isfinite(start) or not math.isfinite(end)
            or not 0<=start<end):
        raise ValueError('수집 구간 형식이 올바르지 않습니다.')
    # Existing coverage can extend beyond this particular captured excerpt.
    duration=max([end]+[item[1] for item in covered if isinstance(item,(list,tuple)) and len(item)==2
                       and isinstance(item[1],(int,float)) and not isinstance(item[1],bool)
                       and math.isfinite(item[1])]) if isinstance(covered,list) else end
    ranges=normalize_ranges(covered,duration)
    missing=[];cursor=float(start)
    for left,right in ranges:
        if right<=cursor:continue
        if left>=end:break
        if left>cursor:missing.append([cursor,min(float(end),left)])
        cursor=max(cursor,right)
        if cursor>=end:break
    if cursor<end:missing.append([cursor,float(end)])
    return missing

def line_candidates(lines,segments,start_position,duration):
    if len(lines)!=len(segments):
        raise ValueError('음원과 가사의 구절 수가 달라 자동 적용하지 않았습니다.')
    times=[];coverage=[];confidence=[];last=-1.0
    for line,segment in zip(lines,segments):
        if normalized(line.original)!=normalized(segment['text']):
            raise ValueError('정렬된 가사 순서가 원문과 달라 자동 적용하지 않았습니다.')
        words=[w for w in segment.get('words',[]) if normalized(w['word'])]
        positive=[w for w in words if all(math.isfinite(float(w[k])) for k in ('start','end','probability'))
                  and w['end']>w['start']>=0]
        count=sum(len(normalized(w['word'])) for w in words)
        aligned=sum(len(normalized(w['word'])) for w in positive)
        ratio=aligned/count if count else 0
        score=sum(w['probability']*len(normalized(w['word'])) for w in positive)/aligned if aligned else 0
        stamp=None
        if (len(normalized(line.original))>=3 and positive and positive[0] is words[0]
                and ratio>=.7 and score>=.25):
            candidate=positive[0]['start']+start_position
            extent=positive[-1]['end']-positive[0]['start']
            if last<candidate<duration and .2<=extent<=25:
                stamp=round(candidate,3)
                last=stamp
        times.append(stamp);coverage.append(round(ratio,3));confidence.append(round(score,3))
    validate_times(times,len(lines))
    return {'times':times,'coverage':coverage,'confidence':confidence,'matched':sum(t is not None for t in times),
            'duration':duration,'method':'supplied-lyrics-alignment'}


def align_lyrics(request,samples,start_position,progress,emit=None):
    import torch
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    from audio_runtime import load_final_model
    model,runtime=load_final_model(request,emit)
    try:
        return _align_with_model(model,request,samples,start_position,progress)
    except RuntimeError:
        if runtime['device']!='cuda':raise
    # Full-length attention can need more VRAM than the warmup. Release the
    # failed CUDA model before one CPU retry; no partial result was committed.
    try:model.model.unload_model()
    except RuntimeError:pass
    del model
    import gc
    gc.collect()
    model,runtime=load_final_model({**request,'final_device':'cpu'})
    runtime.update(fallback=True,fallback_reason='cuda_inference_failed')
    if emit:
        emit('progress',state='analyzing',runtime=runtime,restart=True,
             text='GPU 정렬을 완료하지 못해 CPU 소형 모델로 전체 분석을 한 번 다시 시작합니다.')
    return _align_with_model(model,request,samples,start_position,progress)


def _align_with_model(model,request,samples,start_position,progress):
    end=min(request['duration'],start_position+len(samples)/16000)
    incremental=bool(request.get('incremental'))
    raw_known=request.get('confirmed_times',[None]*len(request['lines']))
    validate_times(raw_known,len(request['lines']))
    known=list(raw_known)
    if any(t is not None and t>=request['duration'] for t in known):
        raise ValueError('저장된 가사 시각이 곡 길이를 벗어났습니다.')
    covered=normalize_ranges(request.get('analyzed_ranges',[]),request['duration'])
    full=start_position<=3 and end>=request['duration']-2
    if full and not covered and not any(t is not None for t in known):
        try:result=_align_full(model,request,samples,start_position,
                              lambda current,total:progress(.7*current/max(total,.01),1))
        except AlignmentUnmatched:result=None
        if result is not None and result['matched']:
            if incremental:
                result.update(incremental=True,analyzed_ranges=[[start_position,end]],partial=False)
            progress(1,1)
            return result
        # Failed whole-song alignment can still contain independently heard
        # phrases. Recover those in bounded windows instead of losing them all.
        report=progress
        progress=lambda current,total:report(.7+.3*current/max(total,.01),1)
    # Even older callers must not squeeze an unheard suffix into a short clip.
    return _align_incremental(model,request,samples,start_position,end,known,covered,progress)


def _align_full(model,request,samples,start_position,progress):
    from lyrics import Line
    result=model.align(samples,'\n'.join(request['lines']),language='ja',original_split=True,regroup=False,
                       verbose=None,suppress_silence=False,nonspeech_skip=None,
                       progress_callback=lambda current,total:progress(current*.8,total))
    if result is None:
        raise AlignmentUnmatched('음원 분석은 끝났지만 확실하게 일치하는 구절을 찾지 못했습니다. 기존 시간표를 유지합니다.')
    lines=[Line(text,'','') for text in request['lines']]
    segments=result.to_dict()['segments']
    baseline=line_candidates(lines,segments,start_position,request['duration'])
    refined=recover_bounded_gaps(model,lines,samples,start_position,request['duration'],segments,baseline,
                                lambda current,total:progress(total*.8+current*.2,total))
    progress(len(samples)/16000,len(samples)/16000)
    return refined


def _partial_evidence(model,lines,samples,start,duration,left,right,progress):
    """Use independent recognition to locate the heard part of the document."""
    from collections import Counter
    from audio_sync import match_partial_words
    segments,_=model.transcribe_original(samples,language='ja',beam_size=3,best_of=1,
        temperature=0,word_timestamps=True,condition_on_previous_text=False,vad_filter=False,
        compression_ratio_threshold=2.4,log_prob_threshold=-1.0,no_speech_threshold=.6)
    words=[]
    for segment in segments:
        if segment.compression_ratio<=2.4 and segment.avg_logprob>=-1.0:
            words.extend({'word':word.word,'start':word.start,'end':word.end,
                          'probability':word.probability} for word in (segment.words or ()))
        else:words.append(None)
        progress(min(len(samples)/16000,segment.end),len(samples)/16000)
    result=match_partial_words(lines[left:right],words,start,duration)
    counts=Counter(normalized(line.original) for line in lines[left:right])
    # Repeated chorus text alone cannot identify its occurrence. Unique lines
    # establish the span before supplied-lyrics alignment can contribute rows.
    anchors={left+i:t for i,t in enumerate(result['times']) if t is not None
             and len(normalized(lines[left+i].original))>=5
             and counts[normalized(lines[left+i].original)]==1}
    if not anchors:return {},{},{}
    all_matches={left+i:t for i,t in enumerate(result['times']) if t is not None}
    ends={left+i:t for i,t in enumerate(result['ends']) if t is not None}
    return anchors,all_matches,ends


def _evidence_groups(evidence,ends):
    """Do not force missing verses between distant recognized snippets."""
    groups=[]
    for index in sorted(evidence):
        if (not groups or index-groups[-1][-1]>6
                or ends[index]-evidence[groups[-1][0]]>45):
            groups.append([])
        groups[-1].append(index)
    return groups


def _align_incremental(model,request,samples,start,end,known,covered,progress):
    from lyrics import Line
    from audio_sync import partial_alignment_text
    lines=[Line(text,'','') for text in request['lines']]
    count=len(lines);times=[None]*count;coverage=[0.0]*count;confidence=[0.0]*count
    processed=[];recovered=[]
    missing=uncovered_ranges(start,end,covered)
    for step,(target_start,target_end) in enumerate(missing):
        if target_end-target_start<.5:continue
        begin=max(start,target_start-2);finish=min(end,target_end+2)
        clip=samples[max(0,int((begin-start)*16000)):int((finish-start)*16000)]
        if len(clip)<.5*16000:continue
        # Earlier/later confirmed rows bound the allowed lyric occurrence.
        left=max([i+1 for i,t in enumerate(known) if t is not None and t<begin-2],default=0)
        right=min([i for i,t in enumerate(known) if t is not None and t>=finish],default=count)
        if left>=right or all(known[i] is not None for i in range(left,right)):continue
        report=lambda current,total:progress((step+current/max(total,.01)*.4)/max(1,len(missing)),1)
        anchors,evidence,ends=_partial_evidence(model,lines,clip,begin,request['duration'],left,right,report)
        if not anchors:continue
        groups=_evidence_groups(evidence,ends);added=0
        for group_number,group in enumerate(groups):
            if not any(index in anchors for index in group):continue
            # Bound both text and audio; distant matches are separate windows.
            span_left=group[0];span_right=group[-1]+1
            align_begin=max(begin,evidence[span_left]-2)
            align_end=min(finish,ends[span_right-1]+2)
            aligned_clip=samples[max(0,int((align_begin-start)*16000)):int((align_end-start)*16000)]
            local_request={**request,'lines':[partial_alignment_text(text)
                for text in request['lines'][span_left:span_right]]}
            try:
                candidate=_align_full(model,local_request,aligned_clip,align_begin,
                    lambda current,total:progress((step+.4+(group_number+current/max(total,.01))/len(groups)*.6)/max(1,len(missing)),1))
            except AlignmentUnmatched:continue
            agreed={i for i,t in anchors.items() if span_left<=i<span_right
                    and candidate['times'][i-span_left] is not None
                    and abs(candidate['times'][i-span_left]-t)<=1.5}
            if not agreed:continue
            # A failed boundary is not permission to infer intermediate phrases.
            bounds=(min(agreed),max(agreed))
            for relative,stamp in enumerate(candidate['times']):
                index=span_left+relative
                if stamp is None or known[index] is not None or not target_start<=stamp<target_end:continue
                support=evidence.get(index)
                supported=support is not None and abs(stamp-support)<=1.5
                bounded=bounds[0]<index<bounds[1]
                if not supported and not bounded:continue
                before=max([t for t in known[:index] if t is not None]+[t for t in times[:index] if t is not None],default=-1)
                after=min([t for t in known[index+1:] if t is not None]+[t for t in times[index+1:] if t is not None],default=math.inf)
                if not before<stamp<after:continue
                times[index]=stamp;coverage[index]=candidate['coverage'][relative]
                confidence[index]=candidate['confidence'][relative];added+=1
                if relative in candidate.get('recovered_indices',[]):recovered.append(index)
        if added:processed.append([target_start,target_end])
    validate_times(times,count)
    progress(1,1)
    return {'times':times,'coverage':coverage,'confidence':confidence,
            'matched':sum(t is not None for t in times),'duration':request['duration'],
            'method':INCREMENTAL_METHOD,'incremental':True,'analyzed_ranges':processed,
            'recovered_indices':recovered,'partial':True}


def recover_bounded_gaps(model,lines,samples,start_position,duration,segments,baseline,progress=None):
    """Re-align missing runs locally; keep them only when both endpoints agree."""
    times=list(baseline['times']);original=list(times);recovered=[]
    anchors=[i for i,t in enumerate(original) if t is not None]
    for left,right in zip(anchors,anchors[1:]):
        if not 1<right-left<=6:continue
        begin=max(0,original[left]-start_position-1)
        end=min(len(samples)/16000,float(segments[right]['end'])+1)
        if not .5<end-begin<=45:continue
        if progress:progress(begin,len(samples)/16000)
        local=model.align(samples[int(begin*16000):int(end*16000)],
                          '\n'.join(line.original for line in lines[left:right+1]),
                          language='ja',original_split=True,regroup=False,verbose=None,
                          suppress_silence=False,nonspeech_skip=None)
        if local is None:continue
        try:
            candidate=line_candidates(lines[left:right+1],local.to_dict()['segments'],
                                      start_position+begin,duration)
        except ValueError:continue
        values=candidate['times']
        # A repeated chorus or a failed local alignment must not move either anchor.
        if (values[0] is None or values[-1] is None or
                abs(values[0]-original[left])>1 or abs(values[-1]-original[right])>1):continue
        for relative,stamp in enumerate(values[1:-1],1):
            index=left+relative
            coarse=float(segments[index]['start'])+start_position
            # Agreement at the endpoints alone can still place a missing
            # phrase late in an instrumental gap. Contradictory full-track
            # and local onsets stay unconfirmed instead of becoming anchors.
            if (stamp is not None and math.isfinite(coarse) and abs(stamp-coarse)<=1.5
                    and original[left]<stamp<original[right]):
                times[index]=stamp;recovered.append(index)
    validate_times(times,len(lines))
    return {**baseline,'times':times,'matched':sum(x is not None for x in times),
            'recovered_indices':recovered,'method':ALIGNMENT_METHOD}
