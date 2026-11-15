"""Synthetic worker pipes exercise process cleanup without WebView or network."""
from pathlib import Path
import io
import os
import queue
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from background_lyrics import SpotifyJob
from internal_reader import BrowserJob


class WorkerPipeLifetimeTests(unittest.TestCase):
    def worker(self,cls,event=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        base=Path(temp.name)
        filename='spotify_worker.py' if cls is SpotifyJob else 'browser_worker.py'
        script='import json,os,sys,time\nrequest=json.loads(sys.stdin.readline())\n'
        if event is not None:script+=f'print(json.dumps({event!r}),flush=True)\n'
        script+='os.close(sys.stdout.fileno())\ntime.sleep(15)\n'
        (base/filename).write_text(script,encoding='utf-8')
        messages=queue.Queue()
        if cls is SpotifyJob:job=cls(base,base/'profile',messages)
        else:job=cls(base,{'token':'test','track':'one'},messages,profile=base/'profile')
        def cleanup():
            if job.process.poll() is None:job.process.terminate()
            job.process.wait(timeout=3)
            job.close()
        self.addCleanup(cleanup)
        return job,messages

    def finished_events(self,job,messages):
        job.thread.join(timeout=5)
        self.assertFalse(job.thread.is_alive(),'pipe EOF must not leave the reader waiting forever')
        self.assertIsNotNone(job.process.poll(),'finished must not abandon a live worker')
        return [messages.get_nowait()[1][1] for _ in range(messages.qsize())]

    def test_spotify_eof_reaps_live_child_before_publishing_finished(self):
        job,messages=self.worker(SpotifyJob)
        events=self.finished_events(job,messages)
        self.assertEqual(events,[{'kind':'finished','exit_code':job.process.returncode}])

    def test_reader_eof_without_result_reports_error_and_reaps_child(self):
        job,messages=self.worker(BrowserJob)
        events=self.finished_events(job,messages)
        self.assertEqual([event['kind'] for event in events],['error','finished'])

    def test_reader_eof_after_result_reaps_child_without_replacing_result(self):
        job,messages=self.worker(BrowserJob,{'kind':'choices','urls':[]})
        events=self.finished_events(job,messages)
        self.assertEqual([event['kind'] for event in events],['choices','finished'])

    def test_cancel_during_eof_cleanup_does_not_publish_unexpected_error(self):
        job,messages=self.worker(BrowserJob)
        job.cancel()
        events=self.finished_events(job,messages)
        self.assertEqual(events,[{'kind':'finished'}])

    def test_reader_ignores_non_object_frames_before_a_valid_result(self):
        job=BrowserJob.__new__(BrowserJob)
        job.messages=queue.Queue();job.cancelled=threading.Event();job._stop_input=threading.Event()
        job.process=SimpleNamespace(stdout=io.StringIO('null\n[]\n{"kind":"choices","urls":[]}\n'),
                                    wait=Mock(return_value=0))
        job._read()
        events=[job.messages.get_nowait()[1][1] for _ in range(job.messages.qsize())]
        self.assertEqual(events,[{'kind':'choices','urls':[]},{'kind':'finished'}])
        job.process.wait.assert_called_once_with(timeout=1)

    def test_frozen_reader_inherits_parent_bundle_environment(self):
        inherited={'PYINSTALLER_RESET_ENVIRONMENT':'1','_PYI_APPLICATION_HOME_DIR':'synthetic-bundle'}
        with patch.dict(os.environ,inherited), patch('app_paths.frozen',return_value=True), \
                patch('internal_reader.subprocess.Popen') as launch, patch('internal_reader.threading.Thread'):
            BrowserJob('.',{'token':'test','track':'one'},queue.Queue())
        environment=launch.call_args.kwargs['env']
        self.assertNotIn('PYINSTALLER_RESET_ENVIRONMENT',environment)
        self.assertEqual(environment['_PYI_APPLICATION_HOME_DIR'],'synthetic-bundle')

    def test_cancel_does_not_wait_for_blocked_startup_pipe(self):
        writing=threading.Event();exited=threading.Event();closed=threading.Event()
        waiting=threading.Event();allow_terminate=threading.Event()

        def write(value):
            writing.set()
            exited.wait(5)
            raise BrokenPipeError

        def wait(timeout):
            if not exited.is_set():
                waiting.set()
                allow_terminate.wait(5)
                raise subprocess.TimeoutExpired('test reader',timeout)
            return -1

        class Output:
            def __iter__(self):
                exited.wait(5)
                return iter(())

            def close(self):pass

        stream=Mock(write=Mock(side_effect=write),close=Mock(side_effect=closed.set))
        process=SimpleNamespace(stdin=stream,stdout=Output(),wait=wait,terminate=Mock(side_effect=exited.set))
        messages=queue.Queue()
        with patch('internal_reader.subprocess.Popen',return_value=process):
            job=BrowserJob('.',{'token':'test','track':'one','full_reference_lines':['歌詞'*6000]},messages)
        try:
            self.assertTrue(writing.wait(5))
            self.assertFalse(closed.is_set())
            job.cancel()
            self.assertTrue(waiting.wait(5))
            self.assertTrue(job.cancelled.is_set())
            self.assertFalse(exited.is_set())
            self.assertFalse(closed.is_set())
        finally:
            allow_terminate.set()
        self.assertTrue(closed.wait(5))
        job.thread.join(timeout=5)
        self.assertFalse(job.thread.is_alive())
        process.terminate.assert_called_once()
        stream.write.assert_called_once()
        self.assertEqual(messages.get_nowait()[1][1],{'kind':'finished'})


if __name__=='__main__':unittest.main()
