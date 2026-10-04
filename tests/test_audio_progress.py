import math
import unittest
from audio_progress import AudioProgress, duration_text


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.now=0.
        self.progress=AudioProgress('Example',clock=lambda:self.now)

    def update(self,phase,completed=None,total=None):
        self.progress.update({'kind':'progress','state':phase,'text':phase,
                              'completed':completed,'total':total})

    def test_capture_remaining_uses_captured_duration_not_whole_song(self):
        self.update('recording',20,80)
        state=self.progress.snapshot()
        self.assertEqual(state['percent'],25)
        self.assertEqual(state['remaining'],'약 1분 00초')
        self.now=4
        self.assertEqual(self.progress.snapshot()['percent'],25)
        self.now=6
        self.assertEqual(self.progress.snapshot()['remaining'],'계산 중')
        self.assertIn('다음 처리 결과',self.progress.snapshot()['detail'])

    def test_loading_and_queue_never_invent_a_remaining_time(self):
        self.update('queued');self.now=300
        state=self.progress.snapshot()
        self.assertIsNone(state['percent']);self.assertEqual(state['remaining'],'순서 대기')
        self.assertIn('5분',state['detail'])
        self.update('analyzing');self.now=350
        self.assertEqual(self.progress.snapshot()['remaining'],'계산 중')
        self.assertEqual(self.progress.snapshot()['elapsed'],50)

    def test_eta_needs_real_advances_and_excludes_model_load_time(self):
        self.update('analyzing');self.now=100
        self.update('analyzing',10,100);self.now=110
        self.update('analyzing',20,100)
        self.assertEqual(self.progress.snapshot()['remaining'],'계산 중')
        self.now=120;self.update('analyzing',30,100)
        self.assertEqual(self.progress.snapshot()['remaining'],'약 1분 10초')
        self.now=140
        self.assertEqual(self.progress.snapshot()['remaining'],'계산 중')
        self.assertEqual(self.progress.snapshot()['percent'],30)

    def test_unchanged_callbacks_do_not_make_fake_rate_or_freshness(self):
        self.update('analyzing',20,100)
        for moment in (5,10,20):
            self.now=moment;self.update('analyzing',20,100)
        state=self.progress.snapshot()
        self.assertEqual(state['remaining'],'계산 중')
        self.assertIn('20초 전',state['detail'])

    def test_phase_transition_resets_percent_and_elapsed(self):
        self.update('recording',99,100);self.now=15
        self.update('queued');self.now=20
        self.assertIsNone(self.progress.snapshot()['percent'])
        self.assertEqual(self.progress.snapshot()['elapsed'],5)
        self.update('analyzing',1,100)
        self.assertEqual(self.progress.snapshot()['percent'],1)

    def test_regressive_or_invalid_numbers_are_not_displayed(self):
        self.update('analyzing',30,100)
        for completed,total in [(20,100),(math.nan,100),(math.inf,100),(1,0),(-1,100),(None,100)]:
            self.now+=1;self.update('analyzing',completed,total)
            self.assertEqual(self.progress.snapshot()['percent'],30)

    def test_hundred_percent_still_waits_for_result(self):
        self.update('analyzing',100,100)
        self.assertEqual(self.progress.snapshot()['remaining'],'결과 정리 중')
        self.assertEqual(self.progress.snapshot()['phase'],'음원 분석')
        self.progress.update({'kind':'result'})
        self.progress.text='3구절 저장'
        self.progress.update({'kind':'finished'})
        self.assertEqual(self.progress.snapshot()['phase'],'분석 완료')
        self.assertEqual(self.progress.snapshot()['detail'],'3구절 저장')

    def test_error_partial_and_cancel_survive_finish(self):
        for event,phase in [({'kind':'error','text':'원인'},'오류'),
                            ({'kind':'progress','state':'partial','text':'중간 시작'},'부분 수집 종료')]:
            with self.subTest(phase=phase):
                self.progress.update(event)
                self.progress.update({'kind':'finished'})
                self.assertEqual(self.progress.snapshot()['phase'],phase)
                self.assertEqual(self.progress.snapshot()['detail'],event['text'])
        self.progress.update({'kind':'progress'},cancelled=True)
        self.assertEqual(self.progress.snapshot()['phase'],'취소 중')
        self.progress.update({'kind':'finished'},cancelled=True)
        self.assertEqual(self.progress.snapshot()['phase'],'취소됨')

    def test_unexpected_finish_is_not_success(self):
        self.progress.update({'kind':'finished'})
        self.assertEqual(self.progress.snapshot()['phase'],'오류')

    def test_terminal_timestamp_can_order_recent_results_without_advancing_in_snapshot(self):
        self.assertIsNone(self.progress.snapshot()['ended_at'])
        self.now=10;self.progress.update({'kind':'result'})
        self.now=11;self.progress.update({'kind':'finished'})
        self.now=50
        self.assertEqual(self.progress.snapshot()['ended_at'],11)

    def test_capture_detail_distinguishes_full_analysis_from_provisional_following(self):
        self.update('recording',20,80)
        self.assertTrue(self.progress.snapshot()['final_analysis'])
        self.assertIn('앱 종료 시 10초 이상',self.progress.snapshot()['detail'])
        partial=AudioProgress('Middle',clock=lambda:self.now,final_analysis=False)
        partial.update({'kind':'progress','state':'recording','text':'수집'})
        self.assertFalse(partial.snapshot()['final_analysis'])
        self.assertIn('다음 처음 재생',partial.snapshot()['detail'])

    def test_stale_alignment_remains_analyzing_without_claiming_queue_or_eta(self):
        self.update('analyzing',0,100)
        self.now=180
        state=self.progress.snapshot()
        self.assertEqual(state['state'],'analyzing')
        self.assertEqual(state['remaining'],'계산 중')
        self.assertIn('구간 정렬 처리 중',state['detail'])
        self.assertNotIn('순서 대기',state['detail'])

    def test_cpu_fallback_resets_observed_progress_and_retains_only_runtime_metadata(self):
        self.update('analyzing',50,100);self.now=10
        self.progress.update({'kind':'progress','state':'analyzing','restart':True,
            'runtime':{'device':'cpu','model':'whisper-small','fallback':True,'lyrics':'private'}})
        state=self.progress.snapshot()
        self.assertIsNone(state['percent']);self.assertEqual(state['elapsed'],0)
        self.assertEqual(state['runtime'],{'device':'cpu','model':'whisper-small','fallback':True})
        self.assertNotIn('private',str(state))

    def test_time_format_handles_long_low_cpu_jobs(self):
        self.assertEqual(duration_text(3700),'1시간 1분')
        self.assertEqual(duration_text(.1),'1초')


if __name__=='__main__':unittest.main()
