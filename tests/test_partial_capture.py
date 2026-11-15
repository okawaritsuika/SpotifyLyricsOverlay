from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from audio_capture_cache import CAPTURE_NAME, capture_frames, write_capture_audio
from audio_worker import CaptureDiscarded, analyze, capture, main, _error_diagnostic
from media import Playback


class PartialCaptureTests(unittest.TestCase):
    def run_capture(self, boundary=None, boundary_at=12.5, start=0, capture_from=0, duration=40, initial_mismatch=None,
                    capture_window=None, playlist_analysis=False):
        clock=[float(start)]
        initial=Playback('Test','Artist','Album',0,duration,True,0)
        reads=[]
        class Stream:
            rate=8000;channels=1
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def start(self):pass
            def read(self,timeout=None):
                at=clock[0];reads.append(at);clock[0]+=.25
                if boundary=='device_gap' and at>=boundary_at:at+=4
                return b'\0'*4000,2000,at
            def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/CAPTURE_NAME
            def snapshot(*args):
                now=clock[0];changed=now>=boundary_at
                if changed and boundary=='app_closed':path.with_name('finish-capture').touch()
                initial_other=initial_mismatch=='track' and now<.5
                initial_paused=initial_mismatch=='paused' and now<.5
                return Playback('Other' if initial_other or changed and boundary=='track_changed' else 'Test',
                    'Artist','Album',now+(7 if changed and boundary=='seeked' else 0),duration,
                    not (initial_paused or changed and boundary=='paused'),now-(4 if changed and boundary=='stale' else 0),
                    1.2 if changed and boundary=='rate' else 1)
            with patch('audio_worker.ProcessLoopback',Stream),patch('audio_worker.SpotifyMonitor'), \
                 patch('audio_worker.time.monotonic',side_effect=lambda:clock[0]), \
                 patch('audio_worker.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)), \
                 patch('audio_worker.latest',side_effect=snapshot),patch('audio_worker.emit'):
                result=capture({'duration':duration,'key':initial.key,'incremental':True,
                                'capture_from':capture_from,'capture_window':capture_window,
                                'playlist_analysis':playlist_analysis},path)
            frames,rate,_=capture_frames(path)
            return result,frames/rate,reads

    def test_playback_interruptions_keep_only_stable_prefix(self):
        for boundary in ('track_changed','paused','seeked','app_closed'):
            with self.subTest(boundary=boundary):
                result,seconds,reads=self.run_capture(boundary)
                self.assertEqual(result['capture_end_reason'],boundary)
                self.assertEqual(result['clip_tail'],1)
                self.assertEqual(seconds,12.5)
                self.assertEqual(result['captured_seconds'],11.5)
                self.assertEqual(result['capture_end'],11.5)
                self.assertLess(max(reads),12.5)

    def test_playlist_rejects_an_early_boundary_and_accepts_a_normal_track_end(self):
        with self.assertRaisesRegex(CaptureDiscarded,'곡 끝까지'):
            self.run_capture('track_changed',playlist_analysis=True)
        result, seconds, _ = self.run_capture('track_changed',boundary_at=39,playlist_analysis=True)
        self.assertEqual(seconds,39)
        self.assertEqual(result['capture_end'],38)

    def test_short_listens_are_discarded_without_failure(self):
        for boundary in ('track_changed','paused','seeked','app_closed'):
            for seconds in (3,10.75):
                with self.subTest(boundary=boundary,seconds=seconds):
                    with self.assertRaisesRegex(CaptureDiscarded,'짧게 재생'):
                        self.run_capture(boundary,boundary_at=seconds)

    def test_minimum_counts_audio_after_transition_safety_tail(self):
        result,seconds,_=self.run_capture('track_changed',boundary_at=11)
        self.assertEqual(seconds,11)
        self.assertEqual(result['captured_seconds'],10)

    def test_short_remaining_tail_is_discarded_at_natural_end(self):
        with self.assertRaises(CaptureDiscarded):
            self.run_capture(start=32)

    def test_mid_song_capture_reports_absolute_usable_end(self):
        result,_,_=self.run_capture('paused',start=14,boundary_at=27)
        self.assertEqual(result['start_position'],14)
        self.assertEqual(result['capture_end'],26)
        self.assertEqual(result['captured_seconds'],12)

    def test_resume_waits_until_uncovered_position_before_reading_audio(self):
        result,_,reads=self.run_capture(capture_from=12,duration=24)
        self.assertGreaterEqual(result['start_position'],12)
        self.assertLess(result['start_position'],12.1)
        self.assertGreaterEqual(min(reads),12)
        self.assertEqual(result['capture_end_reason'],'complete')
        self.assertAlmostEqual(result['capture_end'],24,places=3)

    def test_bounded_capture_stops_at_right_anchor_even_for_short_gaps(self):
        result,seconds,reads=self.run_capture(capture_window=[12,16])
        self.assertAlmostEqual(result['capture_end'],16,places=3)
        self.assertGreaterEqual(min(reads),12)
        self.assertLess(max(reads),16)
        self.assertAlmostEqual(seconds,4,delta=.06)
        with self.assertRaisesRegex(CaptureDiscarded,'시작 지점을 지나'):
            self.run_capture(start=14,capture_window=[12,16])

    def test_resume_wait_is_discarded_immediately_when_playback_changes(self):
        for boundary in ('track_changed','paused','app_closed'):
            with self.subTest(boundary=boundary):
                with self.assertRaisesRegex(CaptureDiscarded,'수집 전에'):
                    self.run_capture(boundary,boundary_at=3,capture_from=20)

    def test_new_monitor_allows_initial_transition_snapshot_to_settle(self):
        for mismatch in ('track','paused'):
            with self.subTest(mismatch=mismatch):
                result,_,reads=self.run_capture(initial_mismatch=mismatch)
                self.assertGreaterEqual(min(reads),.5)
                self.assertLess(min(reads),.6)
                self.assertEqual(result['capture_end_reason'],'complete')

    def test_new_monitor_does_not_wait_forever_for_an_already_skipped_track(self):
        with self.assertRaisesRegex(CaptureDiscarded,'수집 전에') as raised:
            self.run_capture('track_changed',boundary_at=0)
        self.assertIn('초기 연결 중',str(raised.exception))
        self.assertIn('대기 2.0초',str(raised.exception))

    def test_capture_wait_failure_explains_resume_boundary_without_track_text(self):
        with self.assertRaises(CaptureDiscarded) as raised:
            self.run_capture('paused',boundary_at=3,capture_from=20)
        detail=str(raised.exception)
        self.assertIn('목표 곡 확인 후',detail)
        self.assertIn('일시정지',detail)
        self.assertIn('수집 예정 20.0초',detail)
        self.assertNotIn('Test',detail)

    def test_resuming_far_into_long_track_does_not_hit_three_minute_wait(self):
        result,_,reads=self.run_capture(capture_from=200,duration=212)
        self.assertGreaterEqual(min(reads),200)
        self.assertEqual(result['capture_end_reason'],'complete')

    def test_connection_rate_and_audio_clock_errors_still_fail(self):
        for boundary,text in (('stale','연결이 끊겼'),('rate','재생 속도'),('device_gap','어긋나')):
            with self.subTest(boundary=boundary):
                with self.assertRaisesRegex(ValueError,text) as error:
                    self.run_capture(boundary)
                self.assertNotIsInstance(error.exception,CaptureDiscarded)

    def test_short_capture_is_rejected_before_importing_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/CAPTURE_NAME
            with write_capture_audio(path, rate=8000) as output:
                output.write(b'\0'*(8000*2*10))
            with patch('audio_worker.align_lyrics') as alignment, \
                 patch('analysis_limits.apply_analysis_limits') as limits:
                with self.assertRaises(CaptureDiscarded):
                    analyze({'incremental':True},path,0,1)
            alignment.assert_not_called();limits.assert_not_called()

    def test_worker_short_listen_emits_discarded_instead_of_error_and_removes_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            request=Path(directory)/'request.json'
            request.write_text('{"incremental":true}',encoding='utf-8')
            audio=request.with_name(CAPTURE_NAME);audio.write_bytes(b'temporary audio')
            with patch('audio_worker.sys.argv',['audio_worker.py',str(request)]), \
                 patch('audio_worker.capture',side_effect=CaptureDiscarded('short')), \
                 patch('audio_worker.emit') as emit:
                main()
            emit.assert_called_once_with('progress',state='discarded',text='short')
            self.assertFalse(audio.exists())

    def test_worker_failure_diagnostic_has_no_exception_text_or_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            request=Path(directory)/'request.json'
            request.write_text('{"incremental":true}',encoding='utf-8')
            secret='private Japanese lyrics / user-secret-directory'
            with patch('audio_worker.sys.argv',['audio_worker.py',str(request)]), \
                 patch('audio_worker.capture',side_effect=RuntimeError(secret)),patch('audio_worker.emit') as emit:
                main()
            event=emit.call_args.kwargs
            self.assertEqual(emit.call_args.args,('error',))
            self.assertEqual(event['diagnostic']['stage'],'capture')
            self.assertEqual(event['diagnostic']['error_type'],'RuntimeError')
            self.assertTrue(event['diagnostic']['frames'])
            self.assertNotIn(secret,json.dumps(event,ensure_ascii=False))
            self.assertNotIn(directory,json.dumps(event,ensure_ascii=False))

    def test_worker_diagnostic_keeps_only_last_six_structural_frames(self):
        namespace={}
        exec(compile("def recurse(n):\n    if n: return recurse(n-1)\n    raise ValueError('private original text')\n",
                     'C:/private-user-name/private-location/audio_worker.py','exec'),namespace)
        try:namespace['recurse'](9)
        except ValueError as error:diagnostic=_error_diagnostic(error,True)
        self.assertEqual(diagnostic['stage'],'analyze')
        self.assertEqual(len(diagnostic['frames']),6)
        self.assertTrue(all(set(frame)=={'module','function','line'} for frame in diagnostic['frames']))
        self.assertTrue(all(frame['module']=='audio_worker' for frame in diagnostic['frames']))
        self.assertNotIn('private',json.dumps(diagnostic))

    def test_analysis_keeps_interval_metadata_and_trims_transition_tail(self):
        expected={'matched':1,'times':[2],'incremental':True,'analyzed_ranges':[[0,11]]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/CAPTURE_NAME
            with write_capture_audio(path, rate=8000) as output:
                output.write(b'\0\x20'*(8000*12))
            with patch('analysis_limits.apply_analysis_limits'),patch('audio_worker.emit'), \
                 patch('audio_worker.align_lyrics',return_value=expected.copy()) as alignment:
                result=analyze({'incremental':True,'duration':40},path,0,1)
            self.assertEqual(len(alignment.call_args.args[1]),11*16000)
            self.assertEqual(result['analyzed_ranges'],[[0,11]])
            self.assertTrue(result['incremental'])
            self.assertTrue(result['partial'])
            self.assertEqual(result['captured_seconds'],11)


if __name__=='__main__':unittest.main()
