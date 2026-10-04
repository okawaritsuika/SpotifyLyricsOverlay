from pathlib import Path
import tempfile
import unittest
import wave
from realtime_audio import LiveWindowWriter,PartialFollower
from storage import read_json


def word(text='青い空',start=.3,end=2.8,probability=.8):
    return {'word':text,'start':start,'end':end,'probability':probability}


class RealtimeAudioTests(unittest.TestCase):
    def run_error_windows(self,outcomes):
        from types import SimpleNamespace
        from unittest.mock import patch,Mock
        from realtime_audio import run
        class Done(Exception):pass
        class Samples:
            def __len__(self):return 16000
            def __mul__(self,other):return self
        aligner=Mock();aligner.analyze.side_effect=outcomes
        samples=Samples();calls=0;events=[]
        def window(*args):
            nonlocal calls
            sequence=calls//2+1;calls+=1
            return {'sequence':sequence,'file':'live-0.wav','observed_at':0,'start_position':0,'end_position':3}
        def emit(kind,**value):
            events.append({'kind':kind,**value})
            if kind=='realtime' and aligner.analyze.call_count==len(outcomes):raise Done()
        modules={'numpy':SimpleNamespace(sqrt=lambda x:x,mean=lambda x:1),
            'faster_whisper':SimpleNamespace(WhisperModel=lambda *a,**k:None),
            'faster_whisper.audio':SimpleNamespace(decode_audio=lambda *a,**k:samples)}
        with patch.dict('sys.modules',modules),patch('analysis_limits.apply_analysis_limits'), \
             patch('realtime_audio.PartialAligner',return_value=aligner),patch('realtime_audio.read_json',side_effect=window), \
             patch('realtime_audio.time.monotonic',return_value=0),patch.object(Path,'read_bytes',return_value=b'synthetic'):
            try:run({'model':'unused','lines':['synthetic'],'duration':10,'key':'track'},'.',emit)
            except Done:pass
        return events,aligner.analyze.call_count

    def test_one_bad_window_recovers_without_reloading_model_or_exposing_text(self):
        events,count=self.run_error_windows([ValueError('private text'),{'index':0}])
        self.assertEqual(count,2)
        self.assertTrue(any(e.get('state')=='retrying' for e in events))
        self.assertEqual(events[-1]['kind'],'realtime')
        self.assertNotIn('private text',str(events))

    def test_three_consecutive_bad_windows_stop_instead_of_retrying_forever(self):
        events,count=self.run_error_windows([ValueError(),IndexError(),RuntimeError()])
        self.assertEqual(count,3);self.assertEqual(events[-1]['state'],'failed')

    def test_leaving_vocalise_needs_two_strong_fresh_matches_of_next_words(self):
        follower=PartialFollower(['La...', '新しい道へ進もう'])
        self.assertEqual(follower.select([[word('la')]],0,3,3)['index'],0)
        for position in (4,5):
            self.assertIsNone(follower.select([[],[word('新しい',probability=.4)]],0,3,position))
        groups=[[],[word('新しい道へ')]]
        self.assertIsNone(follower.select(groups,0,3,6))
        self.assertIsNone(follower.select(groups,0,3,6))
        self.assertEqual(follower.select(groups,0,3,7)['index'],1)

    def test_mid_song_estimate_only_seeds_search_and_needs_two_acoustic_matches(self):
        lines=[f'違う歌詞の番号{i}' for i in range(30)]
        follower=PartialFollower(lines,start_index=18)
        self.assertIsNone(follower.index)
        for position in (100,101):
            self.assertIsNone(follower.select([[word(lines[18],probability=.1)]],18,3,position,acquiring=True))
        self.assertIsNone(follower.select([[word(lines[18])]],18,3,102,acquiring=True))
        self.assertEqual(follower.select([[word(lines[18])]],18,3,103,acquiring=True)['index'],18)

    def test_mid_song_search_does_not_acquire_distant_or_repeated_chorus(self):
        lines=[f'違う歌詞の番号{i}' for i in range(30)]
        follower=PartialFollower(lines,start_index=18)
        for position in (100,101):
            self.assertIsNone(follower.select([[word(lines[1])]],1,3,position,acquiring=True))
        lines[17]=lines[18]
        follower=PartialFollower(lines,start_index=18)
        for position in (102,103):
            self.assertIsNone(follower.select([[word(lines[18])]],18,3,position,acquiring=True))

    def test_only_visible_supported_first_word_supplies_an_onset_for_pace(self):
        follower=PartialFollower(['青い空'])
        self.assertAlmostEqual(follower.select([[word()]],0,3,13)['onset'],10.3)
        follower=PartialFollower(['青い空'])
        result=follower.select([[word(start=0)]],0,3,13)
        self.assertIsNone(result['onset'])
        self.assertEqual(result['evidence_start'],10)

    def test_partial_acoustic_phrase_is_enough_without_full_line_coverage(self):
        follower=PartialFollower(['青い空を見上げながら歩こう','風が吹いた'])
        result=follower.select([[word()],[]],0,3,3)
        self.assertEqual(result['index'],0)

    def test_music_without_confident_words_does_not_advance_by_time(self):
        follower=PartialFollower(['青い空','風が吹く'])
        for position in (3,30,100):
            self.assertIsNone(follower.select([[word(probability=.05)],[]],0,3,position))
        self.assertIsNone(follower.index)

    def test_short_high_confidence_prefix_can_follow_next_line(self):
        follower=PartialFollower(['青い空','風が吹く'])
        follower.select([[word()],[]],0,3,3)
        self.assertEqual(follower.select([[],[word('風が')]],0,3,4)['index'],1)

    def test_jump_requires_fresh_repeated_evidence_and_never_goes_backwards(self):
        follower=PartialFollower(['青い空']*8)
        follower.select([[word()]],0,3,3)
        groups=[[],[],[word()]]
        self.assertIsNone(follower.select(groups,0,3,4))
        self.assertIsNone(follower.select(groups,0,3,4))
        self.assertEqual(follower.select(groups,0,3,5)['index'],2)
        self.assertIsNone(follower.select([[word()]],0,3,6))
        self.assertIsNone(follower.select([[word()]],7,3,7))
        self.assertEqual(follower.index,2)

    def test_collapsed_future_words_and_old_words_are_not_current(self):
        follower=PartialFollower(['青い空'])
        for value in (word(start=3,end=3),word(start=0,end=.4),word(probability=float('nan'))):
            self.assertIsNone(follower.select([[value]],0,3,follower.last_window+1))

    def test_missed_opening_can_acquire_nearby_unique_phrase_with_two_observations(self):
        follower=PartialFollower(['始まり','風が吹く','星の夜','青い空を見上げて','続く道','遠い夢'])
        groups=[[],[],[],[word('青い空を見上げて')],[],[]]
        self.assertIsNone(follower.select(groups,0,3,15))
        self.assertIsNone(follower.select(groups,0,3,16,acquiring=True))
        self.assertIsNone(follower.select(groups,0,3,16,acquiring=True))
        self.assertEqual(follower.select(groups,0,3,17,acquiring=True)['index'],3)

    def test_acquisition_does_not_jump_to_a_repeated_or_weak_phrase(self):
        follower=PartialFollower(['青い空を見上げて']*6)
        for position in (16,17,18):
            self.assertIsNone(follower.select([[],[],[],[word('青い空を見上げて')]],0,3,position,acquiring=True))
        follower=PartialFollower(['始まり','風が吹く','星の夜','青い空を見上げて'])
        for position in (16,17):
            self.assertIsNone(follower.select([[],[],[],[word('青い空を見上げて',probability=.5)]],0,3,position,acquiring=True))

    def test_audio_preview_is_bounded_and_keeps_true_capture_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=LiveWindowWriter(folder,8000,2)
            for second in range(1,11):
                writer.feed(b'\0'*32000,8000,second*8000,.5,100)
                meta=read_json(Path(folder)/'live-window.json',{})
                with wave.open(str(Path(folder)/meta['file']),'rb') as audio:
                    self.assertLessEqual(audio.getnframes(),24000)
                    self.assertEqual(audio.getnchannels(),2)
                self.assertEqual(meta['end_position'],second+.5)
                self.assertEqual(meta['observed_at'],100+second)
                self.assertEqual(meta['start_position'],max(0,second-3)+.5)
            self.assertEqual(len(list(Path(folder).glob('*.wav'))),2)
