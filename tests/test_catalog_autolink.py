import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_autolink import automatic_candidate, cached_pool, local_matches, _recording_aliases, _voice_aliases
from explorer_library import ExplorerLibrary, build_performer_lookup


def song(**changes):
    return {'id': 'song:one', 'title': 'Song!', 'artists': ['Idol'],
            'album': 'Album', 'duration': 240, **changes}


def track(**changes):
    return {'id': 'a' * 22, 'title': 'Song!', 'artists': ['Idol'],
            'album': 'Album', 'duration': 240, 'is_playable': None, **changes}


PEOPLE = [
    {'name': 'Idol', 'kind': 'idol', 'aliases': ['아이돌'], 'cv': ['Voice']},
    {'name': 'Other Idol', 'kind': 'idol', 'cv': ['Other Voice']},
    {'name': 'Unit', 'kind': 'group', 'aliases': ['유닛'], 'members': ['Idol', 'Other Idol']},
]


class AutoLinkTests(unittest.TestCase):
    def test_exact_recording_auto_links_without_claiming_playability(self):
        source = track()
        result = automatic_candidate(song(), [source])
        self.assertEqual(result['id'], source['id'])
        self.assertIsNone(result['is_playable'])
        self.assertEqual(result['verification'], 'automatic_metadata')
        self.assertNotIn('verification', source)

    def test_preserves_reported_playability_but_not_truthy_malformed_values(self):
        for value in (True, False, None, 'true'):
            result = automatic_candidate(song(), [track(is_playable=value)])
            self.assertIs(result['is_playable'], value if isinstance(value, bool) else None)

    def test_unicode_width_and_whitespace_are_safe(self):
        result = automatic_candidate(song(), [track(title='Ｓｏｎｇ！', album=' Album ')])
        self.assertIsNotNone(result)

    def test_version_delimiters_and_quotes_preserve_the_full_recording_label(self):
        for source, target in [('Song! (M@STER VERSION)', 'Song! - M@STER VERSION'),
                               ('Song! [Instrumental]', 'Song! - Instrumental'),
                               ('Song! (Off Vocal)', 'Song! - Off Vocal'),
                               ('Song!（オリジナル・カラオケ）', 'Song! - オリジナル・カラオケ'),
                               ('Song! (Idol Ver.)', 'Song! - Idol Ver.'),
                               ('"W"onderful', '“W”onderful')]:
            with self.subTest(source=source):
                self.assertIsNotNone(automatic_candidate(song(title=source), [track(title=target)]))
        for source, target in [('Song!', 'Song! - Live'),
                               ('Song! (Live)', 'Song! - Off Vocal'),
                               ('Song! (Idol Ver.)', 'Song! - Other Idol Ver.'),
                               ('Song! (M@STER VERSION)', 'Song! - GAME VERSION'),
                               ('Song! (Subtitle)', 'Song! - Subtitle'),
                               ('Song!', 'Song?')]:
            with self.subTest(source=source, target=target):
                self.assertIsNone(automatic_candidate(song(title=source), [track(title=target)]))

    def test_another_documented_release_of_the_same_recording_is_allowed(self):
        recording = song(releases=[{'title': 'Original Album'}, {'title': 'Digital Album'}])
        self.assertIsNotNone(automatic_candidate(recording, [track(album='Digital Album')]))
        self.assertIsNone(automatic_candidate(recording, [track(album='Unlisted Album')]))
        self.assertIsNone(automatic_candidate(recording, [track(album='Digital Album', duration=244)]))
        self.assertEqual(automatic_candidate(recording,
            [track(album='Original Album'), track(id='b'*22, album='Digital Album')])['id'], 'a'*22)

    def test_documented_release_can_supply_album_evidence_when_primary_is_missing(self):
        recording = song(album='', duration=None, releases=[{'title': 'Digital Album'}])
        self.assertIsNotNone(automatic_candidate(recording, [track(album='Digital Album')]))
        self.assertIsNone(automatic_candidate(recording, [track(album='')]))

    def test_album_typesetting_preserves_edition_and_version_identity(self):
        for source, target in [('Album -BEST (INSTRUMENTAL Edition)', 'Album -BEST【INSTRUMENTAL Edition】'),
                               ('C’mon! Join Us', "C'mon! Join Us"),
                               ('EPISODE: 10 Legenders', 'EPISODE：10 Legenders')]:
            with self.subTest(source=source):
                self.assertIsNotNone(automatic_candidate(song(album=source), [track(album=target)]))
                self.assertIsNotNone(automatic_candidate(song(album='Other', releases=[{'title': source}]),
                                                        [track(album=target)]))
        for source, target in [('Album (2022 Ver.)', 'Album (2023 Ver.)'),
                               ('Album (INSTRUMENTAL Edition)', 'Album【VOCAL Edition】'),
                               ('Album Disc 1', 'Album Disc 2'),
                               ('Album', 'Album (Live)'), ('Album!', 'Album?')]:
            with self.subTest(source=source, target=target):
                self.assertIsNone(automatic_candidate(song(album=source), [track(album=target)]))

    def test_reviewed_voyager_aliases_are_bound_to_both_ids_and_full_titles(self):
        entries = _recording_aliases()
        self.assertEqual(len(entries), 4)
        for entry in entries.values():
            recording = song(id=entry['catalog_id'], title=entry['catalog_title'], album='VOY@GER')
            candidate = track(id=entry['spotify_id'], title=entry['spotify_title'], album='VOY@GER')
            self.assertEqual(automatic_candidate(recording, [candidate])['id'], entry['spotify_id'])
            self.assertEqual(local_matches([recording], [candidate])[recording['id']]['id'], entry['spotify_id'])
            self.assertEqual(entry['source_url'], 'https://catalog.bandainamcomusiclive.co.jp/release/69669/')
            for changed_song, changed_track in [
                ({**recording, 'id': 'unreviewed'}, candidate),
                ({**recording, 'title': 'VOY@GER (Other Idol Ver.)'}, candidate),
                (recording, {**candidate, 'id': 'z'*22}),
                (recording, {**candidate, 'title': 'VOY@GER (Live)'})]:
                self.assertIsNone(automatic_candidate(changed_song, [changed_track]))
                self.assertEqual(local_matches([changed_song], [changed_track]), {})

    def test_reviewed_title_alias_keeps_all_other_recording_boundaries_and_ambiguity(self):
        entry = next(iter(_recording_aliases().values()))
        recording = song(id=entry['catalog_id'], title=entry['catalog_title'], album='VOY@GER')
        candidate = track(id=entry['spotify_id'], title=entry['spotify_title'], album='VOY@GER')
        for changes in ({'album': 'Other'}, {'album': ''}, {'artists': ['Other Idol']},
                        {'duration': 243}, {'duration': None}):
            self.assertIsNone(automatic_candidate(recording, [{**candidate, **changes}]))
        competing = {**candidate, 'id': 'z'*22, 'title': recording['title']}
        self.assertIsNone(automatic_candidate(recording, [candidate, competing]))
        self.assertEqual(local_matches([recording], [candidate, competing]), {})

    def test_explicit_boundary_mismatches_are_rejected(self):
        variants = [track(title='Song! (Live)'), track(title='Song! - Solo ver.'),
                    track(album='Best Album'), track(album=''), track(duration=0),
                    track(duration=243), track(artists=['Other Idol']),
                    track(artists=['Idol', 'Other Idol']), track(id='invalid')]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertIsNone(automatic_candidate(song(), [candidate]))

    def test_no_recording_evidence_is_held(self):
        self.assertIsNone(automatic_candidate(song(album='', duration=None), [track()]))
        self.assertIsNotNone(automatic_candidate(song(album='', duration=240), [track()]))
        self.assertIsNone(automatic_candidate(song(artists=[]), [track()]))

    def test_equivalent_recording_ids_have_stable_preference(self):
        self.assertEqual(automatic_candidate(song(), [track(id='b'*22), track()])['id'], 'a'*22)
        self.assertIsNotNone(automatic_candidate(song(), [track(), track()]))

    def test_equivalent_release_prefers_primary_album_then_playability(self):
        recording = song(releases=[{'title': 'Digital Album'}])
        candidates = [track(id='b'*22, is_playable=True), track(is_playable=None),
                      track(id='c'*22, album='Digital Album', is_playable=True)]
        self.assertEqual(automatic_candidate(recording, candidates)['id'], 'b'*22)
        candidates[0]['is_playable'] = False
        self.assertEqual(automatic_candidate(recording, candidates)['id'], 'a'*22)
        self.assertEqual(automatic_candidate(recording, list(reversed(candidates)))['id'], 'a'*22)

    def test_duplicate_ids_need_matching_full_title_credits_and_close_lengths(self):
        self.assertIsNone(automatic_candidate(song(duration=None), [track(), track(id='b'*22)]))
        self.assertIsNone(automatic_candidate(song(), [track(duration=238), track(id='b'*22, duration=242)]))
        recording = song(artists=['Idol'])
        self.assertIsNone(automatic_candidate(recording,
            [track(artists=['Idol (CV. Voice)']), track(id='b'*22, artists=['Idol (CV. Old Voice)'])], PEOPLE))

    def test_known_solo_performer_suffix_is_grounded_in_recording_credits(self):
        for title in ('Song! (Idol Ver.)', 'Song! - 아이돌 Version'):
            candidate = track(title=title, artists=['Idol (CV. Voice)'])
            self.assertIsNotNone(automatic_candidate(song(artists=['Idol', 'Voice']), [candidate], PEOPLE))
            self.assertIn('song:one', local_matches([song(artists=['Idol', 'Voice'])], [candidate], PEOPLE))
        for artists in (['Unit'], ['Other Idol']):
            self.assertIsNone(automatic_candidate(song(artists=artists),
                [track(title='Song! (Idol Ver.)')], PEOPLE))
        for title in ('Song! (Unknown Ver.)', 'Song! (Live Ver.)', 'Song! (Idol Remix)'):
            self.assertIsNone(automatic_candidate(song(), [track(title=title)], PEOPLE))
        self.assertIsNone(automatic_candidate(song(), [track(title='Song! (Idol Ver.)')]))

    def test_cv_and_explicit_aliases_do_not_create_extra_singers(self):
        result = automatic_candidate(song(artists=['Idol', 'Voice']),
                                     [track(artists=['아이돌 (CV. Voice)'])], PEOPLE)
        self.assertIsNotNone(result)
        self.assertIsNone(automatic_candidate(song(artists=['Idol']),
                          [track(artists=['Idol', 'Other Idol'])], PEOPLE))

    def test_explicit_recording_cv_can_explain_a_missing_historical_voice(self):
        result = automatic_candidate(song(artists=['Idol', 'Old Voice']),
                                     [track(artists=['아이돌 (CV. Old Voice)'])], PEOPLE)
        self.assertIsNotNone(result)
        self.assertIsNone(automatic_candidate(song(artists=['Idol', 'Different Voice']),
                         [track(artists=['Idol (CV. Old Voice)'])], PEOPLE))
        self.assertIsNone(automatic_candidate(song(artists=['Idol', 'Other Voice']),
                         [track(artists=['Idol', 'Other Idol (CV. Other Voice)'])], PEOPLE))

    def test_conflicting_old_and_new_voice_recordings_remain_distinct(self):
        for names in (['Idol (CV. Voice)'], ['Idol', 'Voice']):
            self.assertIsNone(automatic_candidate(song(artists=names),
                             [track(artists=['Idol (CV. Old Voice)'])], PEOPLE))

    def test_registered_stage_names_identify_one_voice_without_merging_other_voices(self):
        people = [{'name': 'Idol', 'kind': 'idol', 'cv': ['髙橋ミナミ']}]
        self.assertIsNotNone(automatic_candidate(song(artists=['Idol', '高橋未奈美']),
                            [track(artists=['Idol (CV. 髙橋ミナミ)'])], people))
        self.assertIsNotNone(automatic_candidate(song(artists=['Idol (CV. 高橋未奈美)']),
                            [track(artists=['Idol (CV. Minami Takahashi)'])], people))
        self.assertIsNone(automatic_candidate(song(artists=['Idol (CV. 高橋未奈美)']),
                         [track(artists=['Idol (CV. Other Voice)'])], people))

    def test_voice_aliases_require_existing_sources_and_reject_shared_aliases(self):
        entries = [
            {'kind': 'voice_actor', 'name': 'Voice A', 'aliases': ['Old A', 'Shared'], 'source_urls': ['https://example.test/a']},
            {'kind': 'voice_actor', 'name': 'Voice B', 'aliases': ['Shared'], 'source_urls': ['https://example.test/b']},
            {'kind': 'voice_actor', 'name': 'Voice C', 'aliases': ['Unverified']},
            {'kind': 'group', 'name': 'Unit', 'aliases': ['Unit Alias'], 'source_urls': ['https://example.test/unit']}]
        _voice_aliases.cache_clear()
        self.addCleanup(_voice_aliases.cache_clear)
        with patch('catalog_autolink.Path.read_text', return_value=json.dumps({'schema_version': 1, 'entries': entries})):
            aliases = _voice_aliases()
        self.assertEqual(aliases['olda'], 'voicea')
        for key in ('shared', 'unverified', 'unitalias'):self.assertNotIn(key, aliases)

    def test_complete_explicit_group_is_equivalent_but_solo_is_not(self):
        result = automatic_candidate(song(artists=['유닛']),
                         [track(artists=['Idol (CV.Voice)', 'Other Idol (CV.Other Voice)'])], PEOPLE)
        self.assertIsNotNone(result)
        self.assertIsNone(automatic_candidate(song(artists=['Unit']), [track()], PEOPLE))

    def test_incomplete_group_roster_cannot_be_guessed(self):
        people = copy.deepcopy(PEOPLE)
        people[-1]['members'].append('Unknown Idol')
        self.assertIsNone(automatic_candidate(song(artists=['Unit']),
                            [track(artists=['Idol', 'Other Idol'])], people))

    def test_bulk_match_uses_title_pool_without_touching_songs(self):
        songs = [song(), song(id='song:other', title='Other')]
        before = copy.deepcopy(songs)
        result = local_matches(songs, [track()])
        self.assertEqual(set(result), {'song:one'})
        self.assertEqual(songs, before)

    def test_unique_title_and_album_correct_incomplete_or_wrong_spotify_credits(self):
        recording = song(artists=['Unit'])
        for artists in (['Idol'], ['Wrong Artist']):
            with self.subTest(artists=artists):
                candidate = track(artists=artists)
                self.assertIsNone(automatic_candidate(recording, [candidate], PEOPLE))
                result = local_matches([recording], [candidate], PEOPLE)
                self.assertEqual(result[recording['id']]['id'], candidate['id'])
                self.assertEqual(result[recording['id']]['artists'], artists)

    def test_different_singers_can_link_their_distinct_album_recordings(self):
        songs = [song(id='unit', artists=['Unit']),
                 song(id='solo', artists=['Idol'], album='Solo Album')]
        candidates = [track(artists=['Wrong Artist']),
                      track(id='b'*22, artists=['Wrong Artist'], album='Solo Album')]
        result = local_matches(songs, candidates, PEOPLE)
        self.assertEqual(result['unit']['id'], 'a'*22)
        self.assertEqual(result['solo']['id'], 'b'*22)

    def test_shared_album_solo_variants_cannot_use_wrong_credits(self):
        songs = [song(id='one', artists=['Idol']), song(id='two', artists=['Other Idol'])]
        self.assertEqual(local_matches(songs, [track(artists=['Wrong Artist'])], PEOPLE), {})
        result = local_matches(songs, [track()], PEOPLE)
        self.assertEqual(set(result), {'one'})

    def test_unique_album_override_preserves_version_duration_and_explicit_cv_boundaries(self):
        recording = song(artists=['Idol (CV. Voice)'])
        for candidate in (track(title='Song! (Live)', artists=['Wrong Artist']),
                          track(album='Other', artists=['Wrong Artist']),
                          track(album='', artists=['Wrong Artist']),
                          track(duration=243, artists=['Wrong Artist']),
                          track(duration=None, artists=['Wrong Artist']),
                          track(artists=['Idol (CV. Old Voice)'])):
            with self.subTest(candidate=candidate):
                self.assertEqual(local_matches([recording], [candidate], PEOPLE), {})

    def test_unique_album_override_requires_one_spotify_recording_id(self):
        self.assertEqual(local_matches([song(artists=['Unit'])],
            [track(), track(id='b'*22, artists=['Wrong Artist'])], PEOPLE), {})

    def test_documented_release_can_correct_credits_and_keeps_shared_release_ambiguity(self):
        recording = song(artists=['Unit'], releases=[{'title': 'Digital Album'}])
        candidate = track(album='Digital Album', artists=['Idol'])
        self.assertIn(recording['id'], local_matches([recording], [candidate], PEOPLE))
        other = song(id='solo', artists=['Other Idol'], album='Digital Album')
        self.assertEqual(local_matches([recording, other], [candidate], PEOPLE), {})

    def test_unique_album_override_does_not_expand_reviewed_title_alias(self):
        entry = next(iter(_recording_aliases().values()))
        recording = song(id=entry['catalog_id'], title=entry['catalog_title'], album='VOY@GER')
        candidate = track(id=entry['spotify_id'], title=entry['spotify_title'],
                          album='VOY@GER', artists=['Wrong Artist'])
        self.assertEqual(local_matches([recording], [candidate]), {})

    def test_bulk_matching_reuses_person_indexes_and_full_version_keys(self):
        songs = [song(id=f'song:{index}', title='Song! (Live)', artists=['Unit']) for index in range(20)]
        source = track(title='Song! - Live', artists=['Idol', 'Other Idol'])
        with patch('catalog_autolink.build_performer_lookup', wraps=build_performer_lookup) as lookup:
            result = local_matches(songs, [source], PEOPLE)
        self.assertEqual(len(result), 20)
        self.assertEqual(lookup.call_count, 3)

    def test_local_cache_must_validate_request_and_never_claim_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {'title': 'Song!', 'artist': 'Idol', 'album': 'Album', 'duration': 240}
            values = {'version': 1, 'entries': [
                {'request': request, 'track': track(is_playable=True)},
                {'request': request, 'track': track(id='b'*22, title='Wrong Song')}]}
            Path(directory, 'artist_metadata.json').write_text(json.dumps(values))
            candidates = cached_pool(directory)
            self.assertEqual(len(candidates), 1)
            self.assertIsNone(candidates[0]['is_playable'])

    def test_public_pool_adds_validated_recordings_without_account_or_playability_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            source = 'https://open.spotify.com/album/' + 'b' * 22
            request = {'title': 'Song!', 'artist': 'Idol', 'album': 'Album', 'duration': 240}
            Path(directory, 'artist_metadata.json').write_text(json.dumps({'version': 1,
                'entries': [{'request': request, 'track': track()}]}))
            Path(directory, 'spotify_public_tracks.json').write_text(json.dumps({'schema_version': 1,
                'tracks': [track(id='c'*22, source_url=source, is_playable=True,
                                 checked_at='2026-10-03', authorization='must not survive')]}))
            candidates = cached_pool(directory)
            self.assertEqual([item['id'] for item in candidates], ['a'*22, 'c'*22])
            self.assertEqual(candidates[1]['source_url'], source)
            self.assertIsNone(candidates[1]['is_playable'])
            self.assertEqual(candidates[1]['checked_at'], '')
            self.assertNotIn('authorization', candidates[1])

    def test_public_pool_skips_malformed_metadata_and_non_spotify_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            source = 'https://open.spotify.com/track/' + 'a' * 22
            variants = [track(source_url=source), track(source_url=source, duration=True),
                        track(source_url=source, artists=[]), track(source_url=source, id='bad'),
                        track(source_url=source, title='Song!\n'), track(source_url=source, artists=[{}]),
                        track(source_url=source.replace('https:', 'http:')),
                        track(source_url=source.replace('open.spotify.com', 'open.spotify.com.example.org')),
                        track(source_url=source.replace('open.spotify.com', 'user@open.spotify.com')),
                        track(source_url='https://open.spotify.com/unknown'), track(), None]
            Path(directory, 'spotify_public_tracks.json').write_text(json.dumps(
                {'schema_version': 1, 'tracks': variants}))
            self.assertEqual(len(cached_pool(directory)), 1)

    def test_public_artist_page_can_provide_a_complete_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            source = 'https://open.spotify.com/artist/' + 'b' * 22
            Path(directory, 'spotify_public_tracks.json').write_text(json.dumps({'schema_version': 1,
                'tracks': [track(source_url=source), track(source_url=source, artists=[])]}))
            candidates = cached_pool(directory)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]['source_url'], source)
            self.assertIsNone(candidates[0]['is_playable'])

    def test_public_pool_supports_more_than_playback_cache_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = [track(id=f'{index:022}', source_url='https://open.spotify.com/track/'+f'{index:022}')
                       for index in range(513)]
            Path(directory, 'spotify_public_tracks.json').write_text(json.dumps(
                {'schema_version': 1, 'tracks': entries}))
            self.assertEqual(len(cached_pool(directory)), 513)

    def test_invalid_or_oversized_public_pool_keeps_playback_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {'title': 'Song!', 'artist': 'Idol', 'album': 'Album', 'duration': 240}
            Path(directory, 'artist_metadata.json').write_text(json.dumps({'version': 1,
                'entries': [{'request': request, 'track': track()}]}))
            path = Path(directory, 'spotify_public_tracks.json')
            for content in ('invalid json', '[]', '{"schema_version": 2, "tracks": []}',
                            '{"schema_version": 1, "tracks": {}}'):
                path.write_text(content)
                self.assertEqual(len(cached_pool(directory)), 1)
            path.write_text(json.dumps({'schema_version': 1, 'tracks': [track(), track()]}))
            with patch('catalog_autolink.MAX_PUBLIC_TRACKS', 1):
                self.assertEqual(len(cached_pool(directory)), 1)
            with patch('catalog_autolink.MAX_PUBLIC_BYTES', 10):
                self.assertEqual(len(cached_pool(directory)), 1)


class AutoLinkLibraryTests(unittest.TestCase):
    def test_auto_link_preserves_manual_and_unlink_optout_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            library = ExplorerLibrary(directory)
            library.link_spotify('song:one', track(id='b'*22))
            self.assertFalse(library.link_automatic('song:one', track()))
            self.assertEqual(library.linked_track('song:one')['id'], 'b'*22)
            library.unlink_spotify('song:one')
            library = ExplorerLibrary(directory)
            self.assertFalse(library.link_automatic('song:one', track()))
            self.assertIsNone(library.linked_track('song:one'))
            library.link_spotify('song:one', track())
            self.assertEqual(library.value['automatic_link_exclusions'], [])

    def test_new_auto_link_is_saved_and_returns_true_once(self):
        with tempfile.TemporaryDirectory() as directory:
            library = ExplorerLibrary(directory)
            library.toggle_favorite('song:one')
            self.assertTrue(library.link_automatic('song:one', automatic_candidate(song(), [track()])))
            self.assertFalse(library.link_automatic('song:one', track()))
            reopened = ExplorerLibrary(directory)
            self.assertEqual(reopened.linked_track('song:one')['verification'], 'automatic_metadata')
            self.assertTrue(reopened.is_favorite('song:one'))


if __name__ == '__main__':unittest.main()
