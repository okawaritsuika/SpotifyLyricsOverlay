from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
import unittest

from audio_job import AudioJob
from lyrics import Line, Song
from media import Playback


class PlaylistAnalysisTests(unittest.TestCase):
    def test_playlist_collects_first_then_runs_final_analysis_without_live_preview(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'audio_worker.py').write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "path = Path(sys.argv[-1])\n"
                "request = json.loads(path.read_text(encoding='utf-8'))\n"
                "assert request['playlist_analysis'] and not request['realtime']\n"
                "if len(sys.argv) == 2:\n"
                "    path.with_name('capture.sloa').write_bytes(b'private data')\n"
                "    print('{\"kind\":\"captured\"}', flush=True)\n"
                "else:\n"
                "    assert sys.argv[1] == '--analyze'\n"
                "    assert path.with_name('capture.sloa').is_file()\n"
                "    print('{\"kind\":\"result\",\"result\":{\"times\":[2.0]}}', flush=True)\n",encoding='utf-8')
            messages = Queue()
            song = Song('song','Title','Artist','Album',(Line('first','',''),))
            job = AudioJob(base,song,Playback(duration=90),messages,playlist_analysis=True)
            try:
                job.thread.join(timeout=10)
                self.assertFalse(job.thread.is_alive())
                self.assertTrue(job.capture_complete)
                self.assertIsNone(job.realtime_thread)
                self.assertFalse(Path(job.temp.name).exists())
                events = []
                while not messages.empty():
                    events.append(messages.get()[1][1]['kind'])
                self.assertEqual(events,['progress','result','finished'])
            finally:
                job.close()
