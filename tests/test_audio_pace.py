import unittest
from lyric_follow import AudioPace,is_vocalise,limit_vocalise
from lyrics import Line


class AudioPaceTests(unittest.TestCase):
    def test_vocalise_is_a_whole_phrase_not_a_word_inside_a_sentence(self):
        for text in ('La...', 'la la la', 'Lalala～', 'ラララ', 'らーらー', 'Na na na'):
            self.assertTrue(is_vocalise(text),text)
        for text in ('La vie en rose','LaLaと歌おう','花が咲く','nananaの歌','', 'All'):
            self.assertFalse(is_vocalise(text),text)

    def test_vocalise_never_gets_a_duration_from_character_count(self):
        pace=AudioPace([Line(text,'','') for text in ('あいう','えおか','La...', '続く言葉')])
        for index,onset in enumerate((10,12,14)):self.observe(pace,index,onset)
        self.assertIsNone(pace.next_start())
        for position in (16,30,90,134):self.assertEqual(pace.current(position),2)

    def test_vocalise_hold_uses_evidence_without_inventing_a_two_minute_duration(self):
        for position in (30,60,150,250):
            self.assertEqual(limit_vocalise(4,[2],[10,20,None,None,None],position),2)
        self.assertEqual(limit_vocalise(4,[2],[10,20,None,140,None],150),4)
        self.assertEqual(limit_vocalise(4,[2],[10,20,None,None,None],150,confirmed=3),4)
        self.assertEqual(limit_vocalise(1,[2],[None]*5,10),1)
        self.assertIsNone(limit_vocalise(None,[2],[None]*5,10))

    def pace(self):return AudioPace([Line('例文','あいうえおか','') for _ in range(10)])
    def observe(self,pace,index,onset,heard=None):pace.observe(index,onset,onset+.5 if heard is None else heard,.8)

    def test_requires_observed_speed_instead_of_guessing_from_song_length(self):
        pace=self.pace();self.observe(pace,0,10)
        self.assertEqual(pace.current(12),0)
        self.observe(pace,1,12)
        self.assertEqual(pace.current(14),1)
        self.observe(pace,2,14)
        self.assertEqual(pace.current(15.9),2)
        self.assertEqual(pace.current(16),3)

    def test_only_one_row_is_predicted_and_old_audio_does_not_keep_advancing(self):
        pace=self.pace()
        for index,onset in enumerate((10,12,14)):self.observe(pace,index,onset)
        self.assertEqual(pace.current(16),3)
        self.assertEqual(pace.current(17),3)
        self.assertEqual(pace.current(20),2)
        self.assertEqual(pace.current(100),2)

    def test_fresh_audio_can_cancel_wrong_prediction_without_oscillation(self):
        pace=self.pace()
        for index,onset in enumerate((10,12,14)):self.observe(pace,index,onset)
        self.assertEqual(pace.current(16),3)
        self.observe(pace,2,14,17.2)
        self.assertEqual(pace.current(17.3),2)
        self.observe(pace,2,14,18)
        self.assertEqual(pace.current(18.1),2)

    def test_rate_adapts_to_faster_and_slower_delivery(self):
        pace=self.pace()
        for index,onset in enumerate((0,3,6)):self.observe(pace,index,onset)
        self.assertAlmostEqual(pace.next_start(),9)
        self.observe(pace,3,7.5);self.observe(pace,4,9)
        self.assertAlmostEqual(pace.next_start(),10.5)
        self.observe(pace,5,12);self.observe(pace,6,15)
        self.assertAlmostEqual(pace.next_start(),18)

    def test_long_gap_or_skipped_lines_reset_the_learned_speed(self):
        for index,onset in ((3,40),(5,17)):
            pace=self.pace()
            for row,start in enumerate((10,12,14)):self.observe(pace,row,start)
            self.observe(pace,index,onset)
            self.assertIsNone(pace.next_start())
            self.assertEqual(pace.current(onset+2),index)

    def test_missing_or_weak_onsets_do_not_invent_a_speaking_rate(self):
        pace=self.pace()
        for index in range(4):pace.observe(index,None,index*2+.5,.8)
        self.assertIsNone(pace.next_start())
        pace=self.pace()
        for index in range(4):pace.observe(index,index*2,index*2+.5,.2)
        self.assertIsNone(pace.next_start())

    def test_longer_next_line_uses_its_own_reading_length(self):
        pace=AudioPace([Line('例','あいう',''),Line('例','えおか',''),Line('例','あいうえおか',''),Line('例','あいう','')])
        for index,onset in enumerate((10,11,12)):self.observe(pace,index,onset)
        self.assertAlmostEqual(pace.next_start(),14)

    def test_later_clipped_evidence_of_the_same_phrase_does_not_move_its_anchor(self):
        pace=self.pace()
        for index,onset in enumerate((10,12,14)):self.observe(pace,index,onset)
        self.observe(pace,2,15,15.5)
        self.assertAlmostEqual(pace.next_start(),16)
        self.assertEqual(pace.current(16),3)

    def test_long_phrase_is_not_forced_to_advance_at_an_arbitrary_time_limit(self):
        pace=AudioPace([Line('例','あいう',''),Line('例','えおか',''),Line('例','あいう'*40,''),Line('例','あいう','')])
        for index,onset in enumerate((10,12,14)):self.observe(pace,index,onset)
        self.assertIsNone(pace.next_start())
        self.assertEqual(pace.current(16),2)
