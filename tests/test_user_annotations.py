import json
from pathlib import Path
import tempfile
import unittest

from lyrics import Line
from media import Playback
from user_annotations import EMPTY, UserAnnotations


def supplied(original, korean, singers=(), reading=''):
    return {'original': original, 'korean': korean, 'reading': reading, 'singers': list(singers)}


class UserAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'user_lyrics' / 'song.json'
        self.path.parent.mkdir()
        self.playback = Playback('Synthetic Song - Game Size', 'Unit', 'Album', duration=100)

    def tearDown(self):
        self.temp.cleanup()

    def document(self, rows, **changes):
        value = {'schema_version': 1, 'title': 'Synthetic Song', 'artists': ['Unit'],
                 'source': {'kind': 'user_provided'}, 'lines': rows, **changes}
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return UserAnnotations(self.directory)

    def test_exact_short_edit_phrase_reuses_only_explicit_singers(self):
        annotations = self.document([supplied('青い空を見る', '파란 하늘을 보다', ['如月千早']),
                                     supplied('花を描く', '꽃을 그리다'),
                                     supplied('省かれた節', '생략된 절', ['天海春香'])])
        result = annotations.match(self.playback, [Line('青い 空を見る！', '', ''), Line('花を描く', '', '')])
        self.assertEqual(result[0].korean, '파란 하늘을 보다')
        self.assertEqual(result[0].singers, ('如月千早',))
        self.assertEqual(result[1].singers, ())

    def test_wrong_title_artist_or_recording_variant_never_matches(self):
        annotations = self.document([supplied('青い空', '파란 하늘', ['如月千早'])])
        for title, artist in [('Other Song', 'Unit'), ('Synthetic Song', 'Cover Singer'),
                              ('Synthetic Song (Live)', 'Unit'), ('Synthetic Song (Other Idol Ver.)', 'Unit')]:
            with self.subTest(title=title, artist=artist):
                self.assertEqual(annotations.match(Playback(title, artist), [Line('青い空', '', '')]), (EMPTY,))

    def test_complete_source_sentence_can_span_short_target_rows_without_dividing_reading(self):
        annotations = self.document([supplied('青い空を見て歩こう', '파란 하늘을 보며 걷자', ['如月千早'], 'あおいそらをみてあるこう')])
        result = annotations.match(self.playback, [Line('青い空を', '', ''), Line('見て歩こう', '', '')])
        self.assertEqual([row.korean for row in result], ['파란 하늘을 보며 걷자'] * 2)
        self.assertEqual([row.reading for row in result], ['', ''])
        self.assertEqual([row.singers for row in result], [('如月千早',)] * 2)

    def test_partial_sentence_and_typo_are_not_exact_annotations(self):
        annotations = self.document([supplied('青い空を見る', '파란 하늘을 보다', ['如月千早'])])
        for original in ('青い空', '青い空を観る'):
            self.assertEqual(annotations.match(self.playback, [Line(original, '', '')]), (EMPTY,))

    def test_repeated_phrase_with_conflicting_or_missing_singer_is_unassigned(self):
        for second in (['天海春香'], []):
            annotations = self.document([supplied('同じ言葉', '같은 말', ['如月千早']),
                                         supplied('別の節', '다른 절'), supplied('同じ言葉', '같은 말', second)])
            result = annotations.match(self.playback, [Line('同じ言葉', '', '')])
            self.assertEqual(result[0].korean, '같은 말')
            self.assertEqual(result[0].singers, ())

    def test_repeated_phrase_with_conflicting_translation_is_not_selected(self):
        annotations = self.document([supplied('同じ言葉', '첫 번역', ['如月千早']),
                                     supplied('同じ言葉', '다른 번역', ['如月千早'])])
        result = annotations.match(self.playback, [Line('同じ言葉', '', '')])
        self.assertEqual(result[0].korean, '')
        self.assertEqual(result[0].singers, ('如月千早',))

    def test_same_complete_meaning_ignores_inserted_join_but_singer_conflict_remains(self):
        annotations = self.document([
            supplied('青い空を見る (はい)', '파란 하늘을 보다 (하이)', ['如月千早']),
            supplied('青い空を見る', '파란 하늘을 보다'), supplied('(はい)', '(하이)')])
        result = annotations.match(self.playback, [Line('青い空', '', ''), Line('を見る (はい)', '', '')])
        self.assertEqual([row.korean for row in result], ['파란 하늘을 보다 (하이)'] * 2)
        self.assertEqual([row.singers for row in result], [(), ()])

    def test_original_translation_slash_is_not_treated_as_an_inserted_separator(self):
        annotations = self.document([supplied('同じ言葉', '그 / 사람'), supplied('同じ言葉', '그사람')])
        self.assertEqual(annotations.match(self.playback, [Line('同じ言葉', '', '')])[0].korean, '')

    def test_shifted_boundaries_do_not_spread_two_different_singers_across_rows(self):
        annotations = self.document([supplied('青い空を', '파란 하늘을', ['如月千早']),
                                     supplied('見ながら歩こう', '보며 걸어가자', ['天海春香'])])
        result = annotations.match(self.playback, [Line('青い', '', ''), Line('空を見ながら歩こう', '', '')])
        self.assertTrue(all(row.korean for row in result))
        self.assertEqual([row.singers for row in result], [(), ()])

    def test_merged_target_never_extends_a_label_into_unlabelled_source(self):
        annotations = self.document([supplied('青い空', '파란 하늘', ['如月千早']), supplied('白い雲', '흰 구름')])
        result = annotations.match(self.playback, [Line('青い空 白い雲', '', '')])
        self.assertEqual(result[0].korean, '파란 하늘 / 흰 구름')
        self.assertEqual(result[0].singers, ())

    def test_reviewed_spotify_variant_matches_without_rewriting_supplied_original(self):
        row = supplied('じたくないやいやい', '믿고 싶지 않아', ['如月千早'], 'じたくない')
        row['original_variants'] = [{'text': '信じたくない やい やい', 'source': 'local_spotify_original'}]
        annotations = self.document([row])
        result = annotations.match(self.playback, [Line('信じたくない やい やい', '', '')])
        self.assertEqual(result[0].korean, '믿고 싶지 않아')
        self.assertEqual(result[0].reading, '')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['lines'][0]['original'], row['original'])

    def test_invalid_document_is_skipped_without_changing_the_file(self):
        for changes in ({'source': {'kind': 'automatic'}}, {'schema_version': True}, {'artists': []}):
            annotations = self.document([supplied('青い空', '파란 하늘')], **changes)
            before = self.path.read_bytes()
            self.assertEqual(annotations.documents, [])
            self.assertEqual(len(annotations.errors), 1)
            self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':unittest.main()
