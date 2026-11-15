from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np

from audio_capture_cache import capture_frames
from realtime_audio import LIVE_AUDIO_FILES,LiveWindowWriter,StreamingAligner
from realtime_lyrics import RealtimeLyricMatcher
from storage import read_json


class StreamingTests(unittest.TestCase):
    def segment(self,**changes):
        return SimpleNamespace(compression_ratio=changes.get('compression_ratio',1),
            avg_logprob=changes.get('avg_logprob',-.2),
            no_speech_prob=changes.get('no_speech_prob',.1),
            words=[SimpleNamespace(word='青い空を見上げて',start=6,end=7.8,probability=.9)])

    def test_uses_independent_japanese_recognition_without_lyrics_prompt_or_speech_vad(self):
        model=Mock();model.transcribe.return_value=([self.segment()],None)
        aligner=StreamingAligner(model,['青い空を見上げて'])
        with patch.object(aligner.follower,'select',return_value={'index':0}) as select:
            self.assertEqual(aligner.analyze(np.zeros(128000),10),{'index':0})
        words,start,duration=select.call_args.args
        self.assertEqual((start,duration),(10,8))
        self.assertEqual(words[0]['start'],6)
        options=model.transcribe.call_args.kwargs
        self.assertEqual(options['language'],'ja')
        self.assertFalse(options['vad_filter'])
        self.assertFalse(options['condition_on_previous_text'])
        self.assertNotIn('initial_prompt',options)
        self.assertNotIn('prefix',options)

    def test_rejects_repetitions_and_low_probability_segments(self):
        for changes in ({'avg_logprob':-1.5},{'compression_ratio':3}):
            with self.subTest(changes=changes):
                model=Mock();model.transcribe.return_value=([self.segment(**changes)],None)
                aligner=StreamingAligner(model,['青い空を見上げて'])
                with patch.object(aligner.follower,'select') as select:
                    aligner.analyze(np.zeros(128000),10)
                    self.assertEqual(select.call_args.args[0],[None])

    def test_singing_with_strong_words_is_not_dropped_for_high_no_speech_alone(self):
        model=Mock();model.transcribe.return_value=([self.segment(no_speech_prob=.9)],None)
        aligner=StreamingAligner(model,['青い空を見上げて'])
        with patch.object(aligner.follower,'select') as select:
            aligner.analyze(np.zeros(128000),10)
            self.assertEqual(select.call_args.args[0][0]['word'],'青い空を見上げて')

    def test_rejected_segment_preserves_a_boundary_between_valid_words(self):
        model=Mock();model.transcribe.return_value=([self.segment(),self.segment(avg_logprob=-2),self.segment()],None)
        aligner=StreamingAligner(model,['青い空を見上げて'])
        with patch.object(aligner.follower,'select') as select:
            aligner.analyze(np.zeros(128000),10)
            words=select.call_args.args[0]
            self.assertIsNone(words[1])
            self.assertEqual(len(words),3)

    def test_short_startup_and_oversized_windows_do_not_decode(self):
        model=Mock();aligner=StreamingAligner(model,['青い空を見上げて'])
        self.assertIsNone(aligner.analyze(np.zeros(16000),0))
        with self.assertRaises(ValueError):aligner.analyze(np.zeros(192000),0)
        model.transcribe.assert_not_called()

    def test_recognized_distinct_phrases_can_reacquire_beyond_the_local_window(self):
        lines=['まだ聞いていない歌詞']*30
        lines[0]='始まりの音を探して';lines[22]='星屑と夢の青空';lines[23]='明日へ進もう'
        first=self.segment();first.words=[SimpleNamespace(word=lines[22],start=6,end=7.2,probability=.95)]
        second=self.segment();second.words=[SimpleNamespace(word=lines[23],start=6.4,end=7.8,probability=.85)]
        model=Mock();model.transcribe.side_effect=[([first],None),([second],None)]
        aligner=StreamingAligner(model,lines)
        aligner.follower=RealtimeLyricMatcher(lines,reader=lambda text:text)
        aligner.follower.index=0
        self.assertIsNone(aligner.analyze(np.zeros(128000),40))
        result=aligner.analyze(np.zeros(128000),41)
        self.assertEqual(result['index'],23)
        self.assertTrue(result['global_recovery'])
        self.assertEqual(result['recovery_characters'],13)
        self.assertEqual(result['confidence'],.85)

    def test_longer_ring_preserves_sample_clock_and_remains_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=LiveWindowWriter(folder,8000,2,window_seconds=8)
            for second in range(1,13):
                writer.feed(b'\0'*32000,8000,second*8000,17,100)
            meta=read_json(Path(folder)/'live-window.json',{})
            self.assertEqual(capture_frames(Path(folder)/meta['file']), (64000,8000,2))
            self.assertEqual(meta['start_position'],21)
            self.assertEqual(meta['end_position'],29)
            self.assertEqual(meta['observed_at'],112)
            self.assertEqual({path.name for path in Path(folder).glob('*.sloa')},set(LIVE_AUDIO_FILES))


if __name__=='__main__':unittest.main()
