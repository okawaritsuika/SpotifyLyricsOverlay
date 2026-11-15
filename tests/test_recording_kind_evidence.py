from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_sources as sources


RELEASE = 'a2772e7f-8163-4105-b629-3411ff811334'
RELEASE_TITLE = 'THE IDOLM@STER ANIM@TION MASTER 生っすかSPECIAL 弦楽四重奏 初恋組曲'
RECORDING = '71466e07-8e77-4e58-a6a6-0b1d5ffec2f7'
TITLE = '弦楽四重奏「初恋組曲」 序章'


def cached_song(**changes):
    return {'id': 'mb:' + RECORDING, 'title': TITLE, 'kind': 'song', 'type': 'song',
            'source_id': 'musicbrainz', 'artists': ['NAMCO BANDAI Games'],
            'releases': [{'id': RELEASE, 'title': RELEASE_TITLE}], **changes}


class RecordingKindEvidenceTests(unittest.TestCase):
    def test_normalized_official_quartet_recording_is_instrumental(self):
        raw = {'id': RECORDING, 'title': TITLE, 'artist-credit': [{'name': 'NAMCO BANDAI Games'}],
               'releases': [{'id': RELEASE, 'title': RELEASE_TITLE, 'status': 'Official'}]}
        song = sources.normalize_musicbrainz([raw])[0]
        self.assertEqual(song['kind'], 'instrumental')
        self.assertEqual(song['type'], 'instrumental')
        self.assertEqual(song['kind_source_urls'], ['https://columbia.jp/prod-info/COCX-37926-7/'])

    def test_exact_recording_title_and_release_identity_are_all_required(self):
        changes = [{'id': 'mb:' + '0' * 36}, {'title': TITLE + ' (Vocal Ver.)'},
                   {'source_id': 'local'}, {'releases': [{'id': 'another', 'title': RELEASE_TITLE}]},
                   {'releases': [{'id': RELEASE, 'title': 'Another album'}]}, {'releases': []}]
        for edit in changes:
            song = cached_song(**edit)
            before = deepcopy(song)
            with self.subTest(edit=edit):
                self.assertEqual(sources.reclassify_reviewed_kinds([song]), 0)
                self.assertEqual(song, before)

    def test_correction_preserves_recording_metadata_and_is_idempotent(self):
        song = cached_song(duration=121.5, series=['765'], recordings=[{'id': RECORDING}],
                           aliases=['A'], first_release_date='2013-04-10')
        before = deepcopy(song)
        self.assertEqual(sources.reclassify_reviewed_kinds([song]), 1)
        for field in set(before) - {'kind', 'type'}:
            self.assertEqual(song[field], before[field])
        self.assertEqual(sources.reclassify_reviewed_kinds([song]), 0)

    def test_reviewed_resource_covers_only_825_previously_misclassified_records(self):
        path = Path(__file__).resolve().parents[1] / 'resources' / 'song_catalog.json'
        songs = json.loads(path.read_text(encoding='utf-8'))['songs']
        before = deepcopy(songs)
        self.assertEqual(sources.reclassify_reviewed_kinds(songs), 825)
        changed = [(old, new) for old, new in zip(before, songs) if old != new]
        self.assertEqual(sum(new['kind'] == 'instrumental' for _, new in changed), 744)
        self.assertEqual(sum(new['kind'] == 'spoken' for _, new in changed), 81)
        for old, new in changed:
            self.assertEqual({k: v for k, v in old.items() if k not in ('kind', 'type')},
                             {k: v for k, v in new.items() if k not in
                              ('kind', 'type', 'kind_source_urls')})

    def test_reviewed_instrumental_albums_require_exact_recording_and_release(self):
        cases = [
            ('9caf5392-52f4-410d-a1c5-f32471e68281',
             'THE IDOLM@STER SideM INSTRUMENTAL MUSIC COLLECTION 01',
             '291b9852-3f17-4abc-862f-5e51e6231083', 'ASTERISK', 'sidem',
             'https://www.lantis.jp/sidem/releaseinfo/release_LACZ-10057.html'),
            ('42a1ae7e-01ea-46da-891c-db10e8ce0df8',
             'THE IDOLM@STER LIVE THE@TER PERFORMANCE Instrumental 01',
             '90ed2e89-ec49-4f9e-886c-12189ff8f6b4', 'キラメキ進行形', 'million',
             'https://shop.asobistore.jp/products/detail/155188-00-00-Instr_01'),
        ]
        for release_id, album, recording_id, title, series, url in cases:
            raw = {'id': recording_id, 'title': title,
                   'artist-credit': [{'name': 'Composer'}],
                   'releases': [{'id': release_id, 'title': album, 'status': 'Official'}]}
            with self.subTest(album=album):
                normalized = sources.normalize_musicbrainz([raw])[0]
                self.assertEqual(normalized['kind'], 'instrumental')
                self.assertEqual(normalized['series'], [series])
                self.assertEqual(normalized['kind_source_urls'], [url])
                for wrong in ('recording', 'release', 'title'):
                    unreviewed = deepcopy(raw)
                    if wrong == 'recording':
                        unreviewed['id'] = '00000000-0000-4000-8000-000000000001'
                    elif wrong == 'release':
                        unreviewed['releases'][0]['id'] = '00000000-0000-4000-8000-000000000002'
                    else:
                        unreviewed['title'] += ' (Vocal Version)'
                    self.assertEqual(sources.normalize_musicbrainz([unreviewed])[0]['kind'], 'song')

    def test_vocal_recording_with_same_title_keeps_vocal_live_matches(self):
        from live_catalog import LiveCatalog
        from song_catalog import SongCatalog
        songs = SongCatalog().songs
        versions = [song for song in songs if song['title'] == 'キラメキ進行形']
        instrumental = next(song for song in versions if song['id'] ==
                            'mb:90ed2e89-ec49-4f9e-886c-12189ff8f6b4')
        vocals = [song for song in versions if song['kind'] == 'song']
        self.assertTrue(vocals)
        catalog = LiveCatalog()
        self.assertFalse(catalog.for_song(instrumental))
        self.assertTrue(any(catalog.for_song(song) for song in vocals))

    def test_same_quartet_album_vocal_solo_and_unknown_future_tracks_stay_unchanged(self):
        songs = [cached_song(id='mb:90cb5473-c8ee-4ef8-aeba-0b56c8d1e260',
                             title='初恋 ~一章 片想いの桜~ (美希ソロ・リミックス)'),
                 cached_song(title='弦楽四重奏「初恋組曲」 未確認曲')]
        before = deepcopy(songs)
        self.assertEqual(sources.reclassify_reviewed_kinds(songs), 0)
        self.assertEqual(songs, before)

    def test_missing_or_malformed_reviewed_evidence_never_changes_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reviewed.json'
            with patch.object(sources, 'RECORDING_KIND_CORRECTIONS_PATH', path):
                song = cached_song()
                self.assertEqual(sources.reclassify_reviewed_kinds([song]), 0)
                valid = json.loads((Path(__file__).resolve().parents[1] /
                                    'resources/recording_kind_corrections.json').read_text(encoding='utf-8'))
                bad_values = [None, [], {}, {'schema_version': True, 'entries': []},
                              {**valid, 'entries': [None]}, {**valid, 'entries': [valid['entries'][0]] * 2}]
                for field, value in [('kind', 'song'), ('source_urls', ['javascript:bad']),
                                     ('recordings', [{'id': RECORDING, 'title': None}])]:
                    bad = deepcopy(valid)
                    bad['entries'][0][field] = value
                    bad_values.append(bad)
                for value in bad_values:
                    path.write_text(json.dumps(value), encoding='utf-8')
                    before = deepcopy(song)
                    with self.subTest(value=value):
                        self.assertEqual(sources.reclassify_reviewed_kinds([song]), 0)
                        self.assertEqual(song, before)


if __name__ == '__main__':
    unittest.main()
