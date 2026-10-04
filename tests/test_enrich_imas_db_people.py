"""Public fan-source facts supplement identities without changing known credits."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from explorer_library import performers
from tools.enrich_imas_db_people import parse_people, CV_URL, COLOR_URL
from imas_people_sources import refresh_people


def cv_row(actor, name='萩原雪歩', identifier='3', suffix=''):
    return (f'<tr><td><ruby><rb>{actor}</rb><rt>reading</rt></ruby></td>'
            f'<td><span class="idol-name" data-character-id="{identifier}" '
            f'data-person-id="41" data-brand-id="1">{name}</span>{suffix}</td></tr>')


class SourceParsingTests(unittest.TestCase):
    def test_voice_history_uses_row_actor_and_preserves_tenure_and_adaptation(self):
        html = '<table>' + cv_row('浅倉杏美', suffix='<small class="date">(2010-07-04〜)</small>')
        html += cv_row('長谷優里奈', suffix='<small class="date">(〜2010-07-03)</small>')
        html += cv_row('堀江由衣', suffix='(<span class="idol-name" data-brand-id="2">ゼノグラシア</span>)') + '</table>'
        people = parse_people(html, '')
        self.assertEqual(len(people), 1)
        history = people[0]['voice_actor_history']
        self.assertEqual([x['name'] for x in history], ['浅倉杏美', '長谷優里奈', '堀江由衣'])
        self.assertEqual(history[0]['from'], '2010-07-04')
        self.assertEqual(history[1]['until'], '2010-07-03')
        self.assertEqual(history[2]['role_context'], 'ゼノグラシア')
        self.assertNotIn('source_person_id', history[1])

    def test_colors_ignore_brand_unit_rows_keep_alternatives_and_plain_names(self):
        html = '<table><thead><tr><th>ブランド</th></tr></thead><tbody><tr><td>765PRO</td><td>#ff0000</td></tr></tbody></table>'
        html += '<table><thead><tr><th>ユニット / アイドル(CV)</th><th>カラーコード</th></tr></thead><tbody>'
        html += '<tr><th>Unit</th><td>#ff0000</td></tr><tr><td>萩原雪歩(浅倉杏美)</td><td>#D3DDE9</td><td>#d3e5e5</td></tr>'
        html += '<tr><td>未ボイス</td><td>#abcdef</td><td>#fedcba</td></tr></tbody></table>'
        people = parse_people('<table>' + cv_row('浅倉杏美') + '</table>', html)
        self.assertEqual({p['name'] for p in people}, {'萩原雪歩', '未ボイス'})
        yukiho = next(p for p in people if p['name'] == '萩原雪歩')
        self.assertEqual(yukiho['color'], '#d3dde9')
        self.assertEqual(len(yukiho['color_candidates']), 2)

    def test_different_primary_context_colors_are_not_arbitrarily_selected(self):
        table = '<table><thead><tr><th>アイドル</th></tr></thead><tbody><tr><td>秋月涼</td><td>{}</td></tr></tbody></table>'
        person = parse_people('', table.format('#123456') + table.format('#abcdef'))[0]
        self.assertNotIn('color', person)
        self.assertTrue(person['color_conflict'])

    def test_invalid_tenure_fails_candidate_instead_of_publishing_wrong_dates(self):
        with self.assertRaises(ValueError):
            parse_people('<table>' + cv_row('Actor', suffix='<small class="date">(2022-99-01〜)</small>') + '</table>', '')


class EvidenceMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'artist_names').mkdir()
        (self.root / 'artist_colors').mkdir()
        self.names = [{'kind': 'idol', 'name': '萩原雪歩', 'cv': []},
                      {'kind': 'voice_actor', 'name': '浅倉杏美', 'ko': '아사쿠라 아즈미'}]
        self.entry = {'name': '萩原雪歩', 'voice_actor_history': [
            {'name': '浅倉杏美', 'from': '2010-07-04', 'source_url': CV_URL},
            {'name': '長谷優里奈', 'until': '2010-07-03', 'source_url': CV_URL},
            {'name': '堀江由衣', 'role_context': 'ゼノグラシア', 'source_url': CV_URL}],
            'color': '#d3dde9', 'source_urls': [CV_URL, COLOR_URL]}

    def tearDown(self):self.temp.cleanup()

    def write(self, entries=None):
        (self.root / 'artist_names' / 'test.json').write_text(json.dumps({'entries': self.names}), 'utf-8')
        (self.root / 'imas_db_people.json').write_text(json.dumps({'schema_version': 1, 'verified_at': '2026-10-02',
            'characters': entries if entries is not None else [self.entry]}), 'utf-8')

    def test_missing_values_filled_with_current_voice_provenance_and_history(self):
        self.write();person = performers(self.root)[0]
        self.assertEqual(person['cv'], ['浅倉杏美'])
        self.assertEqual(person['voice_actors'][0]['display'], '아사쿠라 아즈미')
        self.assertEqual(person['color'], '#d3dde9')
        self.assertEqual([x['name'] for x in person['voice_actor_history']], ['浅倉杏美', '長谷優里奈'])
        self.assertIn(CV_URL, person['voice_actor_sources'])
        self.assertIn(COLOR_URL, person['color_sources'])

    def test_existing_voice_and_official_color_are_preserved_on_disagreement(self):
        self.names[0]['cv'] = ['Known Actor']
        (self.root / 'artist_colors' / 'test.json').write_text(json.dumps({'entries': [
            {'name': '萩原雪歩', 'color': '#123456', 'source': 'https://official.example/color'}]}), 'utf-8')
        self.write();person = performers(self.root)[0]
        self.assertEqual(person['cv'], ['Known Actor'])
        self.assertEqual(person['color'], '#123456')
        self.assertTrue(person['voice_actor_conflict'])
        self.assertEqual(person['database_color'], '#d3dde9')

    def test_ended_future_and_ambiguous_actors_are_never_current_voice_guesses(self):
        for history in ([self.entry['voice_actor_history'][1]],
                        [{'name': 'Future', 'from': '2099-01-01', 'source_url': CV_URL}],
                        [{'name': 'A', 'source_url': CV_URL}, {'name': 'B', 'source_url': CV_URL}]):
            with self.subTest(history=history):
                self.write([{**self.entry, 'voice_actor_history': history}])
                self.assertEqual(performers(self.root)[0]['cv'], [])

    def test_unmatched_or_ambiguous_source_names_do_not_create_or_change_identities(self):
        unknown = {**self.entry, 'name': 'Unknown character'}
        self.write([unknown, self.entry, copy.deepcopy(self.entry)])
        people = performers(self.root)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]['cv'], [])
        self.assertFalse(people[0]['color'])

    def test_broken_optional_snapshot_keeps_dictionary_usable(self):
        self.write()
        (self.root / 'imas_db_people.json').write_text('{broken', 'utf-8')
        self.assertEqual(performers(self.root)[0]['name'], '萩原雪歩')

    def test_reviewed_expanded_name_matches_without_guessing_other_nicknames(self):
        self.names[0]['name'] = 'エミリー スチュアート'
        self.entry.update(name='エミリー', match_name='エミリー スチュアート',
                          identity_source_urls=['https://imas-db.jp/song/cd/255.html'])
        self.write();person = performers(self.root)[0]
        self.assertIn('https://imas-db.jp/song/cd/255.html', person['source_urls'])
        self.assertTrue(person['voice_actor_history'])

    def test_updated_evidence_path_and_broken_update_bundle_fallback(self):
        self.write()
        update = self.root / 'data' / 'people.json';update.parent.mkdir()
        data = json.loads((self.root / 'imas_db_people.json').read_text('utf-8'))
        data['characters'][0]['color'] = '#123456'
        update.write_text(json.dumps(data), 'utf-8')
        self.assertEqual(performers(self.root, database_path=update)[0]['color'], '#123456')
        update.write_text('{broken', 'utf-8')
        self.assertEqual(performers(self.root, database_path=update)[0]['color'], '#d3dde9')


class RuntimeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'imas_db_people.json'
        self.path.write_text('{"characters": [{"name": "preserved"}]}', 'utf-8')
        self.before = self.path.read_bytes()
        self.cv = '<table>' + ''.join(cv_row('Actor ' + str(i), 'Idol ' + str(i), str(i)) for i in range(300)) + '</table>'
        self.colors = ('<table><thead><tr><th>アイドル(CV)</th></tr></thead><tbody>'
                       + ''.join(f'<tr><td>Idol {i}</td><td>#123456</td></tr>' for i in range(300))
                       + '</tbody></table>')

    def test_two_valid_sources_replace_snapshot_and_return_actual_counts(self):
        fetcher = Mock(side_effect=[self.cv, self.colors]);progress = Mock()
        result = refresh_people(self.path, progress=progress, fetcher=fetcher)
        self.assertTrue(result['updated']);self.assertEqual(result['characters'], 300)
        self.assertEqual(len(json.loads(self.path.read_text('utf-8'))['characters']), 300)
        self.assertEqual(fetcher.call_count, 2);self.assertEqual(progress.call_count, 2)

    def test_partial_fetch_or_invalid_source_keep_old_file_byte_for_byte(self):
        for responses in ([self.cv, OSError('offline')], ['<html>maintenance</html>', self.colors], [self.cv, '']):
            with self.subTest(responses=str(responses)[:50]):
                result = refresh_people(self.path, fetcher=Mock(side_effect=responses))
                self.assertFalse(result['updated']);self.assertTrue(result['errors'])
                self.assertEqual(result['characters'], 1)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_cancel_before_or_after_network_never_replaces_old_file(self):
        event = threading.Event();event.set();fetcher = Mock()
        self.assertTrue(refresh_people(self.path, cancel=event, fetcher=fetcher)['cancelled'])
        fetcher.assert_not_called();event.clear()
        def cancelling(url, cancel=None):event.set();return self.cv
        self.assertTrue(refresh_people(self.path, cancel=event, fetcher=cancelling)['cancelled'])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_failed_atomic_save_preserves_prior_snapshot(self):
        with patch('imas_people_sources.atomic_json', side_effect=OSError('disk unavailable')):
            result = refresh_people(self.path, fetcher=Mock(side_effect=[self.cv, self.colors]))
        self.assertFalse(result['updated']);self.assertTrue(result['errors'])
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == '__main__':unittest.main()
