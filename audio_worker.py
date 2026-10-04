"""Explicit, finite Windows output capture followed by offline Japanese ASR."""
import json
import math
from pathlib import Path
import queue
import sys
import time
import wave

from audio_sync import capture_problem
from audio_alignment import AlignmentUnmatched, align_lyrics
from media import Playback, SpotifyMonitor
from realtime_audio import LiveWindowWriter
from process_audio import BufferedProcessLoopback as ProcessLoopback

MODEL_REVISION = '536b0662742c02347bc0e980a01041f333bce120'
MIN_PARTIAL_SECONDS = 10.0


class CaptureDiscarded(ValueError):
    """An ordinary short listen is not an analysis failure."""


def _error_diagnostic(error, analyzing):
    frames=[]
    traceback=error.__traceback__
    while traceback is not None:
        code=traceback.tb_frame.f_code
        frames.append({'module':Path(code.co_filename).stem,'function':code.co_name,'line':traceback.tb_lineno})
        traceback=traceback.tb_next
    return {'stage':'analyze' if analyzing else 'capture','error_type':type(error).__name__,'frames':frames[-6:]}


def emit(kind, **value):
    print(json.dumps({'kind':kind, **value}, ensure_ascii=False), flush=True)


def install_model(destination):
    from huggingface_hub import snapshot_download
    snapshot_download('Systran/faster-whisper-small', revision=MODEL_REVISION,
                      local_dir=str(destination), token=False, allow_patterns=[
                          'model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'])


def latest(messages, snapshot):
    while True:
        try:
            kind, value = messages.get_nowait()
        except queue.Empty:
            return snapshot
        if kind == 'media':
            snapshot = value


def capture(request, path):
    duration = float(request['duration'])
    incremental = bool(request.get('incremental'))
    capture_from = float(request.get('capture_from',0)) if incremental else 0.0
    start_limit = min(10, max(0, float(request.get('max_start_position',3))))
    if request.get('allow_partial') or incremental:start_limit=duration
    if not math.isfinite(duration) or not 10 <= duration <= 900:
        raise ValueError('10초~15분 길이의 곡만 분석할 수 있습니다.')
    if not math.isfinite(capture_from) or not 0 <= capture_from <= duration:
        raise ValueError('이어서 수집할 재생 위치를 확인하세요.')
    messages = queue.Queue()
    monitor = SpotifyMonitor(messages)
    snapshot = Playback()
    monitor.start()
    try:
        with ProcessLoopback() as stream:
            channels, rate = stream.channels, stream.rate
            emit('progress', text=f'저장한 구간 이후 대기 · {capture_from:.0f}초부터 수집합니다.' if capture_from else
                 '가사 검색 중 · 현재 재생음을 먼저 수집합니다.' if request.get('defer_lyrics') else
                 '현재 재생음 연결 중 · 중간부터 임시 추적' if request.get('allow_partial') else
                 '분석 대기 · 선택한 곡을 Spotify에서 처음부터 재생하세요. (3분 안에)', state='waiting')
            deadline = time.monotonic()+max(180,capture_from+30)
            while True:
                if incremental and path.with_name('finish-capture').exists():
                    raise CaptureDiscarded('수집 전에 앱을 닫아 분석 대기를 정리했습니다.')
                snapshot = latest(messages, snapshot)
                now = time.monotonic()
                if (incremental and snapshot.has_timeline and now-snapshot.captured < 2
                        and (snapshot.key != request['key'] or not snapshot.playing)):
                    raise CaptureDiscarded('수집 전에 재생이 바뀌어 분석 대기를 정리했습니다.')
                if (snapshot.key == request['key'] and snapshot.has_timeline and snapshot.playing
                        and now-snapshot.captured < 2 and capture_from <= snapshot.position(now) <= start_limit):
                    if abs(snapshot.duration-duration) > 2:
                        raise ValueError('곡 길이가 바뀌었습니다. 현재 곡으로 분석을 다시 시작하세요.')
                    break
                if now >= deadline:
                    raise ValueError('대기 시간이 지났습니다. 분석 대기를 다시 눌러 주세요.')
                time.sleep(.05)
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(channels)
                output.setsampwidth(2)
                output.setframerate(rate)
                live=LiveWindowWriter(path.parent,rate,channels,window_seconds=request.get('live_window_seconds',3.0)) if request.get('realtime') else None
                try:
                    started = time.monotonic()
                    start_position = snapshot.position(started)
                    sample_clock_start = None
                    initial_snapshot=snapshot
                    stream.start()
                    total = 0
                    clip_tail = 0.0
                    capture_end_reason = 'complete'
                    next_report = 0
                    target = int((duration-start_position)*rate)
                    while total < target:
                        now = time.monotonic()
                        snapshot = latest(messages, snapshot)
                        expected = start_position+(now-started)
                        problem = capture_problem(snapshot, request['key'], min(duration, expected), now)
                        # A genuine playback boundary leaves a usable prefix.
                        # Stale media, speed changes and device clock errors do
                        # not establish where the recorded song actually ends.
                        if (incremental and problem and snapshot.has_timeline and now-snapshot.captured <= 3
                                and abs(snapshot.rate-1) <= .01):
                            if snapshot.key != request['key']:capture_end_reason='track_changed'
                            elif not snapshot.playing:capture_end_reason='paused'
                            elif abs(snapshot.position(now)-min(duration,expected)) > 1.5:capture_end_reason='seeked'
                            if capture_end_reason != 'complete':
                                clip_tail=1.0
                                break
                        if incremental and path.with_name('finish-capture').exists() and not problem:
                            capture_end_reason='app_closed'
                            clip_tail=1.0
                            break
                        # Spotify can publish the next title before its old end
                        # time (gapless/crossfade). Keep the consistent prefix;
                        # remove the last second to exclude the poll-boundary mix.
                        if snapshot.key != request['key'] and total >= rate*10:
                            clip_tail = 1.0
                            break
                        # At gapless transition the position can reset before
                        # the new title arrives. Preserve the old track prefix.
                        if (total>=rate*10 and expected>=duration-5 and snapshot.has_timeline
                                and snapshot.position(now)<3):
                            clip_tail=1.0
                            break
                        if expected >= duration-.8 and not snapshot.playing:
                            break
                        if problem:
                            raise ValueError(problem)
                        packet = stream.read(timeout=.25)
                        now = time.monotonic()
                        if packet is None:
                            if now-started-total/rate > 3:
                                raise ValueError('Spotify에서 소리가 수집되지 않습니다. Spotify 앱의 재생을 확인하세요.')
                            continue
                        data, frames, frame_at = packet
                        if sample_clock_start is None and frame_at is not None:
                            # Stream startup and callback delivery are not the
                            # time of the first recorded sample. Use the device
                            # clock, mapped to the media clock before startup.
                            # The first reliable timestamp can arrive after
                            # packets whose timestamps were marked invalid.
                            sample_start=frame_at-total/rate
                            position=initial_snapshot.base+(sample_start-initial_snapshot.captured)*initial_snapshot.rate
                            if total and position < -1/rate:
                                raise ValueError('오디오 시작 시각을 확인할 수 없어 분석을 취소했습니다.')
                            trim=min(frames,max(0,math.ceil(-position*rate))) if not total else 0
                            data=data[trim*channels*2:];frames-=trim
                            if not frames:continue
                            frame_at+=trim/rate
                            started=frame_at-total/rate
                            sample_clock_start=started
                            start_position=max(0,position+trim/rate)
                            target=max(0,int((duration-start_position)*rate))
                        remaining = max(0,min(frames, target-total))
                        output.writeframesraw(data[:remaining*channels*2])
                        total += remaining
                        if live:live.feed(data[:remaining*channels*2],remaining,total,start_position,started)
                        # Slow preview/disk writes can leave valid WASAPI
                        # packets queued. Check the captured sample clock,
                        # rather than treating delayed delivery as lost audio.
                        # A missing/invalid device timestamp says nothing about
                        # packet loss. Wall-clock delivery can lag valid queued
                        # samples; WASAPI separately rejects discontinuities.
                        if (frame_at is not None and
                                abs((frame_at+remaining/rate-sample_clock_start)-total/rate) > 3):
                            raise ValueError('오디오와 재생 시간이 어긋나 분석을 취소했습니다.')
                        if now >= next_report:
                            emit('progress', text=f'Spotify 소리만 수집 중 · {min(duration, start_position+total/rate):.0f} / {duration:.0f}초',
                                 state='recording', completed=total/rate, total=max(0,duration-start_position))
                            next_report = now+1
                finally:stream.close()
                usable_seconds=max(0,total/rate-clip_tail)
                if incremental and usable_seconds < MIN_PARTIAL_SECONDS:
                    raise CaptureDiscarded('짧게 재생한 곡은 제외했습니다. 수집한 구간이 10초 이상이면 분석합니다.')
                if not clip_tail and total/rate < duration-start_position-2:
                    raise ValueError('곡 전체의 재생음을 수집하지 못했습니다.')
            result={'start_position':start_position, 'clip_tail':clip_tail,'capture_source':'spotify-process'}
            if incremental:
                result.update(captured_seconds=round(usable_seconds,3),capture_end=round(start_position+usable_seconds,3),
                              capture_end_reason=capture_end_reason)
            return result
    finally:
        monitor.stop()


def analyze(request, path, start_position, clip_tail=0.0):
    if request.get('incremental'):
        # Inspect only the WAV header before importing or loading any AI model.
        with wave.open(str(path),'rb') as captured:
            usable=captured.getnframes()/captured.getframerate()-clip_tail
        if usable < MIN_PARTIAL_SECONDS:
            raise CaptureDiscarded('짧게 재생한 곡은 제외했습니다. 수집한 구간이 10초 이상이면 분석합니다.')
    from analysis_limits import apply_analysis_limits
    accelerated=request.get('final_device',request.get('realtime_device','cpu')) in ('auto','cuda')
    if accelerated:apply_analysis_limits(cpu_percent=20,memory_mb=4096,gpu=True)
    else:apply_analysis_limits(cpu_percent=request.get('final_cpu_percent',3))
    import numpy as np
    from faster_whisper.audio import decode_audio
    emit('progress', text='전체 음원 분석 준비 · GPU 정렬 모델을 불러옵니다.' if accelerated else
         f'전체 음원 분석 · CPU 최대 {request.get("final_cpu_percent",3)}%, 1스레드 · 완료 후 메모리를 반환합니다.', state='analyzing')
    samples = decode_audio(str(path), sampling_rate=16000)
    if clip_tail:
        samples = samples[:max(0,len(samples)-int(clip_tail*16000))]
    if not len(samples) or float(np.sqrt(np.mean(samples*samples))) < .0001:
        raise ValueError('수집한 소리가 무음입니다. Spotify와 Windows 출력 장치를 확인하세요.')
    runtime={}
    def runtime_status(kind,**event):
        if event.get('runtime'):runtime.update(event['runtime'])
        emit(kind,**event)
    def progress(current,total):
        device='GPU' if runtime.get('device')=='cuda' else runtime.get('device','').upper()
        emit('progress',text=f'{device+" · " if device else ""}일본어 가사를 음원에 정렬 중 · {min(100,current/max(total,.01)*100):.0f}%',
             state='analyzing',completed=float(current),total=float(total),runtime=dict(runtime))
    result = align_lyrics(request,samples,start_position,progress,emit=runtime_status)
    if runtime.get('model'):
        result['model']='faster-'+runtime['model']
        result['runtime']=dict(runtime)
    result['captured_seconds'] = round(len(samples)/16000,3)
    result['partial'] = bool(result.get('partial') or clip_tail or start_position>3 or
        request.get('incremental') and start_position+len(samples)/16000 < float(request['duration'])-2)
    if not result['matched']:
        raise AlignmentUnmatched('음원 분석은 끝났지만 확실하게 일치하는 구절을 찾지 못했습니다. 기존 시간표를 유지합니다.')
    return result


def load_request(path):
    request=json.loads(path.read_text(encoding='utf-8'))
    if request.get('memory_lines'):
        lines=json.loads(sys.stdin.read(1000001))
        if not isinstance(lines,list) or not lines or not all(isinstance(line,str) and len(line)<=10000 for line in lines):
            raise ValueError('메모리 가사 전달 형식을 확인하세요.')
        request['lines']=lines
    return request


def main():
    if sys.argv[1]=='--realtime':
        from realtime_audio import run
        request_path=Path(sys.argv[2])
        try:run(load_request(request_path),request_path.parent,emit)
        except Exception as error:
            emit('realtime_status',state='failed',text=f'실시간 음원 대조 실패 ({type(error).__name__}) · 종료 후 전체 분석은 계속합니다.')
        return
    if sys.argv[1] == '--install-model':
        install_model(Path(sys.argv[2]))
        return
    analyzing = sys.argv[1] == '--analyze'
    request_path = Path(sys.argv[2] if analyzing else sys.argv[1])
    path = request_path.parent / 'capture.wav'
    captured = False
    try:
        request = load_request(request_path)
        if analyzing:
            capture_info = json.loads(request_path.with_name('capture-info.json').read_text(encoding='utf-8'))
            result = analyze(request, path, capture_info['start_position'], capture_info.get('clip_tail',0))
            emit('result', result=result)
        else:
            capture_info = capture(request, path)
            request_path.with_name('capture-info.json').write_text(json.dumps(capture_info),encoding='utf-8')
            captured = True
            emit('captured')
    except CaptureDiscarded as error:
        emit('progress',state='discarded',text=str(error))
    except AlignmentUnmatched as error:
        emit('progress',state='unmatched',text=str(error))
    except Exception as error:
        # No audio, transcribed lyrics, private paths or stack traces are logged.
        detail = str(error) if isinstance(error, ValueError) else f'Spotify 소리 연결 또는 분석 모듈 오류 ({type(error).__name__}). Spotify 앱과 분석 설치를 확인하세요.'
        emit('error', text=detail,diagnostic=_error_diagnostic(error,analyzing))
    finally:
        if not captured:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
