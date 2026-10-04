import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import group_credits
from group_credits import group_member_lines, save_group_credits, with_character_voices


def idol(identity='idol:first', name='First', display='첫 캐릭터', voice='첫 성우'):
    return {'id': identity, 'name': name, 'display': display, 'kind': 'idol', 'aliases': [],
            'voice_actors': [{'name': voice, 'display': voice}] if voice else [],
            'source_urls': ['https://example.org/idol'], 'voice_actor_sources': ['https://example.org/cv']}


def group(identity='group:test', name='Example Group', display='예제 그룹', members=None):
    return {'id': identity, 'name': name, 'display': display, 'kind': 'group', 'aliases': ['Example Unit'],
            'members': ['First', 'Second'] if members is None else members,
            'source_urls': ['https://example.org/group'], 'membership_sources': ['https://example.org/roster']}


class GroupCreditsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bundled.json'
        self.update = Path(self.temp.name) / 'updated.json'
        self.people = [group(), idol(), idol('idol:second', 'Second', '두 번째 캐릭터', '')]
        save_group_credits(self.people, self.path)
        self.resource_patch = patch.object(group_credits, 'RESOURCE_PATH', self.path)
        self.resource_patch.start()
        self.addCleanup(self.resource_patch.stop)
        group_credits._read_snapshot.cache_clear()

    def test_roster_adds_korean_characters_and_known_or_unknown_voice(self):
        self.assertEqual(group_member_lines(['Example Group']),
                         ('예제 그룹 멤버: 첫 캐릭터 (CV. 첫 성우), 두 번째 캐릭터 (CV. 성우 미확인)',))
        self.assertEqual(group_member_lines(['예제 그룹']), group_member_lines(['Example Unit']))

    def test_original_artist_values_are_not_changed(self):
        artists = ['Example Group']
        original = artists[:]
        group_member_lines(artists)
        self.assertEqual(artists, original)

    def test_explicit_member_does_not_expand_subset_to_whole_group(self):
        for artists in (['Example Group', 'First'], ['Example Group, 첫 캐릭터 (CV. 첫 성우)'],
                        ['Example Group × First（CV： Someone）']):
            with self.subTest(artists=artists):self.assertEqual(group_member_lines(artists), ())

    def test_project_roster_is_retained_but_never_added_to_recording_credits(self):
        self.people[0]['credit_scope'] = 'project'
        save_group_credits(self.people, self.path)
        snapshot = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(len(snapshot['groups'][0]['members']), 2)
        self.assertEqual(snapshot['groups'][0]['credit_scope'], 'project')
        self.assertEqual(group_member_lines(['Example Group']), ())
        self.assertEqual(with_character_voices(['Example Group', 'First'], 'THE IDOLM@STER'),
                         ('Example Group', 'First (CV. 첫 성우)'))

    def test_older_hatsuboshi_snapshot_without_scope_does_not_expand_students(self):
        self.people[0].update(name='初星学園', display='하츠보시 학원',
                              aliases=['Hatsuboshi Gakuen', 'Hatsuboshi Academy'])
        save_group_credits(self.people, self.update)
        snapshot = json.loads(self.update.read_text(encoding='utf-8'))
        snapshot['groups'][0].pop('credit_scope', None)
        self.update.write_text(json.dumps(snapshot), encoding='utf-8')
        for artist in ('初星学園', '하츠보시 학원', 'Hatsuboshi Gakuen', 'Hatsuboshi Academy'):
            with self.subTest(artist=artist):
                self.assertEqual(group_member_lines([artist], update_path=self.update), ())

    def test_other_group_with_no_explicit_member_can_still_expand(self):
        self.people.append(group('group:other', 'Another Group', '다른 그룹', ['Second']))
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['Example Group', 'Another Group', 'First']),
                         ('다른 그룹 멤버: 두 번째 캐릭터 (CV. 성우 미확인)',))

    def test_punctuation_inside_known_groups_is_preserved(self):
        self.people[0].update(name='High×Joker', display='High×Joker', aliases=[])
        self.people.append(group('group:iris', 'Re;IRIS', 'Re;IRIS', ['First']))
        save_group_credits(self.people, self.path)
        lines = group_member_lines(['High×Joker × Re;IRIS'])
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('High×Joker 멤버: '))
        self.assertTrue(lines[1].startswith('Re;IRIS 멤버: '))

    def test_composite_group_alias_does_not_repeat_explicit_character_credits(self):
        self.people[0]['aliases'] += ['First, Second', 'First (CV. Actor), Second (CV. Someone)',
                                      '첫 캐릭터, 두 번째 캐릭터']
        save_group_credits(self.people, self.path)
        for alias in self.people[0]['aliases'][1:]:
            with self.subTest(alias=alias):self.assertEqual(group_member_lines([alias]), ())

    def test_explicit_composite_credit_suppresses_separate_group_alias(self):
        self.people[0]['aliases'].append('First (CV. Actor), Second (CV. Someone)')
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['Example Group', 'First (CV. Actor), Second (CV. Someone)']), ())

    def test_short_latin_alias_needs_idolmaster_album_context(self):
        self.people[0]['aliases'].append('ABC')
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['ABC'], 'Ordinary pop album'), ())
        self.assertEqual(group_member_lines(['ABC'], 'THE IDOLM@STER album'),
                         group_member_lines(['Example Group']))

    def test_ambiguous_group_alias_is_not_resolved(self):
        self.people.append(group('group:other', 'Another Group', '다른 그룹', ['First']))
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['Example Unit']), ())
        self.assertTrue(group_member_lines(['Example Group']))

    def test_alias_shared_with_character_outside_roster_is_not_resolved(self):
        outside = idol('idol:outside', 'Elsewhere', '바깥 캐릭터')
        outside['aliases'] = ['Example Unit']
        self.people.append(outside)
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['Example Unit']), ())

    def test_cv_credit_resolves_character_despite_same_named_group(self):
        self.people.append(group('group:first', 'First', '동명 그룹', ['Second']))
        save_group_credits(self.people, self.path)
        self.assertEqual(group_member_lines(['Example Group', 'First (CV. Actor)']), ())

    def test_cv_credit_does_not_resolve_alias_shared_by_two_characters(self):
        outside = idol('idol:outside', 'Third', '다른 캐릭터')
        outside['aliases'] = ['First']
        self.people.append(outside)
        save_group_credits(self.people, self.path)
        # Membership itself is unresolved; no CV is attached to the ambiguous name.
        self.assertIn('First (구성원 미확인)', group_member_lines(['Example Group', 'First (CV. Actor)'])[0])

    def test_duplicate_group_aliases_do_not_repeat_roster(self):
        self.assertEqual(group_member_lines(['Example Group', 'Example Unit']), group_member_lines(['Example Group']))

    def test_empty_or_invalid_inputs_do_not_read_files(self):
        with patch.object(group_credits, '_catalog') as catalog:
            for artists in ([], (), None, 1, [None, 4, ' '], ''):
                self.assertEqual(group_member_lines(artists), ())
            catalog.assert_not_called()

    def test_unknown_group_stays_unknown(self):
        self.assertEqual(group_member_lines(['An unknown group']), ())

    def test_confirmed_character_with_missing_cv_is_explicitly_marked(self):
        self.assertIn('두 번째 캐릭터 (CV. 성우 미확인)', group_member_lines(['Example Group'])[0])

    def test_unresolved_member_is_preserved_as_unconfirmed(self):
        self.people[0]['members'] = ['First', 'Not yet identified']
        save_group_credits(self.people, self.path)
        self.assertIn('Not yet identified (구성원 미확인)', group_member_lines(['Example Group'])[0])

    def test_public_sources_are_preserved_without_private_fields(self):
        self.people[0]['spotify_token'] = 'private-value'
        self.people[1]['local_path'] = 'C:/private/music.mp3'
        save_group_credits(self.people, self.path)
        text = self.path.read_text(encoding='utf-8')
        saved = json.loads(text)
        self.assertIn('https://example.org/roster', saved['groups'][0]['source_urls'])
        self.assertIn('https://example.org/cv', saved['members'][0]['source_urls'])
        self.assertNotIn('private', text)

    def test_update_snapshot_is_used_and_change_invalidates_cache(self):
        people = copy.deepcopy(self.people)
        people[0]['display'] = '업데이트 그룹'
        save_group_credits(people, self.update)
        self.assertTrue(group_member_lines(['Example Group'], update_path=self.update)[0].startswith('업데이트 그룹'))
        people[0]['display'] = '다시 업데이트한 그룹'
        save_group_credits(people, self.update)
        self.assertTrue(group_member_lines(['Example Group'], update_path=self.update)[0].startswith('다시 업데이트한 그룹'))

    def test_unchanged_snapshot_is_not_reparsed(self):
        with patch.object(group_credits.json, 'loads', wraps=json.loads) as loads:
            for _ in range(3):group_member_lines(['Example Group'])
            self.assertEqual(loads.call_count, 1)

    def test_missing_or_malformed_update_falls_back_to_bundle(self):
        expected = group_member_lines(['Example Group'])
        self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), expected)
        for payload in ('{', 'null', '{"schema_version": 2}'):
            self.update.write_text(payload, encoding='utf-8')
            self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), expected)

    def test_invalid_member_reference_falls_back(self):
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        saved['groups'][0]['members'].append('missing:id')
        self.update.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), group_member_lines(['Example Group']))

    def test_invalid_source_or_display_falls_back(self):
        original = json.loads(self.path.read_text(encoding='utf-8'))
        for field, value in (('source_urls', ['file:///private']), ('display', 'text\0invalid')):
            saved = copy.deepcopy(original)
            saved['groups'][0][field] = value
            self.update.write_text(json.dumps(saved), encoding='utf-8')
            self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), group_member_lines(['Example Group']))

    def test_missing_all_resources_returns_no_roster(self):
        self.path.unlink()
        self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), ())

    def test_oversized_update_falls_back_without_loading_json(self):
        self.update.write_bytes(b' ' * 200)
        with patch.object(group_credits, 'MAX_BYTES', 150):
            with patch.object(group_credits, '_read_snapshot') as read:
                self.assertEqual(group_member_lines(['Example Group'], update_path=self.update), ())
                read.assert_not_called()

    def test_character_display_copy_adds_unique_original_voice(self):
        artists = ['First', 'Second', 'Example Group', 'Composer']
        original = artists[:]
        self.assertEqual(with_character_voices(artists, 'THE IDOLM@STER'),
                         ('First (CV. 첫 성우)', 'Second', 'Example Group', 'Composer'))
        self.assertEqual(artists, original)

    def test_character_voice_preserves_spacing_separators_and_group_punctuation(self):
        self.people[0].update(name='High×Joker', display='High×Joker', aliases=['Re;IRIS'])
        save_group_credits(self.people, self.path)
        value = ' High×Joker × First  , Second; Re;IRIS / First\nUnknown '
        self.assertEqual(with_character_voices([value], 'THE IDOLM@STER'),
                         (' High×Joker × First (CV. 첫 성우)  , Second; Re;IRIS / First (CV. 첫 성우)\nUnknown ',))

    def test_character_with_existing_cv_or_parenthetical_credit_is_unchanged(self):
        for value in ('First (CV. Given)', 'First（ｃｖ： Given）', 'First (cv: Given)',
                      'First (Given Actor)', 'First [CV. Given]', 'First (Version 2)'):
            with self.subTest(value=value):
                self.assertEqual(with_character_voices([value], 'THE IDOLM@STER'), (value,))

    def test_character_short_alias_requires_album_context(self):
        self.assertEqual(with_character_voices(['First'], 'Unrelated album'), ('First',))
        self.assertEqual(with_character_voices(['First'], 'THE IDOLM@STER'), ('First (CV. 첫 성우)',))

    def test_character_shared_group_name_is_not_assumed_to_be_idol(self):
        self.people.append(group('group:first', 'First', '동명 그룹', ['Second']))
        save_group_credits(self.people, self.path)
        self.assertEqual(with_character_voices(['First'], 'THE IDOLM@STER'), ('First',))

    def test_character_shared_alias_is_not_assigned_a_voice(self):
        self.people[2]['aliases'] = ['First']
        save_group_credits(self.people, self.path)
        self.assertEqual(with_character_voices(['First'], 'THE IDOLM@STER'), ('First',))

    def test_character_with_multiple_known_voices_does_not_guess_recording_version(self):
        self.people[1]['voice_actors'].append({'name': '다른 성우', 'display': '다른 성우'})
        save_group_credits(self.people, self.path)
        self.assertEqual(with_character_voices(['First'], 'THE IDOLM@STER'), ('First',))

    def test_character_without_group_is_included_in_snapshot(self):
        self.people.append(idol('idol:solo', 'Standalone Character', '단독 캐릭터', '단독 성우'))
        save_group_credits(self.people, self.path)
        self.assertEqual(with_character_voices(['Standalone Character']), ('Standalone Character (CV. 단독 성우)',))

    def test_character_voice_ignores_old_group_only_update(self):
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        saved['schema_version'] = 1
        saved['members'][0]['voice_actors'][0]['name'] = 'Outdated'
        self.update.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(with_character_voices(['First'], 'THE IDOLM@STER', self.update),
                         ('First (CV. 첫 성우)',))

    def test_character_voice_missing_resources_keeps_original_strings(self):
        self.path.unlink()
        self.assertEqual(with_character_voices([' First ', ''], 'THE IDOLM@STER'), (' First ', ''))

    def test_character_voice_empty_strings_do_not_read_resources(self):
        with patch.object(group_credits, '_catalog') as catalog:
            self.assertEqual(with_character_voices(['', '  ']), ('', '  '))
            self.assertEqual(with_character_voices([]), ())
            catalog.assert_not_called()


class BundledCharacterVoiceTests(unittest.TestCase):
    def test_poplinks_five_explicit_characters_keep_all_voices_and_affiliation(self):
        from artist_names import artist_label

        original = '天海春香、島村卯月、春日未来、天道 輝、櫻木真乃 from THE IDOLM@STER POPLINKS'
        artists = [original]
        voiced = with_character_voices(artists, 'THE IDOLM@STER')
        self.assertEqual(artist_label(voiced, 'THE IDOLM@STER'),
                         '아마미 하루카 (CV. 나카무라 에리코)、시마무라 우즈키 (CV. 오오하시 아야카)、'
                         '카스가 미라이 (CV. 야마자키 하루카)、텐도 테루 (CV. 나카무라 슈고)、'
                         '사쿠라기 마노 (CV. 세키네 히토미) from THE IDOLM@STER POPLINKS')
        self.assertEqual(artists, [original])
        self.assertEqual(group_member_lines(artists, 'THE IDOLM@STER'), ())
        suffix = '  from THE IDOLM@STER POPLINKS  '
        self.assertEqual(with_character_voices(['櫻木真乃' + suffix], 'THE IDOLM@STER'),
                         ('櫻木真乃 (CV. 関根瞳)' + suffix,))
        for value in ('Unknown Singer' + suffix, '櫻木真乃 from Another Project',
                      '櫻木真乃 from THE IDOLM@STER POPLINKS Tribute'):
            with self.subTest(value=value):
                self.assertEqual(with_character_voices([value], 'THE IDOLM@STER'), (value,))

    def test_hatsuboshi_project_never_implies_all_students_sang(self):
        for artists in (['初星学園'], ['初星学園', '篠澤 広'],
                        ['初星学園, 篠澤 広 (CV. 川村玲奈)']):
            with self.subTest(artists=artists):
                before = list(artists)
                self.assertEqual(group_member_lines(artists, 'コントラスト'), ())
                self.assertEqual(artists, before)
        self.assertEqual(with_character_voices(['初星学園'], 'コントラスト'), ('初星学園',))

    def test_contrast_credit_only_adds_hiro_voice(self):
        artists = ['初星学園', '佐々木恵梨', '鵜飼大幹', '中村ヒロ', '篠澤 広']
        self.assertEqual(with_character_voices(artists, 'コントラスト'),
                         ('初星学園', '佐々木恵梨', '鵜飼大幹', '中村ヒロ', '篠澤 広 (CV. 川村玲奈)'))


if __name__ == '__main__':unittest.main()
