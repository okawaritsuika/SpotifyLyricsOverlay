"""Verified translations must not replace the rows of an audio capture."""
from dataclasses import replace
import time
import unittest

from audio_alignment import ALIGNMENT_METHOD
from lyrics import Line
from test_auto_audio import FakeJob
import test_background_overlay as background
import test_lyric_annotations as annotations


class AudioAnnotationRefreshTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown

    def prepare(self):
        self.originals,self.translated=annotations.AnnotationTests().parenthetical_document()
        self.app.select_song(self.app.live_lyrics.receive(self.playback,None,self.originals))
        self.app.playback=replace(self.playback,base=0,playing=True,captured=time.monotonic())
        self.app.auto_analyzer.enabled=True;self.app.auto_analyzer.job_factory=FakeJob
        self.app.observe_auto_audio()
        self.job=next(iter(self.app.auto_analyzer.jobs))
        self.track=self.app.reader_bridge.snapshot()['track']

    def result(self):
        self.app.receive_auto_audio(self.job,{'kind':'result','result':{
            'times':[10,20,30,40,50],'method':ALIGNMENT_METHOD,'partial':False}})

    def test_translation_only_keeps_capture_and_accepts_result(self):
        self.prepare()
        self.app.receive_reader_lyrics(self.track,self.translated)
        self.assertEqual(list(self.app.auto_analyzer.jobs),[self.job])
        self.assertEqual([line.original for line in self.app.song.lines],
                         [line.original for line in self.originals])
        self.result()
        self.assertEqual(self.app.timing['times'],[10,20,30,40,50])

    def test_verified_parenthetical_row_keeps_capture_and_manual_onset(self):
        self.prepare()
        timing={**self.app.timing,'times':[8,None,None,None,None],
                'origins':['manual',None,None,None,None]}
        self.app.save_current_timing(timing);self.app.timing=timing
        incoming=(self.translated[0],Line('(風の合図)','','(코러스)'),*self.translated[1:])
        self.app.receive_reader_lyrics(self.track,incoming)
        self.assertEqual(list(self.app.auto_analyzer.jobs),[self.job])
        self.assertEqual([line.original for line in self.app.song.lines],
                         [line.original for line in self.originals])
        self.assertEqual(self.app.translated_count,5)
        self.result()
        self.assertEqual(self.app.timing['times'],[8,20,30,40,50])
        self.assertEqual(self.app.auto_analyzer.progress[self.job].phase,'completed')

    def test_saved_partial_audio_keeps_rows_when_verified_aside_arrives(self):
        self.prepare();self.result()
        self.app.receive_auto_audio(self.job,{'kind':'finished'})
        timing={**self.app.timing,'times':[10,20,30,40,None],
                'origins':['audio']*4+[None],'audio_finalized':False}
        self.app.save_current_timing(timing);self.app.timing=timing
        path=self.app.auto_analyzer.live_timing.path(self.app.song);before=path.read_bytes()
        incoming=(self.translated[0],Line('(風の合図)','','(코러스)'),*self.translated[1:])
        self.app.receive_reader_lyrics(self.track,incoming)
        self.assertEqual(self.app.timing['times'],[10,20,30,40,None])
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(self.app.auto_analyzer.jobs)

    def test_different_japanese_still_rejects_old_result(self):
        self.prepare()
        self.app.receive_reader_lyrics(self.track,tuple(Line('異なる物語'+str(i),'','다른 번역') for i in range(5)))
        self.result()
        self.assertEqual(self.app.auto_analyzer.progress[self.job].phase,'discarded')
        self.assertEqual(self.app.timing['times'],[None]*5)


if __name__=='__main__':unittest.main()
