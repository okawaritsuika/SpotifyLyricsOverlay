"""Personal discovery state survives catalog changes and weights works fairly."""
from collections import Counter
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from explorer_library import (ExplorerLibrary, availability, matches_query,
                              related_songs, spotify_url, artist_key, performers,
                              build_performer_lookup, lookup_performer, group_duplicate_songs,
                              resolve_group_members, SONG_SORTS, song_release_date, sort_songs)


class ExplorerLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = ExplorerLibrary(self.root)
        self.track = {'id': 'a' * 22, 'title': 'Song (Live)', 'artists': ['Singer'],
                      'album': 'Live Album', 'duration': 180, 'is_playable': True,
                      'restriction': '', 'checked_at': '2026-10-02', 'market': 'KR'}

    def tearDown(self):self.temp.cleanup()

    def test_favorites_recordings_and_history_survive_restart_and_catalog_replacement(self):
        self.library.toggle_favorite('public:stable-id')
        self.library.link_spotify('public:stable-id', self.track)
        self.library.remember('public:stable-id')
        (self.root / 'song_catalog.json').write_text('{"songs": []}')
        reopened = ExplorerLibrary(self.root)
        self.assertTrue(reopened.is_favorite('public:stable-id'))
        self.assertEqual(reopened.linked_track('public:stable-id')['title'], 'Song (Live)')
        self.assertEqual(reopened.linked_track('public:stable-id')['album'], 'Live Album')
        self.assertEqual(reopened.value['recent'], ['public:stable-id'])
        reopened.toggle_favorite('public:stable-id');reopened.unlink_spotify('public:stable-id')
        again = ExplorerLibrary(self.root)
        self.assertFalse(again.is_favorite('public:stable-id'))
        self.assertIsNone(again.linked_track('public:stable-id'))
        self.assertEqual(again.value['recent'], ['public:stable-id'])

    def test_failed_save_does_not_mutate_personal_choices(self):
        self.library.toggle_favorite('first')
        before = self.library.path.read_bytes()
        with patch('explorer_library.atomic_json', side_effect=OSError('disk unavailable')):
            for action in (lambda: self.library.toggle_favorite('second'),
                           lambda: self.library.link_spotify('first', self.track),
                           lambda: self.library.remember('first')):
                with self.assertRaises(OSError):action()
        self.assertEqual(self.library.value['favorites'], ['first'])
        self.assertEqual(self.library.value['spotify'], {})
        self.assertEqual(self.library.value['recent'], [])
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_corrupt_library_is_preserved_and_never_replaced_with_empty_state(self):
        self.library.path.write_text('{broken')
        before = self.library.path.read_bytes()
        with self.assertRaises(ValueError):ExplorerLibrary(self.root)
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_history_deduplicates_and_remains_bounded(self):
        for index in range(105):self.library.remember(str(index))
        self.library.remember('100')
        reopened = ExplorerLibrary(self.root)
        self.assertEqual(len(reopened.value['recent']), 100)
        self.assertEqual(reopened.value['recent'][0], '100')
        self.assertEqual(reopened.value['recent'].count('100'), 1)

    def test_random_is_fair_per_work_even_with_many_versions_and_recent_history(self):
        songs = [{'id': f'a{index}', 'work_id': 'work-a', 'title': 'Song A'} for index in range(50)]
        songs.append({'id': 'b', 'work_id': 'work-b', 'title': 'Song B'})
        rng = random.Random(1)
        counts = Counter()
        for _ in range(200):
            chosen = self.library.choose(songs, rng=rng)
            counts[chosen['work_id']] += 1
            self.library.remember(chosen['id'])
        # A work with fifty versions must not suppress the single-version work.
        self.assertGreaterEqual(counts['work-a'], 70)
        self.assertGreaterEqual(counts['work-b'], 70)

    def test_same_titles_without_verified_work_ids_remain_distinct(self):
        songs = [{'id': 'a', 'title': 'Same Title'}, {'id': 'b', 'title': 'Same Title'}]
        self.library.remember('a')
        self.assertEqual(self.library.choose(songs, rng=random.Random(1))['id'], 'b')
        self.assertIsNone(self.library.choose([]))
        self.assertEqual(self.library.choose([songs[0]])['id'], 'a')

    def test_playable_pool_excludes_unknown_false_and_unlinked_recordings(self):
        songs = [{'id': value, 'title': value} for value in ('yes', 'no', 'unknown', 'unlinked')]
        self.library.link_spotify('yes', self.track)
        self.library.link_spotify('no', {**self.track, 'is_playable': False})
        self.library.link_spotify('unknown', {**self.track, 'is_playable': None})
        for _ in range(10):self.assertEqual(self.library.choose(songs, playable=True)['id'], 'yes')
        self.assertIsNone(self.library.choose(songs[1:], playable=True))

    def test_only_valid_track_ids_form_links_and_never_infer_availability(self):
        for value in ('../unsafe', 'spotify:track:' + 'a' * 22, '', 'a' * 21):
            with self.subTest(value=value):
                self.assertEqual(spotify_url({'id': value}), '')
                with self.assertRaises(ValueError):self.library.link_spotify('song', {'id': value})
        self.assertEqual(spotify_url(self.track), 'https://open.spotify.com/track/' + 'a' * 22)
        self.assertEqual(availability(None), '미연결')
        self.assertEqual(availability({'is_playable': None}), 'Spotify 링크')
        self.assertEqual(availability({'is_playable': False}), '재생 제한')
        self.assertEqual(availability(self.track), 'Spotify 가능')

    def test_related_songs_require_actual_credit_not_membership_guess(self):
        person = {'name': '如月千早', 'ko': '키사라기 치하야', 'aliases': ['Chihaya Kisaragi']}
        songs = [{'id': 'solo', 'artists': ['如月千早 (CV. 今井麻美)']},
                 {'id': 'group', 'artists': ['765PRO ALLSTARS']},
                 {'id': 'voice', 'artists': ['今井麻美']},
                 {'id': 'alias', 'artists': ['Chihaya Kisaragi']}]
        self.assertEqual([song['id'] for song in related_songs(songs, person)], ['solo', 'alias'])

    def test_related_songs_match_individual_credits_inside_composite_string(self):
        person = {'name': '如月千早', 'ko': '키사라기 치하야', 'aliases': ['Chihaya Kisaragi']}
        for separator in (', ', '、', ' × ', ' x ', ' & ', ' / ', ' + ', '; ', '，', ' ＆ ', '\n'):
            with self.subTest(separator=separator):
                songs = [{'id': 'duet', 'artists': ['天海春香 (CV. 中村繪里子)' + separator
                          + 'Chihaya Kisaragi （ＣＶ． 今井麻美, Voice）']},
                         {'id': 'tribute', 'artists': ['Chihaya Kisaragi Tribute']},
                         {'id': 'voice-only', 'artists': ['Other (CV. Chihaya Kisaragi, Voice)']}]
                before = copy.deepcopy(songs)
                self.assertEqual([song['id'] for song in related_songs(songs, person)], ['duet'])
                self.assertEqual(songs, before)

    def test_related_songs_preserve_known_group_names_and_aliases_with_separators(self):
        group = {'name': 'Alpha & Beta', 'aliases': ['A & B'], 'members': ['Alpha', 'Beta']}
        known = {artist_key(name) for name in ('Alpha & Beta', 'A & B', 'Alpha', 'Beta', 'Guest')}
        songs = [{'id': 'group', 'artists': ['Alpha & Beta']},
                 {'id': 'alias', 'artists': ['A & B × Guest']},
                 {'id': 'cv', 'artists': ['Guest, Alpha & Beta (CV. Voice A, Voice B)']},
                 {'id': 'solo', 'artists': ['Alpha & Guest']}]
        self.assertEqual([song['id'] for song in related_songs(songs, group, known_names=known)],
                         ['group', 'alias', 'cv'])
        self.assertEqual([song['id'] for song in related_songs(songs, {'name': 'Alpha'}, known_names=known)],
                         ['solo'])
        self.assertEqual(related_songs(songs, {'name': 'A'}, known_names=known), [])

    def test_related_songs_preserve_shipped_units_without_explicit_known_names(self):
        songs = [{'id': 'unit', 'artists': ['High×Joker × 如月千早']},
                 {'id': 'alias', 'artists': ['High×Joker (CV. Voice) × 如月千早']}]
        self.assertEqual(related_songs(songs, {'name': 'High×Joker'}), songs)
        self.assertEqual(related_songs(songs, {'name': 'Joker'}), [])
        self.assertEqual(related_songs(songs, {'name': '如月千早'}), songs)

    def test_related_songs_keep_explicit_ids_and_ordinary_singer_names(self):
        person = {'name': 'Singer & Producer', 'external_ids': ['artist:external']}
        songs = [{'id': 'named', 'artists': ['Guest, Singer & Producer']},
                 {'id': 'identified', 'artists': ['Unrecognized'], 'performer_ids': ['artist:external']},
                 {'id': 'partial', 'artists': ['Singer & Producer Tribute']}]
        self.assertEqual([song['id'] for song in related_songs(songs, person)], ['named', 'identified'])

    def test_search_includes_korean_performers_series_and_aliases(self):
        song = {'title': 'Example', 'artists': ['如月千早'], 'album': 'Album',
                'aliases': ['다른 제목'], 'series': ['765']}
        self.assertTrue(matches_query(song, '치하야 Example'))
        self.assertTrue(matches_query(song, '７６５ 다른 제목'))
        self.assertFalse(matches_query(song, '다른 가수'))

    def test_duplicate_preference_defaults_false_and_survives_other_edits(self):
        self.assertFalse(self.library.hide_duplicates)
        self.library.set_hide_duplicates(True)
        self.library.toggle_favorite('a');self.library.link_spotify('a', self.track);self.library.remember('a')
        reopened = ExplorerLibrary(self.root)
        self.assertTrue(reopened.hide_duplicates)
        reopened.set_hide_duplicates(False)
        again = ExplorerLibrary(self.root)
        self.assertFalse(again.hide_duplicates)
        self.assertTrue(again.is_favorite('a'))
        self.assertEqual(again.linked_track('a')['id'], self.track['id'])
        self.assertEqual(again.value['recent'], ['a'])

    def test_duplicate_preference_failed_save_and_invalid_value_preserve_state(self):
        self.library.toggle_favorite('a')
        before = self.library.path.read_bytes()
        with patch('explorer_library.atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.library.set_hide_duplicates(True)
        self.assertFalse(self.library.hide_duplicates)
        for value in (1, 'false', None):
            with self.assertRaises(ValueError):self.library.set_hide_duplicates(value)
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_legacy_library_defaults_duplicate_preference_without_rewriting(self):
        self.library.path.write_text('{"favorites": ["old"], "spotify": {}, "recent": ["old"], "other_choice": {"sort": "title"}}')
        before = self.library.path.read_bytes()
        reopened = ExplorerLibrary(self.root)
        self.assertFalse(reopened.hide_duplicates)
        self.assertTrue(reopened.is_favorite('old'))
        self.assertEqual(self.library.path.read_bytes(), before)
        reopened.set_hide_duplicates(True)
        self.assertEqual(ExplorerLibrary(self.root).value['other_choice'], {'sort': 'title'})

    def test_malformed_duplicate_preference_never_replaces_original(self):
        self.library.path.write_text('{"hide_duplicates": "false"}')
        before = self.library.path.read_bytes()
        with self.assertRaises(ValueError):ExplorerLibrary(self.root)
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_sort_preference_survives_restart_and_other_personal_changes(self):
        self.assertEqual(self.library.song_sort, 'default')
        self.library.value['other_choice'] = {'retained': True}
        self.library.toggle_favorite('favorite')
        self.library.link_spotify('linked', self.track)
        self.library.remember('recent')
        self.library.unlink_spotify('excluded')
        self.library.set_hide_duplicates(True)
        for order in SONG_SORTS:
            self.library.set_song_sort(order)
            reopened = ExplorerLibrary(self.root)
            self.assertEqual(reopened.song_sort, order)
            self.assertTrue(reopened.is_favorite('favorite'))
            self.assertEqual(reopened.linked_track('linked')['id'], self.track['id'])
            self.assertEqual(reopened.value['recent'], ['recent'])
            self.assertEqual(reopened.value['automatic_link_exclusions'], ['excluded'])
            self.assertTrue(reopened.hide_duplicates)
            self.assertEqual(reopened.value['other_choice'], {'retained': True})

    def test_unknown_sort_preference_falls_back_without_rewriting_stored_data(self):
        for invalid in ('future-order', None, False, 12, ['title_asc'], {'order': 'title_asc'}):
            with self.subTest(invalid=invalid):
                self.library.path.write_text(json.dumps({'favorites': ['old'], 'song_sort': invalid}))
                before = self.library.path.read_bytes()
                reopened = ExplorerLibrary(self.root)
                self.assertEqual(reopened.song_sort, 'default')
                self.assertEqual(self.library.path.read_bytes(), before)
                reopened.remember('played')
                self.assertEqual(reopened.value['song_sort'], invalid)
                self.assertTrue(reopened.is_favorite('old'))
                reopened.set_song_sort('release_desc')
                self.assertEqual(ExplorerLibrary(self.root).song_sort, 'release_desc')

    def test_sort_preference_invalid_choice_or_failed_save_preserves_existing_state(self):
        self.library.set_song_sort('title_asc')
        before = self.library.path.read_bytes()
        for invalid in ('unknown', None, False, 1, ['title_asc']):
            with self.assertRaises(ValueError):self.library.set_song_sort(invalid)
        with patch('explorer_library.atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.library.set_song_sort('live_desc')
        self.assertEqual(self.library.song_sort, 'title_asc')
        self.assertEqual(self.library.path.read_bytes(), before)


class SongSortTests(unittest.TestCase):
    def ids(self, songs, order, **kwargs):
        return [song['id'] for song in sort_songs(songs, order, **kwargs)]

    def test_default_preserves_order_within_existing_kind_priority(self):
        songs = [{'id': 'spoken', 'kind': 'spoken', 'title': 'A'},
                 {'id': 'unknown', 'title': 'B'}, {'id': 'song-z', 'kind': 'song', 'title': 'Z'},
                 {'id': 'instrumental', 'type': 'instrumental', 'title': 'C'},
                 {'id': 'song-a', 'kind': 'song', 'title': 'A'}]
        expected = ['song-z', 'song-a', 'unknown', 'instrumental', 'spoken']
        self.assertEqual(self.ids(songs, 'default'), expected)
        self.assertEqual(self.ids(songs, 'unsupported'), expected)
        self.assertEqual(self.ids(songs, 'title_asc'), ['song-a', 'spoken', 'unknown', 'instrumental', 'song-z'])

    def test_title_sort_normalizes_width_spacing_and_case_with_stable_id_ties(self):
        songs = [{'id': 'b', 'title': 'Ａ lpha'}, {'id': 'z', 'title': 'Zulu'},
                 {'id': 'a', 'title': 'ALPHA'}]
        self.assertEqual(self.ids(songs, 'title_asc'), ['a', 'b', 'z'])
        self.assertEqual(self.ids(songs, 'title_desc'), ['z', 'a', 'b'])

    def test_release_date_priority_and_earliest_valid_release_fallback(self):
        song = {'first_release_date': '2020-04', 'release_date': '2019',
                'releases': [{'date': '2000-01-01'}, {'date': '2010-03-02'}]}
        self.assertEqual(song_release_date(song), '2020-04')
        song['first_release_date'] = '2020-02-30'
        self.assertEqual(song_release_date(song), '2019')
        song['release_date'] = ''
        song['releases'] += [{'date': 'invalid'}, {'date': '1999-13'}, {'date': '0000'}, {}]
        self.assertEqual(song_release_date(song), '2000-01-01')
        self.assertEqual(song_release_date({}), '')

    def test_release_dates_preserve_partial_precision_and_reject_impossible_dates(self):
        for value in ('2000', '2000-02', '2000-02-29', '2024-02-29', '9999-12-31'):
            self.assertEqual(song_release_date({'first_release_date': value}), value)
        for value in ('1900-02-29', '2023-02-29', '2020-04-31', '2020-00', '2020-13', '0000',
                      '20', '2020-1', '2020-01-00', '2020-01-32', '2020-01-01-extra', None, 2020):
            with self.subTest(value=value):self.assertEqual(song_release_date({'first_release_date': value}), '')

    def test_date_sort_keeps_unknown_last_in_both_directions_and_title_id_ties(self):
        songs = [{'id': 'unknown', 'title': 'A'},
                 {'id': 'old-b', 'title': 'B', 'first_release_date': '2000'},
                 {'id': 'old-a', 'title': 'A', 'first_release_date': '2000-01-01'},
                 {'id': 'new', 'title': 'N', 'first_release_date': '2020-02'}]
        self.assertEqual(self.ids(songs, 'release_desc'), ['new', 'old-a', 'old-b', 'unknown'])
        self.assertEqual(self.ids(songs, 'release_asc'), ['old-a', 'old-b', 'new', 'unknown'])

    def test_duration_sort_rejects_nonpositive_nonfinite_boolean_and_text_values(self):
        unknown = [{'id': f'unknown-{i}', 'title': f'U{i}', 'duration': value}
                   for i, value in enumerate((None, 0, -1, float('nan'), float('inf'), -float('inf'), True, '180'))]
        songs = unknown + [{'id': 'long', 'title': 'L', 'duration': 250},
                           {'id': 'short', 'title': 'S', 'duration': 120.5},
                           {'id': 'large', 'title': 'X', 'duration': 10 ** 400}]
        last = [song['id'] for song in unknown]
        self.assertEqual(self.ids(songs, 'duration_desc'), ['large', 'long', 'short'] + last)
        self.assertEqual(self.ids(songs, 'duration_asc'), ['short', 'long', 'large'] + last)

    def test_live_sort_defaults_missing_counts_to_zero_and_breaks_ties_by_title(self):
        songs = [{'id': 'c', 'title': 'Charlie'}, {'id': 'b', 'title': 'Bravo'},
                 {'id': 'a', 'title': 'Alpha'}, {'id': 'd', 'title': 'Delta'}]
        counts = {'a': 2, 'b': 2, 'c': 5}
        self.assertEqual(self.ids(songs, 'live_desc', live_counts=counts), ['c', 'a', 'b', 'd'])
        self.assertEqual(self.ids(songs, 'live_asc', live_counts=counts), ['d', 'a', 'b', 'c'])
        self.assertEqual(self.ids(songs, 'live_desc'), ['a', 'b', 'c', 'd'])

    def test_artist_sort_and_all_orders_preserve_input_objects_and_metadata(self):
        songs = [{'id': 'a', 'title': 'Alpha', 'artists': ['Zulu Singer'], 'duration': 10,
                  'releases': [{'date': '2000-01-01'}]},
                 {'id': 'b', 'title': 'Beta', 'artists': ['Alpha Singer'], 'duration': 20}]
        before = copy.deepcopy(songs)
        self.assertEqual(self.ids(songs, 'artist_asc'), ['b', 'a'])
        for order in SONG_SORTS:
            result = sort_songs(songs, order, live_counts={'b': 5})
            self.assertIsNot(result, songs)
            self.assertEqual(songs, before)
            self.assertEqual({id(song) for song in result}, {id(song) for song in songs})


class PerformerMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.names = [
            {'name': '如月 千早', 'ko': '키사라기 치하야', 'kind': 'idol', 'franchise': '765AS',
             'aliases': ['Chihaya Kisaragi'], 'source_urls': ['https://example.test/character']},
            {'name': '今井 麻美', 'ko': '이마이 아사미', 'kind': 'voice_actor', 'aliases': ['Asami Imai']},
            {'name': 'Unit', 'ko': '유닛', 'kind': 'group', 'franchise': '765AS', 'aliases': []}]
        self.colors = [{'name': '如月 千早', 'kind': 'idol', 'color': '#2743D2',
                        'source': 'https://example.test/color'},
                       {'name': 'Unit', 'kind': 'group', 'members': ['如月 千早'],
                        'membership_sources': ['https://example.test/members']}]
        self.external = [{'id': 'imas:chihaya', 'name': '如月千早', 'kind': 'idol', 'series': ['765'],
                          'cv': ['今井麻美'], 'color': 'ffffff', 'aliases': ['Chihaya Kisaragi'],
                          'source_url': 'https://example.test/relationship',
                          'provenance': [{'source_id': 'imasparql', 'url': 'https://example.test/relationship'}]}]

    def tearDown(self):self.temp.cleanup()

    def load(self):
        for directory, entries in [('artist_names', self.names), ('artist_colors', self.colors)]:
            folder = self.root / directory;folder.mkdir(exist_ok=True)
            (folder / 'test.json').write_text(json.dumps({'entries': entries}), encoding='utf-8')
        return performers(self.root, self.external)

    def test_local_color_wins_and_external_cv_and_sources_are_retained(self):
        before = copy.deepcopy(self.external)
        people = self.load();person = lookup_performer(people, '키사라기 치하야')
        self.assertEqual(person['color'], '#2743d2')
        self.assertEqual(person['color_sources'], ['https://example.test/color'])
        self.assertEqual(person['voice_actors'][0]['name'], '今井 麻美')
        self.assertEqual(person['voice_actors'][0]['display'], '이마이 아사미')
        self.assertIn('https://example.test/relationship', person['voice_actors'][0]['source_urls'])
        self.assertIn('https://example.test/relationship', person['source_urls'])
        self.assertEqual(person['provenance'], before[0]['provenance'])
        self.assertEqual(self.external, before)

    def test_external_only_person_normalizes_color_without_inventing_voice_translation(self):
        self.external.append({'id': 'imas:other', 'name': 'New Idol', 'kind': 'idol', 'series': ['other'],
                              'color': 'Ab12CD', 'cv': ['Unknown Voice'], 'source_url': 'https://example.test/new'})
        person = lookup_performer(self.load(), 'New Idol')
        self.assertEqual(person['color'], '#ab12cd')
        self.assertEqual(person['voice_actors'][0]['display'], 'Unknown Voice')
        self.assertEqual(person['color_sources'], ['https://example.test/new'])

    def test_verified_local_cv_survives_without_external_relationship(self):
        self.names[0].update(cv=['今井麻美'], voice_actor_sources=['https://example.test/local-cv'])
        self.external = []
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual(person['voice_actors'][0]['display'], '이마이 아사미')
        self.assertEqual(person['voice_actor_sources'], ['https://example.test/local-cv'])
        self.assertIn('https://example.test/local-cv', person['source_urls'])

    def test_local_cv_keeps_different_external_voice_instead_of_overriding_it(self):
        self.names[0].update(cv=['Different Voice'], voice_actor_sources=['https://example.test/local-cv'])
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual([voice['display'] for voice in person['voice_actors']], ['Different Voice', '이마이 아사미'])
        self.assertEqual(person['voice_actor_sources'],
                         ['https://example.test/local-cv', 'https://example.test/relationship'])

    def test_external_color_fills_missing_local_color_only(self):
        self.colors[0].pop('color')
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual(person['color'], '#ffffff')
        self.assertEqual(person['color_sources'], ['https://example.test/relationship'])

    def test_invalid_color_and_missing_cv_remain_unknown(self):
        self.colors[0]['color'] = 'red'
        self.external[0]['color'] = 'not a color';self.external[0]['cv'] = []
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual(person['color'], '')
        self.assertEqual(person['voice_actors'], [])

    def test_conflicting_local_colors_are_not_replaced_with_arbitrary_external_color(self):
        self.colors.append({**self.colors[0], 'color': '#ffffff'})
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual(person['color'], '')

    def test_group_membership_is_kept_separate_from_member_color_and_voice(self):
        people = self.load();group = lookup_performer(people, '유닛')
        self.assertEqual(group['members'], ['如月 千早'])
        self.assertEqual(group['voice_actors'], [])
        self.assertEqual(group['color'], '')
        self.assertEqual(lookup_performer(people, group['members'][0])['color'], '#2743d2')

    def test_lookup_accepts_aliases_and_cv_but_omits_shared_aliases(self):
        people = self.load()
        person = lookup_performer(people, 'Chihaya Kisaragi')
        self.assertIs(lookup_performer(people, '如月 千早 （ＣＶ．今井麻美）'), person)
        self.assertIsNone(lookup_performer(people, '如月 千早', kind='group'))
        people.append({'name': 'Other', 'aliases': ['Chihaya Kisaragi']})
        index = build_performer_lookup(people)
        self.assertNotIn(artist_key('Chihaya Kisaragi'), index)
        self.assertIsNone(lookup_performer(people, 'Chihaya Kisaragi'))
        self.assertIs(index[artist_key('如月 千早')], person)

    def test_ambiguous_external_identity_does_not_assign_cv_to_either_local_person(self):
        self.names[0]['aliases'].append('Shared')
        self.names.append({'name': 'Other Idol', 'ko': '다른 아이돌', 'kind': 'idol', 'aliases': ['Shared']})
        self.external = [{'id': 'external:ambiguous', 'name': 'Shared', 'kind': 'idol', 'cv': ['今井麻美']}]
        people = self.load()
        self.assertEqual(lookup_performer(people, '如月 千早')['voice_actors'], [])
        self.assertEqual(lookup_performer(people, 'Other Idol')['voice_actors'], [])
        self.assertIsNone(lookup_performer(people, 'Shared'))
        self.assertEqual(sum(bool(person['voice_actors']) for person in people), 1)

    def test_multiple_explicit_voice_actors_preserved_and_aliases_deduplicated(self):
        self.external[0]['cv'] = ['今井麻美', '今井 麻美', 'Different Voice']
        person = lookup_performer(self.load(), '如月 千早')
        self.assertEqual([voice['display'] for voice in person['voice_actors']], ['이마이 아사미', 'Different Voice'])


class GroupRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.idol = {'id': 'idol:a', 'name': 'Character A', 'display': '캐릭터 A', 'kind': 'idol',
                     'aliases': ['A'], 'color': '#123456', 'cv': ['Voice'],
                     'voice_actors': [{'name': 'Voice', 'display': '성우'}],
                     'source_urls': ['https://example.test/character']}
        self.second = {'id': 'idol:b', 'name': 'Character B', 'display': '캐릭터 B', 'kind': 'idol'}
        self.group = {'id': 'group:unit', 'name': 'Unit', 'kind': 'group', 'aliases': ['유닛'],
                      'members': ['A', 'Character B'], 'membership_sources': ['https://example.test/unit']}
        self.people = [self.idol, self.second, self.group]

    def test_explicit_roster_resolves_aliases_color_voice_and_sources(self):
        members = resolve_group_members('유닛', self.people)
        self.assertEqual([member['id'] for member in members], ['idol:a', 'idol:b'])
        self.assertEqual(members[0]['voice_actors'][0]['display'], '성우')
        self.assertEqual(members[0]['color'], '#123456')
        self.assertEqual(members[0]['source_urls'], ['https://example.test/character'])
        self.assertEqual(members[1].get('voice_actors', []), [])

    def test_same_named_character_and_group_resolve_by_requested_kind(self):
        namesake = {'id': 'group:a', 'name': 'Character A', 'kind': 'group', 'members': ['Character A']}
        self.people.append(namesake)
        self.assertIsNone(lookup_performer(self.people, 'Character A'))
        self.assertIs(lookup_performer(self.people, 'Character A', kind='idol'), self.idol)
        self.assertIs(lookup_performer(self.people, 'Character A', kind='group'), namesake)
        self.assertEqual([p['id'] for p in resolve_group_members(namesake, self.people)], ['idol:a'])

    def test_nested_groups_expand_with_order_and_duplicate_members_preserved_once(self):
        parent = {'id': 'group:parent', 'name': 'Parent', 'kind': 'group',
                  'members': ['Unit', 'Character B', 'Character A']}
        self.people.append(parent)
        self.assertEqual([p['id'] for p in resolve_group_members(parent, self.people)], ['idol:a', 'idol:b'])

    def test_unknown_empty_group_and_cycle_remain_visible(self):
        empty = {'id': 'group:empty', 'name': 'Empty', 'kind': 'group', 'members': []}
        parent = {'id': 'group:parent', 'name': 'Parent', 'kind': 'group',
                  'members': ['Unit', 'Unlisted', 'Empty']}
        self.group['members'].append('Parent')
        self.people.extend([empty, parent])
        members = resolve_group_members(parent, self.people)
        self.assertEqual([p['name'] for p in members], ['Character A', 'Character B', 'Parent', 'Unlisted', 'Empty'])
        self.assertEqual([p['member_status'] for p in members if p['kind'] == 'unknown'],
                         ['cycle', 'unconfirmed', 'unconfirmed'])

    def test_ambiguous_character_alias_never_falls_back_to_same_named_group(self):
        self.second['aliases'] = ['A']
        self.people.append({'id': 'group:a', 'name': 'A', 'kind': 'group', 'members': ['Character B']})
        members = resolve_group_members(self.group, self.people)
        self.assertEqual(members[0]['name'], 'A')
        self.assertEqual(members[0]['kind'], 'unknown')
        self.assertEqual(members[0]['member_status'], 'ambiguous')
        self.assertEqual(members[0]['voice_actors'], [])
        self.assertEqual(members[1]['id'], 'idol:b')

    def test_resolution_does_not_mutate_catalog_or_assign_members_to_song_credit(self):
        before = copy.deepcopy(self.people)
        members = resolve_group_members(self.group, iter(self.people))
        members[0]['voice_actors'][0]['display'] = 'Changed'
        self.assertEqual(self.people, before)
        songs = [{'id': 'group-song', 'artists': ['Unit']}]
        self.assertEqual(related_songs(songs, self.idol), [])
        self.assertEqual(related_songs(songs, self.group), songs)

    def test_non_group_unknown_group_and_empty_roster_are_empty(self):
        self.assertEqual(resolve_group_members(self.idol, self.people), [])
        self.assertEqual(resolve_group_members('Unlisted', self.people), [])
        self.group['members'] = []
        self.assertEqual(resolve_group_members(self.group, self.people), [])

    def test_shipped_same_named_ruka_character_resolves_in_unit_roster(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / 'resources' / 'song_catalog.json').read_text(encoding='utf-8'))
        people = performers(external=catalog['performers'])
        members = resolve_group_members('コメティック', people)
        ruka = next(person for person in members if person['name'] == '斑鳩ルカ')
        self.assertEqual(ruka['kind'], 'idol')
        self.assertEqual(ruka['voice_actors'][0]['display'], '카와구치 리나')

    def test_shipped_aliases_unify_roco_and_letora_and_fill_verified_voices(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / 'resources' / 'song_catalog.json').read_text(encoding='utf-8'))
        people = performers(external=catalog['performers'])
        roco = lookup_performer(people, 'ロコ', kind='idol')
        self.assertIs(lookup_performer(people, '伴田路子', kind='idol'), roco)
        self.assertEqual(roco['voice_actors'][0]['display'], '나카무라 아츠키')
        letora = lookup_performer(people, 'レトラ', kind='idol')
        self.assertIs(lookup_performer(people, 'サラ・レトラ・オリヴェイラ・ウタガワ', kind='idol'), letora)
        self.assertEqual(letora['voice_actors'], [])
        for name, voice in [('音無小鳥', '타키타 쥬리'), ('奥空心白', '타나카 아이미')]:
            self.assertEqual(lookup_performer(people, name, kind='idol')['voice_actors'][0]['display'], voice)

    def test_shipped_315_allstars_has_current_roster_without_changing_315_stars(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / 'resources' / 'song_catalog.json').read_text(encoding='utf-8'))
        people = performers(external=catalog['performers'])
        members = resolve_group_members('315 ALLSTARS', people)
        self.assertEqual(len(members), 49)
        self.assertEqual(len({person['id'] for person in members}), 49)
        self.assertTrue(all(person['kind'] == 'idol' and person['voice_actors'] and person['color'] for person in members))
        self.assertEqual(resolve_group_members('315 STARS', people), [])


class DuplicateSongTests(unittest.TestCase):
    def song(self, identifier, **changes):
        return {'id': identifier, 'title': 'Same Song', 'artists': ['如月 千早'],
                'kind': 'song', 'duration': 180, 'album': 'First album', **changes}

    def groups(self, songs):
        return [[song['id'] for song in group] for group in group_duplicate_songs(songs)]

    def test_title_normalizes_unicode_case_and_whitespace(self):
        songs = [self.song('a', title='Ｓａｍｅ  Song', artists=['如月 千早 (CV. 今井麻美)', 'Artist B']),
                 self.song('b', title='sameSong', artists=['Artist B', '如月千早'], duration=182, album='Compilation')]
        self.assertEqual(self.groups(songs), [['a', 'b']])

    def test_groups_by_title_ignoring_performers_kind_duration_and_album(self):
        songs = [self.song('a'), self.song('live', title='Same Song (Live)'),
                 self.song('solo', title='Same Song -Solo Mix-'), self.song('artist', artists=['Other']),
                 self.song('kind', kind='instrumental'), self.song('long', duration=300, album='Another')]
        self.assertEqual(self.groups(songs), [['a', 'artist', 'kind', 'long'], ['live'], ['solo']])

    def test_missing_non_title_metadata_still_groups(self):
        for changes in ({'duration': None}, {'duration': 0}, {'duration': True}, {'duration': float('nan')},
                        {'duration': float('inf')}, {'artists': []}, {'artists': ['[unknown]']},
                        {'artists': ['[dialogue]']}, {'artists': ['[no artist]']},
                        {'artists': ['(CV. Someone)']}, {'kind': None}, {'kind': 'unknown'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.groups([self.song('a', **changes), self.song('b', **changes)]), [['a', 'b']])

    def test_missing_title_stays_separate(self):
        for title in ('', '  ', None):
            with self.subTest(title=title):
                self.assertEqual(self.groups([self.song('a', title=title), self.song('b', title=title)]),
                                 [['a'], ['b']])

    def test_original_objects_fields_and_order_within_groups_are_preserved(self):
        songs = [self.song('a', duration=182), self.song('other', title='Other'), self.song('b', duration=180)]
        before = copy.deepcopy(songs)
        groups = group_duplicate_songs(songs)
        self.assertEqual([[song['id'] for song in group] for group in groups], [['a', 'b'], ['other']])
        self.assertIs(groups[0][0], songs[0]);self.assertIs(groups[0][1], songs[2])
        self.assertEqual(songs, before)
        self.assertEqual(group_duplicate_songs([]), [])

    def test_single_shared_work_id_does_not_collapse_distinct_versions(self):
        songs = [self.song('studio', work_id='same'), self.song('live', work_id='same', title='Same Song (Live)')]
        self.assertEqual(self.groups(songs), [['studio'], ['live']])

    def test_different_explicit_voice_actors_group_without_changing_credits(self):
        songs = [self.song('old', artists=['萩原雪歩 (CV. 長谷優里奈)']),
                 self.song('new', artists=['萩原雪歩 (CV. 浅倉杏美)'], duration=181)]
        before = copy.deepcopy(songs)
        self.assertEqual(self.groups(songs), [['old', 'new']])
        self.assertEqual(songs, before)


if __name__ == '__main__':unittest.main()
