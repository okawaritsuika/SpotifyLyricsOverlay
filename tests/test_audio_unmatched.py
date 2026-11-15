import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from audio_alignment import AlignmentUnmatched
from audio_capture_cache import CAPTURE_NAME
from audio_job import AudioJob
from audio_progress import AudioProgress
from audio_worker import main
from auto_audio import AutoAnalyzer
from lyrics import Line,Song
from media import Playback
from storage import Store
from test_auto_audio import FakeJob


class UnmatchedAudioTests(unittest.TestCase):
    def test_no_match_is_a_completed_attempt_and_deletes_temporary_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'request.json';path.write_text('{}',encoding='utf-8')
            path.with_name('capture-info.json').write_text('{"start_position":0}',encoding='utf-8')
            path.with_name(CAPTURE_NAME).write_bytes(b'temporary')
            with patch('audio_worker.sys.argv',['audio_worker.py','--analyze',str(path)]), \
                 patch('audio_worker.analyze',side_effect=AlignmentUnmatched('일치 구절 부족')), \
                 patch('audio_worker.emit') as emit:
                main()
            emit.assert_called_once_with('progress',state='unmatched',text='일치 구절 부족')
            self.assertFalse(path.with_name(CAPTURE_NAME).exists())

    def test_worker_completion_does_not_replace_unmatched_with_process_error(self):
        song=Song('test','Example','Artist','Album',(Line('例文です','',''),))
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys\n"
                "event={'kind':'progress','state':'unmatched','text':'No confident match'} if sys.argv[1]=='--analyze' else {'kind':'captured'}\n"
                "print(json.dumps(event),flush=True)\n",encoding='utf-8')
            messages=queue.Queue();job=AudioJob(base,song,Playback(duration=100),messages)
            try:
                job.thread.join(timeout=10);self.assertFalse(job.thread.is_alive())
                progress=AudioProgress(song.title);events=[]
                while not messages.empty():
                    event=messages.get()[1][1];events.append(event);progress.update(event)
                self.assertFalse(any(event['kind']=='error' for event in events))
                self.assertEqual(progress.snapshot()['state'],'unmatched')
                self.assertEqual(progress.snapshot()['phase'],'분석 완료 · 일치 구절 부족')
                self.assertFalse(Path(job.temp.name).exists())
            finally:job.close()

    def test_auto_no_match_preserves_timing_and_retries_only_on_next_play(self):
        song=Song('test','Example','Artist','Album',(Line('例文です','',''),))
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);auto=AutoAnalyzer('.',store,queue.Queue(),job_factory=FakeJob)
            auto.enabled=True
            playback=Playback(song.title,song.artist,song.album,0,100,True,time.monotonic())
            try:
                auto.observe(song,playback);job=next(iter(auto.jobs))
                timing_path=store.directory/'timings'/'test.json'
                store.save_timing(song,{'times':[15],'origins':['manual'],'offset':0})
                before=timing_path.read_bytes()
                auto.receive(job,{'kind':'progress','state':'unmatched','text':'일치 구절 부족'},[song])
                auto.receive(job,{'kind':'finished'},[song])
                self.assertEqual(auto.last_progress.phase,'unmatched')
                self.assertEqual(timing_path.read_bytes(),before)
                self.assertFalse(auto.draft_path(song).exists())
                self.assertIn(job.analysis_key,auto.retryable)
                self.assertEqual(auto.attempts[job.analysis_key],1)
                auto.observe(song,playback)
                self.assertFalse(auto.jobs)
            finally:auto.close()


if __name__=='__main__':unittest.main()
