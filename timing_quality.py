"""Numeric timing provenance and evaluation, without lyric or audio storage."""
from statistics import mean, median


def timing_origins(timing):
    times=timing['times']
    origins=timing.get('origins')
    if isinstance(origins,list) and len(origins)==len(times) and all(x in ('manual','audio',None) for x in origins):
        return [origin if stamp is not None else None for stamp,origin in zip(times,origins)]
    # Legacy mixed/unknown data may contain user work: preserve it conservatively.
    origin='audio' if timing.get('source')=='음원 분석' else 'manual'
    return [origin if stamp is not None else None for stamp in times]


def manual_priority(timing):
    return bool(timing and (timing.get('manual_priority') or timing.get('offset',0)!=0 or
                any(origin=='manual' for origin in timing_origins(timing))))


def saved_rows(timing):
    """Priority belongs to individual known rows, not to the entire song."""
    fixed_auto=timing.get('audio_finalized') or timing.get('manual_priority') or timing.get('source')=='LRCLIB'
    return [stamp is not None and bool(origin=='manual' or fixed_auto)
            for stamp,origin in zip(timing['times'],timing_origins(timing))]


def automatic_times(timing):
    """Keep the underlying automatic times when a row is manually corrected."""
    from storage import validate_times
    cached=timing.get('automatic_times')
    if cached is not None:
        validate_times(cached,len(timing['times']))
        return list(cached)
    return [stamp if origin=='audio' else None
            for stamp,origin in zip(timing['times'],timing_origins(timing))]


def recover_legacy_origins(current,draft,before):
    """Recover only unchanged machine stamps with a matching pre-analysis backup."""
    if current.get('origins') is not None or current.get('source')!='직접 기록 / 음원 분석':return current,0
    fingerprint=current.get('fingerprint')
    count=len(current['times'])
    if (not fingerprint or draft.get('fingerprint')!=fingerprint or before.get('fingerprint')!=fingerprint
            or len(draft.get('times',[]))!=count or len(before.get('times',[]))!=count):return current,0
    origins=timing_origins(current);recovered=0;offset=current.get('offset',0)
    for i,(stamp,candidate,original) in enumerate(zip(current['times'],draft['times'],before['times'])):
        # Any original record or subsequent edit remains protected. A candidate
        # alone, without the matching empty original slot, is not provenance.
        if original is None and stamp is not None and candidate is not None and abs(stamp+offset-candidate)<.0005:
            origins[i]='audio';recovered+=1
    return ({**current,'origins':origins} if recovered else current),recovered


def compare_times(reference,candidates,offset=0):
    if len(reference)!=len(candidates):raise ValueError('비교할 구절 수가 다릅니다.')
    rows=[{'line':i+1,'reference':float(r),'candidate':round(float(c)+offset,3),
           'error':round(float(c)+offset-float(r),3)}
          for i,(r,c) in enumerate(zip(reference,candidates)) if r is not None and c is not None]
    errors=[abs(x['error']) for x in rows]
    return {'compared':len(rows),'total':len(reference),
            'mean_absolute_error':round(mean(errors),3) if errors else None,
            'median_absolute_error':round(median(errors),3) if errors else None,
            'max_error':max(errors,default=None),
            'within_1s':sum(x<=1 for x in errors),'within_2s':sum(x<=2 for x in errors),'rows':rows}


def consensus_candidates(results,tolerance=.65):
    """Keep only onsets supported by at least two independently chunked passes."""
    count=len(results[0]['times']);times=[];votes=[];spread=[];last=-1
    if len(results)<2 or any(len(r['times'])!=count for r in results):
        raise ValueError('동일한 구절의 분석 결과 두 개 이상이 필요합니다.')
    for i in range(count):
        values=sorted(float(r['times'][i]) for r in results if r['times'][i] is not None)
        clusters=[[x for x in values if start<=x<=start+tolerance] for start in values]
        best=max(clusters,key=lambda xs:(len(xs),-(xs[-1]-xs[0])),default=[])
        stamp=round(median(best),3) if len(best)>=2 else None
        if stamp is not None and stamp<=last:stamp=None
        if stamp is not None:last=stamp
        times.append(stamp);votes.append(len(best));spread.append(round(best[-1]-best[0],3) if best else None)
    return {'times':times,'votes':votes,'spread':spread,'matched':sum(x is not None for x in times),
            'duration':results[0]['duration'],'method':'multi-context-agreement-v1'}
