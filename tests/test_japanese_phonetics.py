import importlib.util
import unittest

from japanese_phonetics import PhoneSpan, prepare_lines


@unittest.skipUnless(importlib.util.find_spec('pyopenjtalk'), 'pyopenjtalk-plus is optional')
class JapanesePhoneticsTests(unittest.TestCase):
    def test_mixed_lyrics_preserve_source_text_and_indices(self):
        lyrics = ['君が好き', '君と Hello!', '君が好き', 'ハッピー']
        lines = prepare_lines(lyrics)
        self.assertEqual([(line.index, line.text) for line in lines], list(enumerate(lyrics)))
        self.assertEqual(lines[0].phones, tuple('k i m i g a s u k i'.split()))
        self.assertEqual(lines[1].reading, 'きみとはろー')
        self.assertEqual(lines[1].spans[-1], PhoneSpan(3, 8, 6, 11))
        self.assertEqual(lines[0].phones, lines[2].phones)
        self.assertEqual(lines[3].morae, ('は', 'っ', 'ぴ', 'ー'))
        self.assertEqual(lines[3].phones, ('h', 'a', 'cl', 'p', 'i', 'i'))
        self.assertEqual(lines[3].ctc_phones, ('h', 'a', 'cl', 'p', 'i:'))
        self.assertEqual(lines[3].spans[-1], PhoneSpan(3, 4, 5, 6))

    def test_unknown_latin_reading_requires_override(self):
        line = prepare_lines(['xqz君'])[0]
        self.assertEqual(line.phones, ())
        self.assertIn('xqz', line.error)

    def test_separated_kana_are_read_without_interpunct(self):
        line = prepare_lines(['オ・ウ・エ・ア・イ'])[0]
        self.assertIsNone(line.error)
        self.assertEqual(line.phones, ('o', 'u', 'e', 'a', 'i'))

    def test_override_and_intentional_omission_keep_original_span(self):
        line = prepare_lines(['君と xqz 私'], {'xqz': 'エックス', '私': ''})[0]
        self.assertIsNone(line.error)
        self.assertEqual(line.text, '君と xqz 私')
        self.assertEqual(line.reading, 'きみとえっくす')
        self.assertEqual({(span.start, span.end) for span in line.spans}, {(0, 1), (1, 2), (3, 6)})

    def test_invalid_reading_override_is_reported(self):
        with self.assertRaisesRegex(ValueError, 'kana'):
            prepare_lines(['君'], {'君': 'kimi'})
