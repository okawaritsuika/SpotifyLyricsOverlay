import unittest
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from lyrics import Line, normalized
from tools.verify_catalog_part_candidates import (
    catalog_identity, clean_url, credit_matches, deduplicate_profiles, fan_heading_matches, finalize_result, make_profile, parse_independent,
    parse_search, release_dates, replay, retry_after_seconds, title_matches,
)


class CatalogPartCandidateTests(unittest.TestCase):
    def setUp(self):
        self.lookup = {'天海春香': '天海春香', '如月千早': '如月千早', '星井美希': '星井美希'}
        self.groups = {'unit': {frozenset(('天海春香', '如月千早'))}}
        self.people = ['天海春香', '如月千早']

    def test_tracker_query_is_not_retained_in_sources(self):
        self.assertEqual(clean_url('https://www.uta-net.com/song/1/?target=artist#x'),
                         'https://www.uta-net.com/song/1/')

    def test_search_uses_explicit_title_and_artist_cells(self):
        rows = parse_search('<table class="songlist-table"><tr><td><a href="/song/12/">'
            '<span class="songlist-title">Example title</span><span>Other text</span></a></td>'
            '<td>Unit</td><td>Composer</td><td>unrelated description</td></tr></table>')
        self.assertEqual(rows, [{'title': 'Example title', 'artist': 'Unit',
                                'url': 'https://www.uta-net.com/song/12/'}])

    def test_independent_text_is_isolated_from_metadata_and_ruby_readings(self):
        value = parse_independent('<h2 class="kashi-title">Title</h2><h3 itemprop="recordedAs">'
            '<a itemprop="byArtist" href="/artist/1/">Unit</a></h3><div>発売日：2021/05/19</div>'
            '<div id="kashi_area"><ruby>春<rt>はる</rt></ruby>の空<br>青い雲</div><footer>Extra</footer>')
        self.assertEqual(value['text'], '春の空青い雲')
        self.assertEqual(value['release_date'], '2021-05-19')
        self.assertEqual(value['artist'], 'Unit')

    def test_performer_set_must_match_all_source_people(self):
        self.assertTrue(credit_matches('天海春香(中村繪里子)・如月千早(今井麻美)', self.people, self.lookup, self.groups))
        self.assertFalse(credit_matches('天海春香(中村繪里子)', self.people, self.lookup, self.groups))
        self.assertFalse(credit_matches('天海春香・如月千早・星井美希', self.people, self.lookup, self.groups))
        self.assertFalse(credit_matches('天海春香・Wrong Artist', ['天海春香'], self.lookup, self.groups))
        self.assertFalse(credit_matches('天海春香(今井麻美)', ['天海春香'], self.lookup, self.groups))
        self.assertTrue(credit_matches('天海春香(CV.中村繪里子)', ['天海春香'], self.lookup, self.groups))

    def test_catalog_group_does_not_hide_an_unknown_or_unrelated_extra_artist(self):
        candidate = {'title': 'Title', 'album': 'Album', 'artists': ['Unit']}
        song = {'title': 'Title', 'album': 'Album', 'artists': ['Unit', 'Wrong Artist'], 'kind': 'song'}
        self.assertFalse(catalog_identity(candidate, [song], self.people, self.lookup, self.groups)[0])
        song['artists'] = ['Unit', '中村繪里子', '今井麻美']
        self.assertTrue(catalog_identity(candidate, [song], self.people, self.lookup, self.groups)[0])
        song['artists'].append('長谷川明子')
        self.assertFalse(catalog_identity(candidate, [song], self.people, self.lookup, self.groups)[0])

    def test_documented_former_cv_names_resolve_only_to_their_character(self):
        lookup = {'ジュリア': 'ジュリア', '馬場このみ': '馬場このみ'}
        self.assertTrue(credit_matches('ジュリア(寺川愛美)', ['ジュリア'], lookup, {}))
        self.assertTrue(credit_matches('馬場このみ(高橋未奈美)', ['馬場このみ'], lookup, {}))
        self.assertFalse(credit_matches('馬場このみ(寺川愛美)', ['馬場このみ'], lookup, {}))

    def test_group_roster_requires_an_exact_known_set(self):
        self.assertTrue(credit_matches('Unit', self.people, self.lookup, self.groups))
        self.assertFalse(credit_matches('Unit', ['天海春香'], self.lookup, self.groups))

    def test_master_suffix_requires_explicit_part_source_version(self):
        self.assertTrue(title_matches('Example (M@STER VERSION)', 'Example', 'Example (M@STER VERSION) パート分け'))
        self.assertFalse(title_matches('Example (M@STER VERSION)', 'Example', 'Example パート分け'))
        self.assertFalse(title_matches('Example (Game Version)', 'Example', 'Example (Game Version)'))

    def test_part_source_heading_must_name_same_title_without_an_extra_live_version(self):
        self.assertTrue(fan_heading_matches('Example', 'Example パート分け | Blog'))
        self.assertFalse(fan_heading_matches('Example', 'Example LIVE編 パート分け | Blog'))
        self.assertFalse(fan_heading_matches('Example', 'Another title パート分け'))
        self.assertFalse(fan_heading_matches('Example', 'Example Acoustic 歌割り(パート分け)'))
        self.assertFalse(fan_heading_matches('Example', 'Example Another Story パート分け'))

    def test_unverified_output_artist_is_rejected_even_when_catalog_lineup_matches(self):
        candidate = {'title': 'Title', 'album': 'Album', 'artists': ['Other']}
        songs = [{'title': 'Title', 'album': 'Album', 'artists': ['Unit'], 'kind': 'song'}]
        self.assertFalse(catalog_identity(candidate, songs, self.people, self.lookup, self.groups)[0])
        candidate['artists'] = ['Unit']
        self.assertEqual(catalog_identity(candidate, songs, self.people, self.lookup, self.groups)[0], songs)

    def test_conflicting_duplicate_parts_are_held_instead_of_last_wins(self):
        profile = make_profile({'title': 'Title', 'artists': ['Unit'], 'album': 'Album'},
            'あいう', [('天海春香',)] * 3, 'https://ameblo.jp/a/entry-1.html', 'https://www.uta-net.com/song/1/')
        other = {**profile, 'parts': [{'start': 0, 'end': 3, 'singers': ['如月千早']}]}
        good, conflicts = deduplicate_profiles([profile, other])
        self.assertFalse(good)
        self.assertEqual(len(conflicts), 1)

    def finalize_fixture(self, evidence_url, record_artist='Unit'):
        profile = make_profile({'title': 'Title', 'artists': ['Unit'], 'album': 'Album'},
            'あいう', [('天海春香',)] * 3, 'https://ameblo.jp/a/entry-1.html', 'https://www.uta-net.com/song/1/')
        record = {'album': 'Album', 'artists': [record_artist], 'text_sha256': profile['text_sha256'],
                  'runtime_replay': {'known_singer_rows': 1}}
        evidence = {'part_source_url': evidence_url, 'fan_heading': 'Title パート分け',
            'legend_people': self.people, 'independent_checks': [{
                'url': 'https://www.uta-net.com/song/1/', 'title': 'Title', 'artist': 'Unit',
                'text_sha256': profile['text_sha256'], 'whole_text_alignment': True,
                'release_date': '2021-05-19', 'recordings': [record]}]}
        data = {'profiles': {'recordings': [profile]}, 'outcomes': [{'title': 'Title',
            'status': 'verified', 'reason': '', 'verified_profiles': [], 'evidence': [evidence]}]}
        songs = [{'id': 'recording', 'title': 'Title', 'album': 'Album', 'artists': ['Unit'],
                  'kind': 'song', 'first_release_date': '2021-05-19',
                  'releases': [{'title': 'Album', 'date': '2021-05-19'}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with patch('tools.verify_catalog_part_candidates.person_maps', return_value=(self.lookup, self.groups)), \
                    patch('tools.verify_catalog_part_candidates.SongCatalog') as catalog:
                catalog.return_value.songs = songs
                return finalize_result(path)

    def test_final_gate_cannot_borrow_another_fan_sources_evidence(self):
        result = self.finalize_fixture('https://ameblo.jp/b/entry-2.html')
        self.assertFalse(result['profiles']['recordings'])
        self.assertEqual(result['outcomes'][0]['status'], 'held')

    def test_final_gate_requires_the_same_output_artist_in_runtime_evidence(self):
        result = self.finalize_fixture('https://ameblo.jp/a/entry-1.html', 'Other')
        self.assertFalse(result['profiles']['recordings'])

    def test_final_gate_keeps_linked_metadata_date_and_runtime_evidence(self):
        result = self.finalize_fixture('https://ameblo.jp/a/entry-1.html')
        self.assertEqual(len(result['profiles']['recordings']), 1)
        self.assertTrue(result['final_gate_complete'])

    def test_replay_uses_real_whole_text_hash_and_preserves_unknown_spans(self):
        text = normalized('春の空青い雲')
        profile = make_profile({'title': 'Title', 'artists': ['Unit'], 'album': 'Album'},
            text, [('天海春香',)] * 3 + [()] * 3,
            'https://ameblo.jp/example/entry-1.html?frm=theme', 'https://www.uta-net.com/song/1/')
        result = replay(profile, [Line('春の空', '', ''), Line('青い雲', '', '')])
        self.assertEqual(result['complete_singer_rows'], 1)
        self.assertEqual(result['known_singer_rows'], 1)
        self.assertEqual(replay(profile, [Line('異なる空', '', '')])['known_singer_rows'], 0)
        self.assertFalse(any(key in profile for key in ('lyrics', 'lines', 'text', 'html')))

    def test_catalog_version_and_solo_album_are_rejected(self):
        candidate = {'title': 'Title', 'album': 'SOLO COLLECTION', 'artists': ['Unit']}
        self.assertEqual(catalog_identity(candidate, [], self.people, self.lookup, self.groups)[1],
                         'different_recording_version')

    def test_only_an_explicit_same_recording_release_can_supply_the_original_album(self):
        candidate = {'title': 'Title', 'album': 'Original Album', 'artists': ['Unit']}
        song = {'title': 'Title', 'album': 'Reissue', 'artists': ['Unit'], 'kind': 'song',
                'releases': [{'title': 'Original Album', 'date': '2020-01-01'},
                             {'title': 'Reissue', 'date': '2023-01-01'}]}
        identities, reason = catalog_identity(candidate, [song], self.people, self.lookup, self.groups)
        self.assertFalse(reason)
        self.assertEqual(release_dates(identities, candidate['album']), ['2020-01-01'])
        candidate['album'] = 'Unlisted Album'
        self.assertFalse(catalog_identity(candidate, [song], self.people, self.lookup, self.groups)[0])

    def test_release_anchor_uses_explicit_complete_dates_only(self):
        self.assertEqual(release_dates([{'first_release_date': '2021-05-19', 'releases': [
            {'title': 'Album', 'date': '2022'}, {'title': 'Album', 'date': None},
            {'title': 'Album', 'date': '2021-05-19'}, {'title': 'Other', 'date': '2023-01-01'}]}], 'Album'),
            ['2021-05-19'])
        self.assertFalse(release_dates([{'first_release_date': '2021-05-19',
            'releases': [{'title': 'Other', 'date': '2021-05-19'}]}], 'Album'))

    def test_retry_after_seconds_and_http_date_are_respected(self):
        now = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('120', now), 120)
        self.assertEqual(retry_after_seconds('Fri, 02 Oct 2026 00:00:30 GMT', now), 31)
        self.assertEqual(retry_after_seconds('', now), 15)
        candidate = {'title': 'Title (Game Version)', 'album': 'Album', 'artists': ['Unit']}
        self.assertEqual(catalog_identity(candidate, [], self.people, self.lookup, self.groups)[1],
                         'different_recording_version')


if __name__ == '__main__':
    unittest.main()
