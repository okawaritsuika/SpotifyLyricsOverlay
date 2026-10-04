"""Normal window exit lets a real capture child finish and commits its result."""
from pathlib import Path
import time
import unittest

from audio_job import AudioJob
from media import Playback
from tests import test_audio as fixture


class AudioExitProcessTests(unittest.TestCase):
    setUp=fixture.AudioUITests.setUp

    def tearDown(self):
        if not self.app.closed:self.app._finish_close()
        self.temp.cleanup()

    def test_window_close_finishes_real_child_saves_then_releases_all_audio(self):
        base=Path(self.temp.name)/'worker';base.mkdir()
        (base/'audio_worker.py').write_text(
            "import json,sys,time\nfrom pathlib import Path\n"
            "path=Path(sys.argv[-1]);folder=path.parent\n"
            "request=json.loads(path.read_text(encoding='utf-8'))\n"
            "assert request['incremental'] and request['allow_partial']\n"
            "if sys.argv[1]=='--analyze':\n"
            "    assert (folder/'finish-capture').exists()\n"
            "    print(json.dumps({'kind':'result','result':{'times':[5,None,None],"
            "'method':'incremental-lyrics-alignment-v1','incremental':True,'partial':True,"
            "'captured_seconds':20,'analyzed_ranges':[[0,20]]}}),flush=True)\n"
            "else:\n"
            "    (folder/'capture.wav').write_bytes(b'temporary audio fixture')\n"
            "    print(json.dumps({'kind':'progress','state':'recording','text':'captured20'}),flush=True)\n"
            "    deadline=time.monotonic()+10\n"
            "    while not (folder/'finish-capture').exists():\n"
            "        assert time.monotonic()<deadline\n"
            "        time.sleep(.01)\n"
            "    print(json.dumps({'kind':'captured'}),flush=True)\n",encoding='utf-8')
        self.store.save_timing(self.song,{'times':[None,35,None],'offset':0,'origins':[None,'manual',None]})
        auto=self.app.auto_analyzer;auto.enabled=True;auto.base=base;auto.job_factory=AudioJob
        playback=Playback(self.song.title,self.song.artist,self.song.album,20,90,True,time.monotonic())
        auto.observe(self.song,playback)
        job=next(iter(auto.jobs));folder=Path(job.temp.name)
        deadline=time.monotonic()+8
        while not job.capture_started and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(job.capture_started);self.assertFalse(job.skip_final)
        self.app.close()
        self.assertTrue(self.app.closing);self.assertFalse(self.app.closed)
        while not self.app.closed and time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        self.assertTrue(self.app.closed)
        saved=self.store.timing(self.song)
        self.assertEqual(saved['times'],[5,35,None])
        self.assertEqual(saved['analyzed_ranges'],[[0,20]])
        self.assertFalse(folder.exists());self.assertIsNotNone(job.process.poll())
        self.assertFalse(job.thread.is_alive())


if __name__=='__main__':unittest.main()
