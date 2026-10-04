"""In-memory progress metadata; no audio, lyrics, model work or background timers."""
import math
import time


PHASES = {'waiting':'재생 대기', 'recording':'소리 수집', 'queued':'분석 순서 대기',
          'analyzing':'음원 분석', 'partial':'부분 수집 종료', 'completed':'분석 완료',
          'error':'오류', 'cancelling':'취소 중', 'cancelled':'취소됨', 'discarded':'결과 제외',
          'unmatched':'분석 완료 · 일치 구절 부족'}
TERMINAL = {'partial','completed','error','cancelled','discarded','unmatched'}


def duration_text(seconds):
    seconds = max(0, math.ceil(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f'{hours}시간 {minutes}분' if hours else
            f'{minutes}분 {seconds:02}초' if minutes else f'{seconds}초')


class AudioProgress:
    def __init__(self, title, mode='자동', clock=time.monotonic, *, final_analysis=True):
        self.title, self.mode, self.clock = title, mode, clock
        self.final_analysis = bool(final_analysis)
        self.phase = 'waiting'
        self.text = 'Spotify에서 이 곡을 처음부터 재생하면 수집을 시작합니다.'
        self.phase_started = self.updated_at = clock()
        self.ended_at = None
        self.fraction = None
        self.completed = self.total = None
        self.baseline = None
        self.advances = 0
        self.cleanup_errors = []
        self.temporary_directory = ''
        self.runtime = {}

    def update(self, event, cancelled=False):
        now = self.clock()
        kind = event.get('kind')
        if isinstance(event.get('runtime'),dict):
            self.runtime={key:value for key,value in event['runtime'].items()
                          if key in ('device','model','compute_type','fallback','fallback_reason')
                          and isinstance(value,(str,bool))}
        if kind == 'finished':
            self.cleanup_errors = list(event.get('cleanup_errors',[]))
            self.temporary_directory = str(event.get('temporary_directory','')) if self.cleanup_errors else ''
            phase = 'cancelled' if cancelled else self.phase
            if phase not in TERMINAL:
                phase = 'error'
                self.text = '분석 결과 없이 작업이 종료됐습니다.'
        elif cancelled:
            phase = 'cancelling'
        elif kind == 'progress':
            phase = event.get('state','analyzing')
        elif kind == 'error':
            phase = 'error'
        elif kind == 'result':
            phase = 'completed'
            self.text = '분석이 끝났습니다.'
        else:
            return
        if phase != self.phase or event.get('restart'):
            self.phase = phase
            self.phase_started = now
            self.fraction = self.completed = self.total = self.baseline = None
            self.advances = 0
        if event.get('text'):
            self.text = event['text']
        if phase == 'cancelling':
            self.text = '임시 음원을 정리하고 있습니다.'
        elif phase == 'cancelled':
            self.text = '분석 취소됨 · 기존 시간표는 그대로입니다.'
        self.ended_at = now if phase in TERMINAL else None
        completed, total = event.get('completed'), event.get('total')
        if (kind == 'progress' and phase in ('recording','analyzing') and
                isinstance(completed,(int,float)) and isinstance(total,(int,float)) and
                math.isfinite(completed) and math.isfinite(total) and completed >= 0 and total > 0):
            fraction = min(1.,completed/total)
            if self.fraction is None or fraction > self.fraction:
                self.updated_at = now
                self.advances += 1
                if self.baseline is None:self.baseline = (now,fraction)
                self.fraction = fraction
                self.completed, self.total = min(completed,total), total

    def snapshot(self):
        now = self.clock()
        age = max(0,now-self.updated_at)
        elapsed = max(0,(self.ended_at if self.ended_at is not None else now)-self.phase_started)
        percent = self.fraction*100 if self.fraction is not None else None
        eta = None
        remaining = '—'
        if self.phase == 'recording':
            remaining = '계산 중'
            if self.completed is not None and age <= 5:
                eta = max(0,self.total-self.completed)
        elif self.phase == 'analyzing':
            remaining = '계산 중'
            if self.fraction == 1:
                remaining = '결과 정리 중'
            elif self.baseline and self.advances >= 3 and age <= 15:
                sample_seconds = self.updated_at-self.baseline[0]
                delta = self.fraction-self.baseline[1]
                if sample_seconds >= 3 and delta >= .05:
                    eta = (1-self.fraction)*sample_seconds/delta
        elif self.phase == 'queued':
            remaining = '순서 대기'
        elif self.phase == 'waiting':
            remaining = '재생 대기'
        if eta is not None:remaining = '약 '+duration_text(eta)
        detail = self.text
        if self.phase in ('waiting','recording'):
            detail += ('\n곡 전환·재생 중단·앱 종료 시 10초 이상 수집한 구간을 분석해 저장합니다.' if self.final_analysis else
                       '\n중간부터 임시 추적 중 · 전체 싱크 저장은 다음 처음 재생에서 진행합니다.')
        if self.phase in ('recording','analyzing'):
            detail += '\n현재 단계 경과 '+duration_text(elapsed)
            if self.fraction is not None:
                detail += ' · 진행 수치 갱신 '+duration_text(age)+' 전'
                if age > (5 if self.phase == 'recording' else 15):
                    detail += (' · 구간 정렬 처리 중 · 남은 시간 계산 중' if self.phase == 'analyzing' else
                               ' · 다음 처리 결과를 기다리는 중')
        elif self.phase == 'queued':
            detail += '\n대기 '+duration_text(elapsed)+' · 앞선 분석이 끝나면 시작합니다.'
        if self.cleanup_errors:
            if any(error.get('stage')=='temporary' for error in self.cleanup_errors):
                detail += '\n임시 파일 정리 실패 · 남은 임시 파일은 별도 정리가 필요합니다.'
                if self.temporary_directory:detail += '\n정리할 폴더: '+self.temporary_directory
            else:detail += '\n임시 자원 정리 경고 · 작업 결과와 별도로 정리 상태를 확인해 주세요.'
        return {'state':self.phase, 'phase':PHASES.get(self.phase,self.phase), 'percent':percent,
                'progress':f'{percent:.0f}%' if percent is not None else '—',
                'remaining':remaining, 'elapsed':elapsed, 'detail':detail,
                'ended_at':self.ended_at,'final_analysis':self.final_analysis,'runtime':dict(self.runtime)}
