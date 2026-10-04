import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from lyric_parts import LyricParts, text_fingerprint
from lyrics import Line, normalized
from media import Playback
from user_annotations import UserAnnotations


class LyricPartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'lyric_parts.json'
        self.playback = Playback('Shared Song', 'Unit', 'Album', duration=180)
        self.lines = [Line(text, '', '') for text in ('青い空', '同じ言葉', '白い雲', '同じ言葉')]

    def tearDown(self):self.temp.cleanup()

    def recording(self, **changes):
        fingerprint, length = text_fingerprint(self.lines)
        position, parts = 0, []
        for line, singer in zip(self.lines, ('如月千早', '天海春香', '如月千早', '如月千早')):
            end = position + len(normalized(line.original))
            parts.append({'start': position, 'end': end, 'singers': [singer]})
            position = end
        return {'title': 'Shared Song', 'artists': ['Unit'], 'album': 'Album',
                'text_sha256': fingerprint, 'text_length': length, 'parts': parts,
                'sources': [{'url': 'https://example.org/parts', 'kind': 'fan_part_transcription',
                             'note': 'Explicit character part labels.'}], **changes}

    def load(self, *recordings):
        self.path.write_text(json.dumps({'schema_version': 1, 'recordings': recordings}), encoding='utf-8')
        return LyricParts(resource_path=self.path)

    def test_full_text_positions_distinguish_repeated_phrases(self):
        catalog = self.load(self.recording())
        names = catalog.match(self.playback, self.lines)
        self.assertEqual(names[1], ('天海春香',))
        self.assertEqual(names[3], ('如月千早',))

    def test_independent_text_evidence_is_distinct_from_a_part_credit(self):
        recording = self.recording()
        recording['sources'].append({'url': 'https://example.org/licensed-lyrics',
            'kind': 'independent_lyrics', 'note': 'Whole text only; not a singer part credit.'})
        catalog = self.load(recording)
        self.assertFalse(catalog.errors)
        self.assertTrue(any(catalog.match(self.playback, self.lines)))

    def test_line_wrap_changes_preserve_positions_and_explicit_singer_order(self):
        catalog = self.load(self.recording())
        split = [Line(text, '', '') for text in ('青い', '空同じ言葉', '白い雲同じ言葉')]
        self.assertEqual(catalog.match(self.playback, split),
                         (('如月千早',), ('如月千早', '天海春香'), ('如月千早',)))

    def test_unknown_gap_does_not_label_the_whole_row(self):
        record = self.recording()
        record['parts'][0]['end'] -= 1
        catalog = self.load(record)
        self.assertEqual(catalog.match(self.playback, self.lines)[0], ())
        self.assertEqual(catalog.match(self.playback, self.lines)[1], ('天海春香',))

    def test_one_different_character_or_short_edit_rejects_the_document(self):
        catalog = self.load(self.recording())
        for rows in (self.lines[:-1], [Line('赤い空', '', ''), *self.lines[1:]]):
            self.assertFalse(any(catalog.match(self.playback, rows)))

    def test_known_fragment_is_returned_only_when_partial_is_requested(self):
        record = self.recording()
        record['parts'][0]['end'] -= 1
        record['parts'] = record['parts'][:-1]
        catalog = self.load(record)
        self.assertEqual(catalog.match(self.playback, self.lines, allow_partial=True)[0], ('如月千早',))
        self.assertFalse(catalog.match(self.playback, self.lines, allow_partial=True)[-1])
        rows = UserAnnotations(self.temp.name).match(self.playback, self.lines)
        self.assertTrue(rows[0].singer_partial)
        self.assertEqual(rows[0].singers, ('如月千早',))
        self.assertFalse(rows[1].singer_partial)
        self.assertFalse(rows[-1].singers)
        self.assertFalse(rows[-1].singer_partial)

    def test_verified_reference_can_project_one_typo_but_never_unrelated_text(self):
        self.lines = [Line('青い空に光る星を見つめながら君と歩いて行く' * 6, '', '')]
        catalog = self.load(self.recording())
        changed = [Line(self.lines[0].original.replace('星', '月', 1), '', '')]
        original = tuple(changed)
        self.assertFalse(any(catalog.match(self.playback, changed)))
        self.assertEqual(catalog.match(self.playback, changed, reference_lines=self.lines), (('如月千早',),))
        self.assertEqual(tuple(changed), original)
        wrong = [Line('違う言葉' * 30, '', '')]
        self.assertFalse(any(catalog.match(self.playback, wrong, reference_lines=self.lines)))
        self.assertFalse(any(catalog.match(self.playback, changed, reference_lines=wrong)))
        other = Playback('Shared Song (Live)', 'Unit', 'Album')
        self.assertFalse(any(catalog.match(other, changed, reference_lines=self.lines)))

    def test_reference_projection_preserves_partial_marker(self):
        self.lines = [Line('青い空に光る星を見つめながら君と歩いて行く' * 6, '', '')]
        record = self.recording()
        record['parts'][0]['end'] -= 10
        self.load(record)
        changed = [Line(self.lines[0].original.replace('星', '月', 1), '', '')]
        row = UserAnnotations(self.temp.name).match(self.playback, changed, reference_lines=self.lines)[0]
        self.assertEqual(row.singers, ('如月千早',))
        self.assertTrue(row.singer_partial)

    def test_metadata_versions_and_artists_must_match(self):
        catalog = self.load(self.recording())
        for title, artist, album in [('Shared Song (Live)', 'Unit', 'Album'),
                                     ('Shared Song', 'Other Unit', 'Album'),
                                     ('Shared Song', 'Unit', 'Solo Album'),
                                     ('Shared Song', 'Unit', '')]:
            self.assertFalse(any(catalog.match(Playback(title, artist, album), self.lines)))

    def test_confirmed_display_artists_can_supply_a_matching_credit(self):
        catalog = self.load(self.recording())
        other = Playback('Shared Song', 'First Artist', 'Album')
        self.assertFalse(any(catalog.match(other, self.lines)))
        self.assertTrue(all(catalog.match(other, self.lines, ('Unit', 'First Artist'))))

    def test_reviewed_digital_release_alias_keeps_full_recording_guards(self):
        canonical = 'THE IDOLM@STER SideM 5th ANNIVERSARY DISC 02 DRAMATIC STARS & 神速一魂 & F-LAGS'
        digital = 'THE IDOLM@STER SideM 5th ANNIVERSARY 02'
        record = self.recording(album=canonical, album_aliases=[digital])
        record['sources'].append({'url': 'https://example.org/recording',
            'kind': 'recording_metadata', 'note': 'Reviewed original and digital release of this recording.'})
        catalog = self.load(record)
        self.assertFalse(catalog.errors)
        playback = Playback('Shared Song', 'Unit', digital)
        self.assertTrue(all(catalog.match(playback, self.lines)))
        self.assertTrue(all(catalog.match(Playback('Shared Song', 'Unit', canonical), self.lines)))
        for album in (digital.replace('02', '03'), digital + ' (Live)',
                      digital + ' SOLO COLLECTION', 'THE IDOLM@STER SideM 10th ANNIVERSARY P@SSION 01', ''):
            self.assertFalse(any(catalog.match(Playback('Shared Song', 'Unit', album), self.lines)))
        self.assertFalse(any(catalog.match(Playback('Shared Song (Live)', 'Unit', digital), self.lines)))
        self.assertFalse(any(catalog.match(Playback('Shared Song', 'Other Unit', digital), self.lines)))
        self.assertFalse(any(catalog.match(playback, self.lines[:-1])))
        self.assertFalse(any(catalog.match(playback, [Line('赤い空', '', ''), *self.lines[1:]])))

    def test_alias_does_not_override_a_conflicting_direct_album_profile(self):
        first = self.recording(album_aliases=['Digital Album'])
        second = self.recording(album='Digital Album')
        second['parts'][1]['singers'] = ['星井美希']
        catalog = self.load(first, second)
        rows = catalog.match(Playback('Shared Song', 'Unit', 'Digital Album'), self.lines)
        self.assertEqual(rows[0], ('如月千早',))
        self.assertFalse(rows[1])

    def test_malformed_album_aliases_are_rejected(self):
        for aliases in ('Album', [], [''], ['...'], ['Album\nOther'], [1], ['Album'] * 21):
            catalog = self.load(self.recording(album_aliases=aliases))
            self.assertEqual(catalog.recordings, [])
            self.assertEqual(len(catalog.errors), 1)
        self.assertEqual(self.load(self.recording(album='', album_aliases=['Digital Album'])).recordings, [])

    def test_conflicting_positive_sources_are_not_arbitrarily_selected(self):
        first, second = self.recording(), self.recording()
        second['parts'][1]['singers'] = ['星井美希']
        catalog = self.load(first, second)
        result = catalog.match(self.playback, self.lines)
        self.assertEqual(result[1], ())
        self.assertEqual(result[0], ('如月千早',))

    def test_missing_source_span_does_not_negate_a_complete_explicit_fact(self):
        first, second = self.recording(), self.recording()
        second['parts'] = second['parts'][1:]
        catalog = self.load(first, second)
        self.assertEqual(catalog.match(self.playback, self.lines)[0], ('如月千早',))

    def test_malformed_and_overlapping_records_are_skipped(self):
        for changes in ({'text_length': True}, {'text_sha256': 'bad'},
                        {'parts': [{'start': 0, 'end': 99, 'singers': ['如月千早']}]},
                        {'parts': [{'start': 0, 'end': 3, 'singers': ['如月千早']},
                                   {'start': 2, 'end': 4, 'singers': ['天海春香']}]},
                        {'sources': [{'url': 'file:///private', 'kind': 'official'}]}):
            catalog = self.load(self.recording(**changes))
            self.assertEqual(catalog.recordings, [])
            self.assertEqual(len(catalog.errors), 1)

    def test_catalog_has_no_original_text_and_never_changes_input_lines(self):
        original = tuple(self.lines)
        catalog = self.load(self.recording())
        catalog.match(self.playback, self.lines)
        self.assertEqual(tuple(self.lines), original)
        for line in original:self.assertNotIn(line.original, self.path.read_text(encoding='utf-8'))

    def test_parts_are_available_without_a_user_translation_directory(self):
        self.load(self.recording())
        annotations = UserAnnotations(self.temp.name)
        rows = annotations.match(self.playback, self.lines)
        self.assertEqual(rows[1].singers, ('天海春香',))
        self.assertTrue(all(not row.korean and not row.reading for row in rows))


if __name__ == '__main__':unittest.main()
