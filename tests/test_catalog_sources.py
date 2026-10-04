import io
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
import zipfile

from catalog_sources import (CatalogCancelled, catalog_queries, compact_mb_response, enrich_performers,
                             fetch_bytes, fetch_musicbrainz, is_franchise_release,
                             normalize_musicbrainz, parse_imasparql, recording_kind,
                             reclassify_unknown_series, recording_series_evidence, series_from_release)


MBID = '11111111-2222-3333-4444-555555555555'


def recording(**changes):
    value = {'id': MBID, 'title': 'Twinkle way', 'length': 245000,
             'artist-credit': [{'name': 'イルミネーションスターズ',
                                'artist': {'id': 'artist-1', 'name': 'イルミネーションスターズ'}}],
             'releases': [{'id': 'release-1', 'title': 'THE IDOLM@STER SHINY COLORS GR@DATE WING 02',
                           'status': 'Official', 'date': '2020-10-14'}],
             'isrcs': ['JPTEST0000001'], 'first-release-date': '2020-10-14'}
    value.update(changes)
    return value


def rdf_archive():
    start = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
             'xmlns:schema="http://schema.org/" '
             'xmlns:imas="https://sparql.crssnky.xyz/imasrdf/URIs/imas-schema.ttl#">')
    idol = ('<rdf:Description rdf:about="Sakuragi_Mano"><schema:name>櫻木真乃</schema:name>'
            '<rdf:type rdf:resource="https://example.test/Idol"/>'
            '<imas:cv>関根瞳</imas:cv><imas:nameKana>さくらぎまの</imas:nameKana>'
            '<schema:description>Do not import this prose.</schema:description></rdf:Description>')
    group = ('<rdf:Description rdf:about="illuminationSTARS"><schema:name>illuminationSTARS</schema:name>'
             '<rdf:type rdf:resource="https://example.test/Unit"/>'
             '<schema:member rdf:resource="Sakuragi_Mano"/></rdf:Description>')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('imasparql-master/RDFs/283.rdf', start + idol + '</rdf:RDF>')
        archive.writestr('imasparql-master/RDFs/Units/ShinyColors.rdf', start + group + '</rdf:RDF>')
        archive.writestr('../../untrusted.py', 'never extract')
    return data.getvalue()


class SourceTests(unittest.TestCase):
    def test_factual_recording_retains_identity_and_provenance(self):
        value = normalize_musicbrainz([recording()], fetched_at='2026-10-02')[0]
        self.assertEqual(value['id'], 'mb:' + MBID)
        self.assertEqual(value['series'], ['shiny'])
        self.assertEqual(value['duration'], 245)
        self.assertEqual(value['recordings'][0]['isrcs'], ['JPTEST0000001'])
        self.assertEqual(value['source_url'], 'https://musicbrainz.org/recording/' + MBID)
        self.assertIn('work_link', value['missing'])
        self.assertNotIn('work_id', value)
        self.assertEqual(value['provenance'][0]['license'], 'CC0-1.0')

    def test_unrelated_release_or_bootleg_does_not_enter_catalog(self):
        for title, status in [('An unrelated album', 'Official'),
                              ('THE IDOLM@STER Fan Remix', 'Bootleg'),
                              ('A Tribute to THE IDOLM@STER', 'Official')]:
            with self.subTest(title=title):
                value = recording(releases=[{'title': title, 'status': status}])
                self.assertEqual(normalize_musicbrainz([value]), [])

    def test_gakuen_credit_is_explicit_and_needs_no_album_prefix(self):
        value = recording(**{'artist-credit': [{'name': '初星学園', 'artist': {'name': '初星学園'}}],
                              'releases': [{'id': 'r1', 'title': 'Fighting My Way', 'status': 'Official'}]})
        self.assertEqual(normalize_musicbrainz([value])[0]['series'], ['gakuen'])

    def test_gakuen_queries_include_individual_idol_credits(self):
        queries = catalog_queries([{'name': '花海咲季', 'kind': 'idol', 'series': ['gakuen']},
                                   {'name': '天海春香', 'kind': 'idol', 'series': ['765']}])
        self.assertIn('artistname:"花海咲季"', queries[-1])
        self.assertNotIn('天海春香', queries[-1])

    def test_neutral_album_discovery_includes_other_known_characters_and_named_units(self):
        queries = catalog_queries([{'name': '天海春香', 'kind': 'idol', 'series': ['765']}])
        self.assertTrue(any('artist:"THE IDOLM@STER"' in query for query in queries))
        self.assertTrue(any('artistname:"天海春香"' in query and 'NOT release:' in query for query in queries))

    def test_neutral_release_with_explicit_character_identity_keeps_versions(self):
        credits = [{'name': 'Known character', 'artist': {'id': MBID, 'name': 'Known character',
                     'disambiguation': 'THE IDOLM@STER character'}}]
        for title, kind in [('A new song', 'song'), ('A new song (オリジナル・カラオケ)', 'instrumental')]:
            with self.subTest(title=title):
                value = recording(title=title, releases=[{'id': 'release-1', 'title': 'Neutral album', 'status': 'Official'}],
                                  **{'artist-credit': credits})
                song = normalize_musicbrainz([value])[0]
                self.assertEqual(song['title'], title);self.assertEqual(song['kind'], kind)
                self.assertEqual(song['series'], ['other'])
                self.assertEqual(song['franchise_artist_evidence'][0]['id'], MBID)
                self.assertEqual(song['releases'][0]['title'], 'Neutral album')

    def test_neutral_actor_album_and_franchise_cover_acts_stay_excluded(self):
        for comment, kind in [('voice actor', None), ('THE IDOLM@STER voice actor', None),
                              ('THE IDOLM@STER', 'Person'), ('THE IDOLM@STER cover band', None),
                              ('THE IDOLM@STER unofficial fan unit', None)]:
            with self.subTest(comment=comment, kind=kind):
                credit = {'name': 'Neutral Actor', 'artist': {'id': MBID, 'name': 'Neutral Actor',
                          'disambiguation': comment, 'type': kind}}
                value = recording(releases=[{'id':'r', 'title':'Actor personal album', 'status':'Official'}],
                                  **{'artist-credit': [credit]})
                self.assertEqual(normalize_musicbrainz([value]), [])

    def test_explicit_franchise_credit_never_makes_bootleg_or_unreleased_official(self):
        for artist in ({'name': '初星学園'}, {'name': 'THE IDOLM@STER FIVE STARS!!!!!',
                                         'disambiguation': 'THE IDOLM@STER'}):
            for status in ('Bootleg', 'Pseudo-Release', None):
                value = recording(releases=[{'id':'r', 'title':'Neutral album', 'status':status}],
                                  **{'artist-credit': [{'name':artist['name'], 'artist':artist}]})
                self.assertEqual(normalize_musicbrainz([value]), [])

    def test_unknown_album_brand_does_not_become_765_from_singer(self):
        value = recording(**{'artist-credit': [{'name': '天海春香', 'artist': {'name': '天海春香'}}],
                              'releases': [{'id': 'r1', 'title': 'THE IDOLM@STER BRILLIANT STARS',
                                            'status': 'Official'}]})
        self.assertEqual(normalize_musicbrainz([value])[0]['series'], ['other'])

    def test_shared_765_singers_do_not_expand_million_brand(self):
        self.assertEqual(series_from_release('THE IDOLM@STER 765 MILLION ALLSTARS BEST'), ['million'])
        self.assertEqual(series_from_release('THE IDOLM@STER MASTER ARTIST 4'), ['765'])

    def test_reviewed_as_release_branding_does_not_classify_crossovers(self):
        for name in ('THE IDOLM@STER STELLA MASTER 02 星彩ステッパー',
                     'THE IDOLM@STER BEST ALBUM ～MASTER OF MASTER～',
                     'THE IDOLM@STER 765PRO ALLSTARS LIVE SUNRICH COLORFUL',
                     'THE IDOLM@STER M@STERS OF IDOL WORLD 2025【765PRO ALLSTARS】'):
            self.assertEqual(series_from_release(name), ['765'])
        for name in ('THE IDOLM@STER STARLIT SEASON 03',
                     'THE IDOLM@STER M@STERS OF IDOL WORLD 2025',
                     'THE IDOLM@STER LIVE 【961PRODUCTION & KOHAKU & 765PRO ALLSTARS】'):
            self.assertEqual(series_from_release(name), [])

    def test_starlit_masterpiece_uses_exact_official_track_evidence(self):
        release = {'id': 'starlit-03', 'title': 'THE IDOLM@STER STARLIT SEASON 03', 'status': 'Official'}
        value = normalize_musicbrainz([recording(title='M@STERPIECE', releases=[release])])[0]
        self.assertEqual(value['series'], ['765'])
        self.assertEqual(value['series_source_urls'], ['https://catalog.bandainamcomusiclive.co.jp/release/70023/'])
        for title in ('M@STERPIECE (New Remix)', 'READY!!', 'Spread the Wings!!', '全力★ドリーミングガールズ'):
            value = normalize_musicbrainz([recording(title=title, releases=[release])])[0]
            self.assertEqual(value['series'], ['other'])
        release['title'] = 'THE IDOLM@STER ORCHESTRA CONCERT'
        self.assertEqual(normalize_musicbrainz([recording(title='M@STERPIECE', releases=[release])])[0]['series'], ['other'])

    def test_change_single_classification_requires_reviewed_release_id_and_track(self):
        releases = [
            ('f0851cf5-b32d-4a2b-a6ea-c3025588aa03', 'https://columbia.jp/prod-info/COCC-16516/'),
            ('c8144507-62ab-4799-942a-4b4f101e604e', 'https://columbia.jp/prod-info/COZC-597-8/'),
        ]
        for identifier, source in releases:
            release = {'id': identifier, 'title': 'CHANGE!!!!'}
            for title in ('We just started', '今 スタート！', 'CHANGE!!!! (M@STER VERSION) Instrumental'):
                with self.subTest(identifier=identifier, title=title):
                    brands, names, urls = recording_series_evidence(title, [release])
                    self.assertEqual(brands, ['765'])
                    self.assertEqual(names, ['CHANGE!!!!'])
                    self.assertEqual(urls, [source])
            for title in ('We just started (New Remix)', 'READY!!'):
                self.assertEqual(recording_series_evidence(title, [release])[0], [])
            self.assertEqual(recording_series_evidence('We just started',
                [{**release, 'id': 'unreviewed-release'}])[0], [])
            self.assertEqual(recording_series_evidence('We just started',
                [{**release, 'title': 'Another album'}])[0], [])

    def test_change_single_cached_repair_keeps_instrumental_kind_and_recording_identity(self):
        item = {'id': 'mb:7aff5f43-62bb-4f79-acb6-050525650fd8', 'source_id': 'musicbrainz',
                'title': 'CHANGE!!!! (M@STER VERSION) Instrumental', 'series': ['other'],
                'kind': 'instrumental', 'artists': ['765PRO ALLSTARS'],
                'releases': [{'id': 'f0851cf5-b32d-4a2b-a6ea-c3025588aa03', 'title': 'CHANGE!!!!'}]}
        before = json.loads(json.dumps(item))
        self.assertEqual(reclassify_unknown_series([item]), 1)
        self.assertEqual(item['series'], ['765'])
        for field in ('id', 'kind', 'artists', 'releases'):
            self.assertEqual(item[field], before[field])
        self.assertEqual(reclassify_unknown_series([item]), 0)

    def test_cached_series_repair_keeps_recording_facts_and_is_idempotent(self):
        item = {'id': 'mb:' + MBID, 'source_id': 'musicbrainz', 'title': 'M@STERPIECE', 'series': ['other'],
                'artists': ['Official original credit'], 'duration': 346, 'missing': ['series', 'spotify'],
                'recordings': [{'id': 'mb:' + MBID, 'isrcs': ['JPI102200059']}],
                'releases': [{'id': 'original-release', 'title': 'THE IDOLM@STER STARLIT SEASON 03'}]}
        before = json.loads(json.dumps(item))
        self.assertEqual(reclassify_unknown_series([item]), 1)
        self.assertEqual(item['series'], ['765'])
        self.assertEqual(item['missing'], ['spotify'])
        for field in ('id', 'artists', 'duration', 'recordings', 'releases'):
            self.assertEqual(item[field], before[field])
        self.assertEqual(reclassify_unknown_series([item]), 0)
        item['series'] = ['million']
        self.assertEqual(reclassify_unknown_series([item]), 0)
        self.assertEqual(item['series'], ['million'])

    def test_recording_type_avoids_song_title_substring_false_positives(self):
        self.assertEqual(recording_kind('青い珊瑚礁 (オリジナル・カラオケ)'), 'instrumental')
        self.assertEqual(recording_kind('ドラマ「新しい世界」'), 'spoken')
        self.assertEqual(recording_kind('トーク01'), 'spoken')
        self.assertEqual(recording_kind('トワイライト・モノローグ'), 'song')
        self.assertEqual(recording_kind('ドラマチックノンフィクション'), 'song')
        self.assertEqual(recording_kind("!!!ASAMI'S TRAVEL!!!", ['[dialogue]']), 'spoken')
        self.assertEqual(recording_kind('Dialogue', ['Singer']), 'song')

    def test_drama_context_distinguishes_explicit_speech_from_unknown_bonus_tracks(self):
        release = [{'title': 'アイドルマスター ドラマCD Scene.01'}]
        self.assertEqual(recording_kind('Chapter.3 出会いと発見', releases=release), 'spoken')
        self.assertEqual(recording_kind('Bonus Track', releases=release), 'unknown')
        self.assertEqual(recording_kind('Bonus Track (off vocal)', releases=release), 'instrumental')
        self.assertEqual(recording_kind('なんちゃってコメンタリー二十六話'), 'spoken')
        self.assertEqual(recording_kind('またのご来園を！【ドラマ】'), 'spoken')
        self.assertEqual(recording_kind('Chapter 3'), 'song')

    def test_artist_unit_relationship_uses_exact_ids_and_omits_prose(self):
        songs, people = parse_imasparql(rdf_archive())
        self.assertEqual(songs, [])
        group = next(p for p in people if p['kind'] == 'group')
        self.assertEqual(group['members'], ['櫻木真乃'])
        self.assertEqual(group['members_ids'], ['imas:Sakuragi_Mano'])
        self.assertNotIn('description', str(people))

    def test_group_search_links_member_without_changing_credited_artists(self):
        songs = [{'artists': ['illuminationSTARS']}]
        _, people = parse_imasparql(rdf_archive())
        enrich_performers(songs, people)
        self.assertEqual(songs[0]['artists'], ['illuminationSTARS'])
        self.assertIn('櫻木真乃', songs[0]['performer_aliases'])
        self.assertEqual(songs[0]['groups'], ['illuminationSTARS'])

    def test_conflicting_alias_never_selects_a_performer(self):
        people = [{'id': 'a', 'name': 'A', 'aliases': ['Alias'], 'kind': 'idol'},
                  {'id': 'b', 'name': 'B', 'aliases': ['Alias'], 'kind': 'idol'}]
        songs = [{'artists': ['Alias']}]
        enrich_performers(songs, people)
        self.assertEqual(songs[0]['performer_ids'], [])

    def test_only_core_fields_are_cached(self):
        value = recording(tags=[{'name': 'rock'}], annotation='comment', rating={'value': 5})
        compact = compact_mb_response({'count': 1, 'offset': 0, 'recordings': [value]})
        self.assertNotIn('tags', compact['recordings'][0])
        self.assertNotIn('annotation', compact['recordings'][0])
        self.assertNotIn('rating', compact['recordings'][0])

    def test_cancel_before_http_request(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CatalogCancelled):
            fetch_bytes('https://example.test', cancel=cancel,
                        opener=lambda *a, **k: self.fail('HTTP request must not happen'))

    def test_search_shards_are_single_page_and_complete(self):
        identifiers = [f'{n * ((1 << 128) // 180):032x}' for n in range(180)]
        identifiers = [s[:8] + '-' + s[8:12] + '-' + s[12:16] + '-' + s[16:20] + '-' + s[20:]
                       for s in identifiers]
        calls = []
        def fetch(url, **kwargs):
            q = parse_qs(urlsplit(url).query)
            query = q['query'][0]
            calls.append(query)
            self.assertEqual(q['offset'], ['0'])
            chosen = identifiers
            if 'rid:[' in query:
                low, high = query.split('rid:[', 1)[1].rstrip(']').split(' TO ')
                chosen = [i for i in identifiers if low <= i <= high]
            return json.dumps({'count': len(chosen), 'offset': 0,
                               'recordings': [recording(id=i) for i in chosen[:100]]}).encode()
        result, counts = fetch_musicbrainz(fetcher=fetch, queries=['release:test'], interval=0)
        self.assertEqual(len(result), 180)
        self.assertEqual(counts[0]['downloaded'], 180)
        self.assertGreater(len(calls), 1)

    def test_duplicate_search_rows_cannot_satisfy_the_reported_count(self):
        response = {'count': 2, 'offset': 0, 'recordings': [recording(), recording()]}
        with self.assertRaisesRegex(ValueError, 'MusicBrainz'):
            fetch_musicbrainz(fetcher=lambda *args, **kwargs: json.dumps(response).encode(),
                              queries=['release:test'], interval=0)

    def test_search_shards_must_reconcile_with_the_initial_total(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)['query'][0]
            # The source changes after the initial count. Empty shards must
            # never be published as a successfully completed 101-record query.
            return json.dumps({'count': 0 if 'rid:[' in query else 101,
                               'offset': 0, 'recordings': []}).encode()
        with self.assertRaisesRegex(ValueError, 'MusicBrainz'):
            fetch_musicbrainz(fetcher=fetch, queries=['release:test'], interval=0)

    def test_search_rejects_a_page_from_a_different_offset(self):
        response = {'count': 1, 'offset': 100, 'recordings': [recording()]}
        with self.assertRaisesRegex(ValueError, 'MusicBrainz'):
            fetch_musicbrainz(fetcher=lambda *args, **kwargs: json.dumps(response).encode(),
                              queries=['release:test'], interval=0)

    def test_search_requires_nonnegative_integer_counts(self):
        for field in ('count', 'offset'):
            for bad in (None, True, -1, 1.5, '1'):
                with self.subTest(field=field, bad=bad):
                    response = {'count': 1, 'offset': 0, 'recordings': [recording()], field: bad}
                    with self.assertRaisesRegex(ValueError, 'MusicBrainz'):
                        compact_mb_response(response)

    def test_same_recording_in_independent_queries_is_legitimate(self):
        response = {'count': 1, 'offset': 0, 'recordings': [recording()]}
        result, counts = fetch_musicbrainz(
            fetcher=lambda *args, **kwargs: json.dumps(response).encode(),
            queries=['release:first', 'artist:second'], interval=0)
        self.assertEqual(len(result), 1)
        self.assertEqual([row['downloaded'] for row in counts], [1, 1])


if __name__ == '__main__':
    unittest.main()
