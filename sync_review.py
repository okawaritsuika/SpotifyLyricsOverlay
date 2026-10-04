"""Audit the imported library or analyze a local audio file, entirely offline."""
import argparse
from pathlib import Path
from storage import Store,read_json,atomic_json
from timing_quality import timing_origins,compare_times


def audit_library(store):
    rows=[]
    for song in store.library():
        timing=store.timing(song);origins=timing_origins(timing)
        draft=read_json(store.directory/'audio_drafts'/(song.id+'.json'),{})
        if draft.get('fingerprint')!=song.fingerprint:draft={}
        manual=origins.count('manual');audio=origins.count('audio');total=len(song.lines)
        rows.append({'id':song.id,'title':song.title,'total':total,'manual_or_legacy':manual,
                     'audio':audio,'missing':total-manual-audio,'source':timing.get('source',''),
                     'candidate_count':sum(x is not None for x in draft.get('times',[])),
                     'method':draft.get('method',''),
                     'status':'수동 기록 보존' if manual else '음원 재검증 필요' if audio else '음원 분석 필요'})
    return {'songs':rows,'total_songs':len(rows),'note':'기록 유무와 정확도는 다릅니다. 음원 없이 정확성을 판정하지 않습니다.'}


def analyze_file(base,audio,song_id,output):
    # Audio is read in place, never uploaded, copied, or removed.
    audio=Path(audio).resolve(strict=True)
    if not audio.is_file():raise ValueError('음원 파일을 선택하세요.')
    store=Store(base/'data');song=next((s for s in store.library() if s.id==song_id or s.title==song_id),None)
    if song is None:raise ValueError('등록된 곡의 제목 또는 ID를 지정하세요.')
    from analysis_limits import apply_analysis_limits
    apply_analysis_limits()
    from faster_whisper.audio import decode_audio
    from audio_alignment import align_lyrics
    samples=decode_audio(str(audio),sampling_rate=16000);duration=len(samples)/16000
    if not 10<=duration<=900:raise ValueError('10초~15분 길이의 음원만 분석할 수 있습니다.')
    request={'model':str(base/'models/whisper-small'),'lines':[x.original for x in song.lines],'duration':duration}
    result=align_lyrics(request,samples,0,lambda current,total:None)
    current=store.timing(song)
    reference=[t+current.get('offset',0) if t is not None and origin=='manual' else None
               for t,origin in zip(current['times'],timing_origins(current))]
    report={'id':song.id,'fingerprint':song.fingerprint,'result':result,
            'reference_comparison':compare_times(reference,result['times']),
            'note':'수동 기준이 없는 구절의 정확도는 별도 확인이 필요합니다. 기존 시간표는 변경하지 않았습니다.'}
    atomic_json(output,report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--audio',type=Path)
    parser.add_argument('--song')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.audio:
        if not args.song:parser.error('--audio requires --song')
        report=analyze_file(args.base,args.audio,args.song,args.output or args.base/'data/sync_reviews/file-review.json')
        print('분석 후보:',report['result']['matched'],'기준 비교:',report['reference_comparison']['compared'])
    else:
        report=audit_library(Store(args.base/'data'))
        output=args.output or args.base/'data/sync_reviews/library-audit.json'
        atomic_json(output,report)
        print('점검한 곡:',report['total_songs'])


if __name__=='__main__':main()
