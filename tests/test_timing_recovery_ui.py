"""Optional timing files cannot stop playback updates or overwrite user data."""
import hashlib
import time
import unittest
from unittest.mock import patch

from live_timing import original_signature
from lyrics import Song
from media import Playback
from storage import atomic_json
from tests import test_ui as fixtures


class TimingRecoveryUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    playback=fixtures.UITests.playback

    def test_corrupt_native_file_preserves_tick_and_reloads_after_repair(self):
        path=self.store.directory/'native_timings'/(hashlib.sha256(
            (self.a.id+original_signature(self.a)).encode()).hexdigest()+'.json')
        atomic_json(path,{'duration':180,'times':[20,10,None,None]})
        before=path.read_bytes()
        self.playback(self.a)
        with patch.object(self.root,'after',wraps=self.root.after) as scheduled:
            self.app.tick()
        self.assertTrue(any(call.args[-1]==self.app.tick for call in scheduled.call_args_list))
        self.assertTrue(self.app.is_bound())
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.app.timing['times'],[None]*4)
        self.assertTrue(self.app.error_notice[0])
        atomic_json(path,{'duration':180,'times':[5,10,15,20]})
        self.assertEqual(self.app.effective_times(),([5,10,15,20],False))

    def test_native_write_failure_keeps_live_highlight_and_manual_timing(self):
        self.playback(self.a)
        before=self.store.timing(self.a)
        now=time.monotonic()
        with patch.object(self.app.native_timing,'observe',side_effect=OSError('optional timing unavailable')):
            self.app.receive_spotify_active(self.app.playback.key,
                [line.original for line in self.a.lines],1,now,10,True)
        self.assertEqual(self.app.spotify_current,1)
        self.assertEqual(self.app.spotify_anchor,(1,10))
        self.assertEqual(self.store.timing(self.a),before)
        self.assertIn('optional timing unavailable',self.app.error_notice[0])

    def test_invalid_screen_lyric_timing_does_not_drop_following_playback_message(self):
        playback=Playback('Unregistered','Artist','Album',20,180,False,time.monotonic())
        self.app.set_playback(playback)
        song=Song('memory-'+hashlib.sha256(playback.key.encode()).hexdigest()[:24],
                  playback.title,playback.artist,playback.album,self.a.lines)
        path=self.app.live_lyrics.timing_store.path(song)
        atomic_json(path,{'signature':original_signature(song),'times':[20,10,None,None]})
        before=path.read_bytes()
        self.app.messages.put(('spotify_lyrics',(playback.key,self.a.lines)))
        self.app.messages.put(('media',Playback('Next track','Artist','Album',0,180,False,time.monotonic())))
        with patch.object(self.root,'after',wraps=self.root.after) as scheduled:
            self.app.tick()
        self.assertEqual(self.app.playback.title,'Next track')
        self.assertIsNone(self.app.song)
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(any(call.args[-1]==self.app.tick for call in scheduled.call_args_list))

    def test_failed_manual_selection_does_not_mix_new_song_with_previous_timing(self):
        self.store.save_timing(self.a,{'times':[1,2,3,4],'offset':0})
        self.playback(self.a)
        previous_song=self.app.song
        previous=self.app.timing.copy()
        path=self.store.directory/'timings'/(self.b.id+'.json')
        atomic_json(path,{'fingerprint':self.b.fingerprint,'times':[2,1,None,None]})
        before=path.read_bytes()
        with self.assertRaises(ValueError):self.app.select_song(self.b)
        self.assertIs(self.app.song,previous_song)
        self.assertEqual(self.app.timing,previous)
        self.assertEqual(self.app.display_lines,self.a.lines)
        self.assertEqual(path.read_bytes(),before)

    def test_track_with_corrupt_saved_timing_clears_previous_lyrics_and_keeps_tick(self):
        self.playback(self.a)
        path=self.store.directory/'timings'/(self.b.id+'.json')
        atomic_json(path,{'fingerprint':self.b.fingerprint,'times':[2,1,None,None]})
        before=path.read_bytes()
        self.app.messages.put(('media',Playback(self.b.title,self.b.artist,self.b.album,
                                               10,180,False,time.monotonic())))
        with patch.object(self.root,'after',wraps=self.root.after) as scheduled:self.app.tick()
        self.assertEqual(self.app.playback.title,self.b.title)
        self.assertIsNone(self.app.song)
        self.assertIsNone(self.app.timing)
        self.assertEqual(self.app.display_lines,())
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(any(call.args[-1]==self.app.tick for call in scheduled.call_args_list))


if __name__=='__main__':unittest.main()
