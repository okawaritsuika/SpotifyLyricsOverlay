"""Lyrics arriving late must use the capture already started at the song boundary."""
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from audio_job import AudioJob,MODEL_FILES
from auto_audio import AutoAnalyzer
from lyrics import Line,Song
from media import Playback
from storage import Store
from sync import active_line,estimate_times
from tests.test_auto_audio import FakeJob


class PrefetchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.auto=AutoAnalyzer('.',Store(self.temp.name),queue.Queue(),job_factory=FakeJob)
        self.addCleanup(self.auto.close);self.auto.enabled=True
        self.song=Song('example','Example','Artist','Album',tuple(Line(str(i),'','') for i in range(10)))
        self.now=100.
        self.clock=patch('auto_audio.time.monotonic',side_effect=lambda:self.now)
        self.clock.start();self.addCleanup(self.clock.stop)

    def observe(self,position,song=None,**options):
        self.now=100.+position
        playback=Playback('Example','Artist','Album',position,100,True,self.now)
        self.auto.observe(song,playback,**options)
        return playback

    def test_missing_original_starts_capture_once_then_binds_same_job_after_eighteen_seconds(self):
        self.observe(0)
        job=next(iter(self.auto.jobs))
        for position in (1,2,12):self.observe(position,awaiting_sync=True)
        self.assertEqual(list(self.auto.jobs),[job]);self.assertFalse(self.auto.seen)
        self.assertTrue(job.waiting_for_lyrics);self.assertFalse(job.skip_final)
        self.auto.receive(job,{'kind':'progress','state':'recording','text':'12 / 100초','completed':12,'total':100},[])
        self.assertIn('가사 검색 중',self.auto.activity_snapshot()['active'][0]['detail'])
        self.assertTrue(self.auto.activity_snapshot()['active'][0]['awaiting_lyrics'])
        self.observe(18,self.song,memory_only=True)
        self.assertEqual(list(self.auto.jobs),[job]);self.assertIs(job.song,self.song)
        self.assertFalse(job.waiting_for_lyrics);self.assertFalse(job.skip_final)
        self.assertFalse(self.auto.activity_snapshot()['active'][0]['awaiting_lyrics'])
        self.assertIsNotNone(job.bound_start_index)
        self.assertEqual(self.auto.progress[job].snapshot()['percent'],12)
        self.assertEqual(job.analysis_key,(self.observe(19,self.song,memory_only=True).key,self.auto.signature(self.song,True)))

    def test_late_library_lyrics_also_attach_after_three_second_start_window(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.observe(18,self.song)
        self.assertIs(job.song,self.song);self.assertFalse(job.skip_final)

    def test_existing_capture_accepts_lyrics_even_in_the_last_ten_seconds(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.observe(96,self.song,memory_only=True)
        self.assertIs(job.song,self.song);self.assertFalse(job.skip_final)

    def test_reenabling_auto_capture_allows_the_same_track_to_prefetch_again(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.auto.set_enabled(False)
        self.auto.receive(old,{'kind':'finished'},[])
        with patch('auto_audio.ready'):self.auto.set_enabled(True)
        self.observe(1)
        self.assertEqual(len(self.auto.jobs),1)
        self.assertIsNot(next(iter(self.auto.jobs)),old)

    def test_provider_timing_discards_unbound_audio_without_consuming_an_analysis_attempt(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.observe(2,self.song,memory_only=True,external_sync=True)
        self.assertTrue(job.cancelled.is_set());self.assertFalse(self.auto.attempts)
        self.assertFalse(self.auto.seen)

    def test_seek_cannot_reuse_a_prefix_from_the_old_position(self):
        self.observe(0);old=next(iter(self.auto.jobs))
        self.now=101.
        self.auto.observe(None,Playback('Example','Artist','Album',40,100,True,self.now))
        current=list(self.auto.jobs)[-1]
        self.assertTrue(old.cancelled.is_set());self.assertIsNot(current,old)
        self.assertFalse(current.skip_final)

    def test_pause_and_track_change_discard_unbound_captures(self):
        self.observe(0);first=next(iter(self.auto.jobs))
        self.now=101.
        self.auto.observe(None,Playback('Example','Artist','Album',1,100,False,self.now))
        self.assertTrue(first.cancelled.is_set())
        self.observe(2);second=list(self.auto.jobs)[-1]
        self.auto.observe(None,Playback('Other','Artist','Album',0,100,True,self.now))
        self.assertTrue(second.cancelled.is_set())

    def test_failed_prefetch_does_not_spawn_a_worker_on_every_poll(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.auto.receive(job,{'kind':'error','text':'capture failed'},[])
        self.auto.receive(job,{'kind':'finished'},[])
        for position in (1,2,3):self.observe(position)
        self.assertFalse(self.auto.jobs)
        self.observe(4,self.song,memory_only=True)
        self.assertEqual(len(self.auto.jobs),1)

    def test_unbound_result_cannot_write_a_timeline(self):
        self.observe(0);job=next(iter(self.auto.jobs))
        self.assertIsNone(self.auto.receive(job,{'kind':'result','result':{'times':[1]}},[]))
        self.assertEqual(self.auto.progress[job].phase,'discarded')
        self.assertFalse((self.auto.store.directory/'audio_drafts').exists())

    def test_other_singers_complete_clock_only_seeds_live_search_and_keeps_captured_prefix(self):
        tracking=[float(i*2) for i in range(10)]
        self.auto.live_timing.save(self.song,{'times':[None]*10,'offset':20,'source':''})
        path=self.auto.live_timing.path(self.song);before=path.read_bytes()
        self.observe(0);job=next(iter(self.auto.jobs))
        self.observe(30,self.song,memory_only=True,tracking_times=tracking)
        self.assertEqual(job.bound_start_index,9)  # Hint offset is already applied.
        self.assertFalse(job.skip_final);self.assertFalse(job.waiting_for_lyrics)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(tracking,[float(i*2) for i in range(10)])
        self.assertEqual(self.auto.attempts[job.analysis_key],1)

    def test_unusable_tracking_clocks_fall_back_to_the_existing_live_seed(self):
        expected=active_line(estimate_times(self.song.lines,100,[None]*10),50)
        cases=(None,[],[0]*10,[None]*10,list(range(9)),list(reversed(range(10))),
               [-1]+list(range(1,10)),list(range(9))+[100],list(range(9))+[101],
               [False]+list(range(1,10)),list(range(9))+[float('nan')],
               list(range(9))+[float('inf')],['0']+list(range(1,10)))
        for hint in cases:
            with self.subTest(hint=hint):
                auto=AutoAnalyzer('.',self.auto.store,queue.Queue(),job_factory=FakeJob)
                auto.enabled=True
                try:
                    snapshot=Playback('Example','Artist','Album',50,100,True,self.now)
                    auto.observe(self.song,snapshot,memory_only=True,tracking_times=hint)
                    job=next(iter(auto.jobs))
                    self.assertEqual(job.options['start_index'],expected)
                    self.assertEqual(auto.timing(self.song,True)['times'],[None]*10)
                finally:auto.close()


class PrefetchProcessTests(unittest.TestCase):
    def test_real_child_processes_keep_prefix_and_pass_late_memory_lyrics_only_via_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);model=base/'models'/'whisper-small';model.mkdir(parents=True)
            for name in MODEL_FILES:(model/name).touch()
            (base/'audio_worker.py').write_text(
                "import json,sys,time\nfrom pathlib import Path\n"
                "path=Path(sys.argv[-1]);folder=path.parent;request=json.loads(path.read_text(encoding='utf-8'))\n"
                "if sys.argv[1] in ('--realtime','--analyze'):\n"
                "    assert 'lines' not in request and request['memory_lines']\n"
                "    assert json.loads(sys.stdin.read())==['synthetic original']\n"
                "    assert (folder/'capture.wav').read_bytes().startswith(b'early audio prefix')\n"
                "    if sys.argv[1]=='--realtime':\n"
                "        (folder/'live.ready').touch()\n"
                "        print(json.dumps({'kind':'realtime_status','state':'listening'}),flush=True)\n"
                "        time.sleep(30)\n"
                "    else:\n"
                "        assert (folder/'capture.wav').read_bytes()==b'early audio prefix and later audio'\n"
                "        assert request['start_index']==4\n"
                "        print(json.dumps({'kind':'result','result':{'times':[1]}}),flush=True)\n"
                "else:\n"
                "    assert request['defer_lyrics'] and 'lines' not in request and 'memory_lines' not in request\n"
                "    (folder/'capture.wav').write_bytes(b'early audio prefix')\n"
                "    print(json.dumps({'kind':'progress','state':'recording','text':'prefix saved'}),flush=True)\n"
                "    deadline=time.monotonic()+8\n"
                "    while not (folder/'live.ready').exists():\n"
                "        assert time.monotonic()<deadline\n"
                "        time.sleep(.01)\n"
                "    assert json.loads(path.read_text(encoding='utf-8'))==request\n"
                "    with (folder/'capture.wav').open('ab') as output:output.write(b' and later audio')\n"
                "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue();playback=Playback('Example',duration=100,playing=True,captured=time.monotonic())
            job=AudioJob(base,None,playback,messages,defer_lyrics=True)
            try:
                self.assertEqual(messages.get(timeout=8)[1][1]['text'],'prefix saved')
                self.assertIsNone(job.realtime_process)
                initial=job.request_path.read_bytes()
                song=Song('late','Example','','',(Line('synthetic original','',''),))
                self.assertTrue(job.bind_lyrics(song,memory_only=True,start_index=4))
                self.assertFalse(job.skip_final)
                job.thread.join(timeout=12)
                self.assertFalse(job.thread.is_alive())
                events=[]
                while not messages.empty():events.append(messages.get()[1][1])
                self.assertFalse(any(event['kind']=='error' for event in events),events)
                self.assertTrue(any(event['kind']=='result' for event in events),events)
                self.assertNotIn(b'synthetic original',initial)
                self.assertFalse(Path(job.temp.name).exists())
            finally:job.close()

    def test_track_ending_without_lyrics_runs_no_model_and_removes_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "assert not sys.argv[1].startswith('--')\n"
                "Path(sys.argv[1]).with_name('capture.wav').write_bytes(b'early audio')\n"
                "print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue();job=AudioJob(base,None,Playback(duration=100),messages,defer_lyrics=True)
            try:
                job.thread.join(timeout=8)
                self.assertFalse(job.thread.is_alive());self.assertFalse(Path(job.temp.name).exists())
                self.assertFalse(job.bind_lyrics(Song('s','s','','',(Line('x','',''),))))
                events=[]
                while not messages.empty():events.append(messages.get()[1][1])
                self.assertEqual([event['kind'] for event in events],['progress','finished'])
                self.assertEqual(events[0]['state'],'discarded')
                self.assertIsNone(job.realtime_process)
            finally:job.close()


if __name__=='__main__':unittest.main()
