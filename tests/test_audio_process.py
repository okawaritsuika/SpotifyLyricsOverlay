from pathlib import Path
import queue
import tempfile
import unittest

from audio_job import AudioJob
from lyrics import Line, Song
from media import Playback


class AudioProcessTests(unittest.TestCase):
    def test_final_turbo_is_selected_only_when_gpu_allowed_and_local_files_complete(self):
        from unittest.mock import Mock,patch
        from audio_job import MODEL_FILES,FINAL_GPU_MODEL_FILES
        song=Song('runtime','Example','Artist','Album',(Line('synthetic','',''),))
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for name,files in (('whisper-small',MODEL_FILES),('whisper-medium',MODEL_FILES),
                               ('whisper-turbo',FINAL_GPU_MODEL_FILES)):
                folder=base/'models'/name;folder.mkdir(parents=True)
                for filename in files:(folder/filename).touch()
            for device in ('cpu','auto','cuda'):
                with self.subTest(device=device),patch.object(AudioJob,'_launch',return_value=Mock()), \
                     patch('audio_job.threading.Thread'):
                    job=AudioJob(base,song,Playback(duration=100),queue.Queue(),realtime_device=device)
                    try:
                        self.assertEqual(job.request['model'],str(base/'models'/'whisper-small'))
                        if device=='cpu':self.assertNotIn('final_model',job.request)
                        else:
                            self.assertEqual(job.request['final_model'],str(base/'models'/'whisper-turbo'))
                            self.assertEqual(job.request['realtime_model'],str(base/'models'/'whisper-medium'))
                    finally:job.temp.cleanup()
            for missing in FINAL_GPU_MODEL_FILES:
                path=base/'models'/'whisper-turbo'/missing;path.unlink()
                with self.subTest(missing=missing),patch.object(AudioJob,'_launch',return_value=Mock()), \
                     patch('audio_job.threading.Thread'):
                    job=AudioJob(base,song,Playback(duration=100),queue.Queue(),realtime_device='auto')
                    try:self.assertNotIn('final_model',job.request)
                    finally:job.temp.cleanup()
                path.touch()

    def test_gpu_preference_reaches_both_workers_and_legacy_cpu_budget_is_preserved(self):
        import json
        from unittest.mock import Mock,patch
        from audio_job import MODEL_FILES
        song=Song('runtime','Example','Artist','Album',(Line('synthetic','',''),))
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);model=base/'models'/'whisper-small';model.mkdir(parents=True)
            for name in MODEL_FILES:(model/name).touch()
            for device in ('cpu','auto','cuda'):
                with self.subTest(device=device),patch.object(AudioJob,'_launch',return_value=Mock()), \
                     patch('audio_job.threading.Thread'):
                    job=AudioJob(base,song,Playback(duration=100),queue.Queue(),realtime_device=device)
                    try:
                        request=json.loads(job.request_path.read_text(encoding='utf-8'))
                        if device=='cpu':
                            self.assertNotIn('final_device',request)
                            self.assertEqual(request['final_cpu_percent'],1)
                        else:
                            self.assertEqual(request['realtime_device'],device)
                            self.assertEqual(request['final_device'],device)
                            self.assertEqual(request['final_cpu_percent'],20)
                        self.assertEqual(request['model'],str(model))
                    finally:job.temp.cleanup()

    def test_memory_lyrics_use_stdin_in_every_phase_and_mid_song_skips_final_analysis(self):
        import hashlib,json
        from audio_job import MODEL_FILES
        song=Song('memory','Example','Artist','Album',(Line('例文です','',''),))
        expected=hashlib.sha256(json.dumps([x.original for x in song.lines],ensure_ascii=False).encode()).hexdigest()
        for start_index in (None,0):
            with self.subTest(start_index=start_index),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);model=base/'models'/'whisper-small';model.mkdir(parents=True)
                for name in MODEL_FILES:(model/name).touch()
                (base/'audio_worker.py').write_text(
                    "import hashlib,json,sys,time\nfrom pathlib import Path\n"
                    "path=Path(sys.argv[-1]);request=json.loads(path.read_text(encoding='utf-8'))\n"
                    "assert 'lines' not in request and request['memory_lines']\n"
                    f"assert hashlib.sha256(sys.stdin.read().encode()).hexdigest()=={expected!r}\n"
                    "if sys.argv[1]=='--realtime':\n"
                    "    print(json.dumps({'kind':'realtime_status','state':'listening'}),flush=True)\n"
                    "    path.with_name('live.ready').touch();time.sleep(30)\n"
                    "elif sys.argv[1]=='--analyze':\n"
                    "    assert not request.get('allow_partial')\n"
                    "    print(json.dumps({'kind':'result','result':{'times':[1]}}),flush=True)\n"
                    "else:\n"
                    "    deadline=time.monotonic()+8\n"
                    "    while not path.with_name('live.ready').exists():\n"
                    "        assert time.monotonic()<deadline\n"
                    "        time.sleep(.01)\n"
                    "    time.sleep(.1)\n"
                    "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
                messages=queue.Queue()
                job=AudioJob(base,song,Playback(duration=100),messages,memory_only=True,start_index=start_index)
                try:
                    job.thread.join(timeout=12)
                    self.assertFalse(job.thread.is_alive())
                    events=[]
                    while not messages.empty():events.append(messages.get()[1][1])
                    kinds=[event['kind'] for event in events]
                    self.assertIn('realtime_status',kinds);self.assertNotIn('error',kinds)
                    self.assertEqual(kinds.count('result'),1 if start_index is None else 0)
                    if start_index is not None:self.assertTrue(any(event.get('state')=='partial' for event in events))
                    self.assertFalse(Path(job.temp.name).exists())
                finally:job.close()

    def test_live_results_arrive_during_capture_then_final_analysis_runs(self):
        from audio_job import MODEL_FILES
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);model=base/'models'/'whisper-small';model.mkdir(parents=True)
            for name in MODEL_FILES:(model/name).touch()
            (base/'audio_worker.py').write_text(
                "import json,sys,time,msvcrt\nfrom pathlib import Path\n"
                "folder=Path(sys.argv[-1]).parent\n"
                "if sys.argv[1]=='--realtime':\n"
                "    lock=(folder/'live.lock').open('w+b');lock.write(b'x');lock.flush();lock.seek(0)\n"
                "    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)\n"
                "    print(json.dumps({'kind':'realtime','match':{'index':0}}),flush=True)\n"
                "    (folder/'live.ready').touch()\n"
                "    time.sleep(30)\n"
                "elif sys.argv[1]=='--analyze':\n"
                "    lock=(folder/'live.lock').open('r+b');msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)\n"
                "    lock.close()\n"
                "    print(json.dumps({'kind':'result','result':{'times':[1]}}),flush=True)\n"
                "else:\n"
                "    deadline=time.monotonic()+8\n"
                "    while not (folder/'live.ready').exists():\n"
                "        assert time.monotonic()<deadline\n"
                "        time.sleep(.01)\n"
                "    time.sleep(.1)\n"
                "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue();song=Song('a','Example','Artist','Album',(Line('例文です','',''),))
            job=AudioJob(base,song,Playback(duration=100),messages)
            try:
                job.thread.join(timeout=12)
                self.assertFalse(job.thread.is_alive())
                events=[]
                while not messages.empty():events.append(messages.get()[1][1]['kind'])
                self.assertLess(events.index('realtime'),events.index('result'))
                self.assertNotIn('error',events)
                self.assertIsNotNone(job.realtime_process.poll())
                self.assertFalse(Path(job.temp.name).exists())
            finally:job.close()

    def test_two_ready_tracks_never_load_models_at_the_same_time(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys,time\nfrom pathlib import Path\n"
                "if sys.argv[1]=='--analyze':\n"
                "    lock=Path(__file__).with_name('model.lock')\n"
                "    with lock.open('x'):\n"
                "        time.sleep(.3)\n"
                "    lock.unlink()\n"
                "    print(json.dumps({'kind':'result','result':{'times':[1]}}),flush=True)\n"
                "else:\n"
                "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue()
            song=Song('a','Example','Artist','Album',(Line('例文です','',''),))
            jobs=[AudioJob(base,song,Playback(duration=100),messages) for _ in range(2)]
            try:
                for job in jobs:
                    job.thread.join(timeout=10)
                    self.assertFalse(job.thread.is_alive())
                events=[]
                while not messages.empty():events.append(messages.get()[1][1]['kind'])
                self.assertEqual(events.count('result'),2)
                self.assertNotIn('error',events)
            finally:
                for job in jobs:job.close()

    def test_capture_and_model_run_in_distinct_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys,os,ctypes as C\nfrom pathlib import Path\n"
                "kernel=C.windll.kernel32;kernel.GetCurrentProcess.restype=C.c_void_p\n"
                "kernel.GetPriorityClass.argtypes=[C.c_void_p]\n"
                "priority=kernel.GetPriorityClass(kernel.GetCurrentProcess())\n"
                "if sys.argv[1]=='--analyze':\n"
                "    assert priority==0x4000,priority\n"
                "    old=int(Path(sys.argv[2]).with_name('pid.txt').read_text())\n"
                "    assert old!=os.getpid()\n"
                "    print(json.dumps({'kind':'result','result':{'times':[1]}}),flush=True)\n"
                "else:\n"
                "    assert priority==0x20,priority\n"
                "    Path(sys.argv[1]).with_name('pid.txt').write_text(str(os.getpid()))\n"
                "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue()
            song=Song('a','Example','Artist','Album',(Line('例文です','',''),))
            job=AudioJob(base,song,Playback(duration=100),messages)
            job.thread.join(timeout=10)
            self.assertFalse(job.thread.is_alive())
            self.assertEqual(messages.get(timeout=2)[1][1]['kind'],'progress')
            self.assertEqual(messages.get(timeout=2)[1][1]['kind'],'result')
            self.assertEqual(messages.get(timeout=2)[1][1]['kind'],'finished')
            self.assertFalse(Path(job.temp.name).exists())

    def test_completion_returns_results_and_removes_all_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys,os\nfrom pathlib import Path\n"
                "Path(sys.argv[1]).with_name('capture.wav').write_bytes(b'fake audio')\n"
                "print(json.dumps({'kind':'result','result':{'times':[1]},'pid':os.getpid()}),flush=True)\n",encoding='utf-8')
            messages=queue.Queue()
            song=Song('a','Example','Artist','Album',(Line('例文です','',''),))
            job=AudioJob(base,song,Playback(duration=100),messages)
            temporary=Path(job.temp.name)
            job.thread.join(timeout=10)
            self.assertFalse(job.thread.is_alive())
            self.assertIsNotNone(job.process.poll())
            self.assertFalse(temporary.exists())
            events=[]
            while not messages.empty():
                event=messages.get()[1][1]
                events.append(event['kind'])
                if event['kind']=='result':
                    self.assertEqual(event['pid'],job.process.pid)
            self.assertEqual(events,['result','finished'])

    def test_cancel_terminates_owned_worker_and_removes_partial_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'audio_worker.py').write_text(
                "import json,sys,time\nfrom pathlib import Path\n"
                "held=Path(sys.argv[1]).with_name('capture.wav').open('wb')\n"
                "held.write(b'fake audio'); held.flush()\n"
                "print(json.dumps({'kind':'progress','text':'ready'}),flush=True)\n"
                "time.sleep(30)\n",encoding='utf-8')
            messages=queue.Queue()
            song=Song('a','Example','Artist','Album',(Line('例文です','',''),))
            job=AudioJob(base,song,Playback(duration=100),messages)
            temporary=Path(job.temp.name)
            self.assertEqual(messages.get(timeout=10)[1][1]['kind'],'progress')
            job.close()
            self.assertFalse(job.thread.is_alive())
            self.assertIsNotNone(job.process.poll())
            self.assertFalse(temporary.exists())
            self.assertEqual(messages.get(timeout=2)[1][1]['kind'],'finished')


if __name__=='__main__':
    unittest.main()
