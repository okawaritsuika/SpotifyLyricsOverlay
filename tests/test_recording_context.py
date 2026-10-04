from dataclasses import replace
import gc
import unittest

from lyrics import Line,Song
from media import Playback
from recording_context import RecordingContexts


class RecordingContextTests(unittest.TestCase):
    def setUp(self):
        self.contexts=RecordingContexts()
        self.song=Song('a','Title','Artist','Album',(Line('One','',''),))
        self.playback=Playback(self.song.title,self.song.artist,self.song.album,duration=90)

    def observe(self,duration):self.contexts.observe(replace(self.playback,duration=duration))

    def test_conflict_and_return_permanently_invalidate_request(self):
        request=self.contexts.capture(self.song,self.playback)
        self.observe(180);self.observe(90)
        self.assertFalse(self.contexts.valid(request,self.song))

    def test_unknown_and_jitter_keep_context_without_assigning_unknown_owner(self):
        request=self.contexts.capture(self.song,self.playback)
        for duration in (0,None,-1,True,float('nan'),float('inf'),10**1000,88,90,92):
            self.observe(duration)
            self.assertTrue(self.contexts.valid(request,self.song))
        unknown=self.contexts.capture(self.song)
        self.assertIsNone(unknown.duration)
        self.observe(180);self.assertFalse(self.contexts.valid(unknown,self.song))

    def test_each_request_keeps_its_start_length_even_inside_context_jitter(self):
        first=self.contexts.capture(self.song,self.playback)
        second=self.contexts.capture(self.song,replace(self.playback,duration=92))
        self.observe(89);self.observe(92)
        self.assertTrue(self.contexts.valid(first,self.song))
        self.assertFalse(self.contexts.valid(second,self.song))

    def test_gradual_drift_cannot_evade_fixed_request_length(self):
        request=self.contexts.capture(self.song,self.playback)
        for duration in (91,92,93,92,91,90):self.observe(duration)
        self.assertFalse(self.contexts.valid(request,self.song))

    def test_different_metadata_does_not_invalidate_background_request(self):
        request=self.contexts.capture(self.song,self.playback)
        self.contexts.observe(replace(self.playback,title='Other',duration=180))
        self.assertTrue(self.contexts.valid(request,self.song))

    def test_changed_fingerprint_or_song_id_reject_request(self):
        request=self.contexts.capture(self.song,self.playback)
        self.assertFalse(self.contexts.valid(request,replace(self.song,id='b')))
        self.assertFalse(self.contexts.valid(request,replace(self.song,lines=(Line('Other','',''),))))

    def test_completed_candidate_uses_duration_not_old_request_generation(self):
        request=self.contexts.capture(self.song,self.playback)
        candidate={'duration':90,'recording_key':request.key}
        self.observe(180);self.assertFalse(self.contexts.candidate_matches(self.song,candidate))
        self.observe(90);self.assertTrue(self.contexts.candidate_matches(self.song,candidate))
        self.assertFalse(self.contexts.valid(request,self.song))

    def test_legacy_candidate_compares_target_playback_without_guessing_unknown(self):
        self.assertFalse(self.contexts.candidate_matches(self.song,{'duration':90},replace(self.playback,duration=180)))
        for duration in (None,0):
            value={'duration':duration}
            self.assertTrue(self.contexts.candidate_matches(self.song,value,replace(self.playback,duration=180)))
            self.assertEqual(value,{'duration':duration})

    def test_requests_do_not_accumulate_after_workers_release_them(self):
        request=self.contexts.capture(self.song,self.playback)
        self.assertEqual(len(self.contexts.requests),1)
        del request;gc.collect()
        self.assertEqual(len(self.contexts.requests),0)


if __name__=='__main__':unittest.main()
