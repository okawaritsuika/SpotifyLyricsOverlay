"""Exercise Electron dictionary credits and combined recording filters."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from artist_names import name_key
from catalog_workspace import CatalogWorkspace
from explorer_library import ExplorerLibrary
from song_catalog import SongCatalog
from tests.test_song_catalog import song, write_catalog


class CatalogWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        store=SimpleNamespace(directory=self.root)
        self.workspace=CatalogWorkspace(SimpleNamespace(store=store))
        self.addCleanup(self.workspace.close)

    def test_album_index_opens_both_directions_and_keeps_distinct_editions(self):
        def release(ident, date='2025-01-22'):
            return dict(id=ident,title='EXAMPLE ECHOES',date=date,url='https://musicbrainz.org/release/'+ident)

        tracks=[song('a','First',releases=[release('cd'),release('same')]),
                song('b','Second',releases=[release('cd'),release('same')]),
                song('c','Revised',releases=[release('new','2026-11-15')]),
                song('d','Single',series=['million'],artists=['春日未来'],album='EXAMPLE ECHOES'),
                song('e','Streaming',album='Another Album',releases=[dict(title='Another Album',
                    source_url='https://open.spotify.com/track/'+'a'*22)])]
        bundled=self.root/'albums.json'
        write_catalog(bundled,tracks)
        with patch('catalog_workspace.SongCatalog',return_value=SongCatalog(bundled)):
            rows=self.workspace.dispatch('albums',dict(query='example echoes',series='shiny'))['items']
            self.assertEqual([item['trackCount'] for item in rows],[2,1])
            detail=self.workspace.dispatch('album',dict(id=rows[0]['id']))
            self.assertEqual([item['id'] for item in detail['tracks']],['a','b'])
            self.assertEqual(self.workspace.dispatch('detail',dict(id='a'))['releases'],[rows[0]])
            other=self.workspace.dispatch('albums',dict(query='example echoes',series='million'))['items']
            self.assertEqual(len(other),1)
            self.assertNotIn(other[0]['id'],[item['id'] for item in rows])
            streaming=self.workspace.dispatch('albums',dict(query='another album'))['items'][0]
            self.assertIsNone(self.workspace.dispatch('album',dict(id=streaming['id']))['sourceUrl'])

    def test_japanese_singer_colors_and_voice_actors(self):
        expected={
            '765':('天海春香','中村繪里子'),
            'million':('春日未来','山崎はるか'),
            'cinderella':('島村卯月','大橋彩香'),
            'sidem':('天道輝','仲村宗悟'),
            'shiny':('櫻木真乃','関根瞳'),
            'gakuen':('花海咲季','長月あおい'),
        }
        for series,(name,voice) in expected.items():
            with self.subTest(series=series):
                singers=self.workspace.dispatch('people',dict(kind='idol',series=series,query=voice))
                singer=next(item for item in singers['items'] if name_key(item['title'])==name)
                self.assertRegex(singer['color'],r'^#[a-f0-9]{6}$')
                self.assertIn(voice,[name_key(credit) for credit in singer['artists']])
                self.assertTrue(singer['id'].startswith('idol:'))

    def test_song_uses_explicit_singer_credits_and_separate_group_rosters(self):
        group='イルミネーションスターズ'
        singers=self.workspace.performer_credits([group,'櫻木真乃（CV. 関根瞳）','関根瞳'])
        self.assertEqual([item['title'] for item in singers],[group,'櫻木真乃'])
        self.assertEqual(singers[1]['artists'],['関根瞳'])
        self.assertEqual(self.workspace.credited_rosters(singers),[])

        groups=self.workspace.dispatch('people',dict(kind='group',query=group))
        unit=next(item for item in groups['items'] if item['title']==group)
        detail=self.workspace.dispatch('person',dict(id=unit['id']))
        self.assertEqual([member['title'] for member in unit['memberPreview']],['櫻木真乃','風野灯織','八宮めぐる'])
        self.assertEqual(unit['memberCount'],3)
        self.assertEqual(len(detail['trackPreview']),4)
        matches=self.workspace.group_songs(self.workspace.songs().songs,self.workspace.get_person(unit['id']))
        self.assertTrue({track['id'] for track in detail['trackPreview']} <= {track['id'] for track in matches})
        self.assertEqual({member['title'] for member in detail['members']},{'櫻木真乃','風野灯織','八宮めぐる'})
        self.assertTrue(all(member['color'] for member in detail['members']))
        self.assertTrue(all(member['id'] for member in detail['members']))
        row=self.workspace.song_row(song(),ExplorerLibrary(self.root))
        self.assertEqual([credit['kind'] for credit in row['credits']],['그룹'])
        self.assertEqual({member['title'] for member in row['rosterPreview']},{'櫻木真乃','風野灯織','八宮めぐる'})

    def test_recording_and_person_keep_original_credits_and_expose_korean_voices(self):
        recording=self.workspace.performer_credits(['LUCA IKARUGA (CV: Rina Kawaguchi)'])[0]
        person=self.workspace.dispatch('person',dict(id='idol:斑鳩ルカ'))
        self.assertEqual(recording['localizedName'],'이카루가 루카')
        self.assertEqual(person['title'],'斑鳩ルカ')
        self.assertEqual(person['artists'],['川口莉奈'])
        self.assertEqual(recording['localizedVoices'],['카와구치 리나'])
        self.assertEqual(person['localizedVoices'],recording['localizedVoices'])
        explicit=self.workspace.performer_credits(['Unknown performer (CV: Rina Kawaguchi)'])[0]
        self.assertEqual(explicit['localizedVoices'],['카와구치 리나'])
        unknown=self.workspace.performer_credits(['Unknown performer'])[0]
        self.assertEqual(unknown['localizedVoices'],[])

    def test_catalog_update_routes_to_github_dialog(self):
        with patch.object(self.workspace.app,'open_updates',create=True) as updates:
            self.assertTrue(self.workspace.dispatch('updates',{}))
        updates.assert_called_once_with()
        for old_action in ('refresh','refreshInfo'):
            with self.assertRaises(ValueError):
                self.workspace.dispatch(old_action,{'kind':'music'})

    def test_add_song_becomes_searchable_and_can_be_favorited(self):
        from song_catalog import PERSONAL_CATALOG_NAME

        public=self.root/'song_catalog.json'
        write_catalog(public,[song('official','Existing Example')])
        before=public.read_bytes()
        fields=dict(title='Personal Example Song',artists=['Test Artist'],album='Test Album',
                    release_date='2026-11-05',source_url='https://example.test/release',
                    spotify_url='https://open.spotify.com/track/'+'z'*22,series='other')
        added=self.workspace.dispatch('addSong',fields)
        self.assertTrue(added['added'])
        self.assertTrue((self.root/PERSONAL_CATALOG_NAME).is_file())
        self.assertEqual(public.read_bytes(),before)
        self.assertEqual(self.workspace.dispatch('addSong',fields),dict(id=added['id'],added=False))
        detail=self.workspace.dispatch('detail',dict(id=added['id']))
        self.assertTrue(detail['personal'])
        self.assertTrue(detail['spotifyLinked'])
        self.assertEqual(detail['sourceUrl'],fields['source_url'])
        self.assertEqual(detail['releaseDate'],fields['release_date'])
        self.assertTrue(self.workspace.dispatch('favorite',dict(id=added['id']))['favorite'])
        items=self.workspace.dispatch('list',dict(query=fields['title'],favorites=True))['items']
        self.assertEqual([item['id'] for item in items],[added['id']])

    def test_idol_index_starts_with_the_brand_roster(self):
        expected={'765':('天海春香','如月千早','星井美希'),'million':('春日未来','最上静香','伊吹翼'),
                  'cinderella':('島村卯月','渋谷凛','本田未央'),'sidem':('天道輝','桜庭薫','柏木翼'),
                  'shiny':('櫻木真乃','風野灯織','八宮めぐる'),'gakuen':('花海咲季','月村手毬','藤田ことね')}
        for series,names in expected.items():
            with self.subTest(series=series):
                rows=self.workspace.dispatch('people',dict(kind='idol',series=series))['items']
                self.assertEqual(tuple(name_key(row['title']) for row in rows[:3]),names)

    def test_group_spotify_brand_query_and_sort_apply_together(self):
        def track_id(character):return character*22
        def url(character):return 'https://open.spotify.com/track/'+track_id(character)
        candidates=[song('explicit-spotify','Group Alpha',spotify_url=url('a')),
                    song('linked','Group Zulu'),song('single','Group Solo',artists=['櫻木真乃'],spotify_url=url('b')),
                    song('unknown','Group Unknown'),song('excluded','Group Excluded',spotify_url=url('d')),
                    song('restricted','Group Restricted'),song('other-series','Group Other',series=['other'],spotify_url=url('f')),
                    song('other-title','Nothing',spotify_url=url('g'))]
        bundled=self.root/'base.json'
        write_catalog(bundled,candidates)
        library=ExplorerLibrary(self.root)
        library.link_spotify('linked',dict(id=track_id('h'),is_playable=True))
        library.link_spotify('restricted',dict(id=track_id('j'),is_playable=False,restriction='market'))
        library.unlink_spotify('excluded')
        with patch('catalog_workspace.SongCatalog',return_value=SongCatalog(bundled)):
            group=next(row for row in self.workspace.dispatch('people',dict(kind='group',query='イルミネーションスターズ'))['items']
                       if row['title']=='イルミネーションスターズ')
            params=dict(kind='song',series='shiny',query='Group',group=group['id'],sort='title_desc',playable=True)
            filtered=self.workspace.dispatch('list',params)
            self.assertEqual([row['id'] for row in filtered['items']],['linked','explicit-spotify'])
            filtered=self.workspace.dispatch('list',{**params,'playable':False})
            self.assertEqual({row['id'] for row in filtered['items']},
                             {'linked','explicit-spotify','unknown','excluded','restricted'})

    def test_shiny_group_list_and_member_details_follow_official_categories(self):
        groups=self.workspace.dispatch('people',dict(kind='group',series='shiny'))['items']
        regular=['イルミネーションスターズ','アンティーカ','放課後クライマックスガールズ','アルストロメリア',
                 'ストレイライト','ノクチル','シーズ','コメティック']
        self.assertEqual([row['title'] for row in groups[:9]],regular+['シャイニーカラーズ'])
        self.assertEqual([row['groupCategory'] for row in groups[:9]],['unit']*8+['project'])
        self.assertTrue(all(row['groupCategory']=='collaboration' for row in groups[9:]))
        self.assertNotIn('斑鳩ルカ',[row['title'] for row in groups])
        project=self.workspace.dispatch('person',dict(id=groups[8]['id']))
        self.assertEqual([row['title'] for row in project['units']],regular)
        self.assertEqual(len(project['members']),28)
        luca=self.workspace.dispatch('person',dict(id='idol:斑鳩ルカ'))
        self.assertEqual([row['title'] for row in luca['units']],['コメティック'])
        self.assertEqual([name_key(voice) for voice in luca['artists']],['川口莉奈'])

    def test_gakuen_groups_include_documented_duets_without_reassigning_credits(self):
        groups=self.workspace.dispatch('people',dict(kind='group',series='gakuen'))
        rows=groups['items']
        self.assertEqual(len(rows),groups['total'])
        self.assertEqual(len({row['id'] for row in rows}),19)
        self.assertFalse(any(row['groupCategory']=='featured' for row in rows))
        named=next(row for row in rows if row['title']=='ゆめぱしー')
        expected={'倉本千奈','篠澤広'}
        self.assertEqual({member['title'] for member in named['memberPreview']},expected)

        def recordings(group):
            filters=dict(group=group['id'],series='gakuen',kind='song',duplicates=False)
            return self.workspace.dispatch('list',filters)['items']

        duet_songs=recordings(named)
        self.assertEqual(len(duet_songs),4)
        self.assertIn('みちなるひろがる',[row['title'] for row in duet_songs])
        regional=self.workspace.get_song('mb:048b6b62-7928-4391-8528-9294223c607b')
        self.assertEqual(regional['title'],'古今東西ちょちょいのちょい [長野県ver.]')
        original=self.workspace.performer_credits(['倉本千奈 × 篠澤広'])
        self.assertEqual({credit['title'] for credit in original},expected)

        live=next(row for row in rows if row['title']=='乖轟')
        detail=self.workspace.dispatch('person',dict(id=live['id']))
        self.assertEqual({member['title'] for member in detail['members']},{'雨夜燕','有村麻央'})
        self.assertEqual([event['performanceTitle'] for event in detail['liveEvents']],
                         ['アンバランス','アンバランス'])
        self.assertEqual(detail['trackPreview'],[])
        group=next(row for row in rows if row['title']=='1年1組')
        self.assertEqual([row['title'] for row in recordings(group)],['SUPREMACY'])

    def test_shiny_recordings_distinguish_actual_unit_joint_solo_and_project_credits(self):
        groups={row['title']:row['id'] for row in self.workspace.dispatch(
            'people',dict(kind='group',series='shiny'))['items']}
        def matches(group,query,**filters):
            return self.workspace.dispatch('list',dict(group=groups[group],query=query,series='shiny',
                kind='song',duplicates=False,**filters))['items']

        co, al='コメティック','アルストロメリア'
        for group in (co,al):
            title=matches(group,'リフレインブレイン')
            self.assertEqual([row['title'] for row in title],['リフレインブレイン'])
            self.assertEqual([credit['title'] for credit in title[0]['credits']],[al,co])
        self.assertEqual(matches(co,'ルピラ'),[])
        self.assertEqual(matches(al,'似セ者'),[])
        self.assertEqual([row['title'] for row in matches(co,'似セ者')],['似セ者'])
        self.assertEqual(matches(co,'1/3'),[])
        self.assertEqual(matches('イルミネーションスターズ','Shiny Stories'),[])
        project=matches('シャイニーカラーズ','Migratory Echoes')
        self.assertEqual([row['title'] for row in project],['Migratory Echoes'])
        self.assertEqual(len([credit for credit in project[0]['credits'] if credit['kind']=='아이돌']),28)
        self.assertEqual(project[0]['rosterPreview'],[])
        for group in (co,'シーズ','アンティーカ','イルミネーションスターズ'):
            with self.subTest(group=group):
                unit=matches(group,'Migratory Echoes',playable=True)
                self.assertEqual(len(unit),1)
                self.assertTrue(unit[0]['spotify'])
                self.assertEqual([credit['title'] for credit in unit[0]['credits']],[group])

    def test_compact_joint_credit_and_guest_do_not_expand_guest_unit(self):
        groups=self.workspace.dispatch('people',dict(kind='group',series='shiny'))['items']
        co=next(row['id'] for row in groups if row['title']=='コメティック')
        song_joint=song('joint','Example',artists=['ALSTROEMERIA/CoMETIK'])
        solo=song('solo','Example',artists=['斑鳩ルカ'],album='COLORFUL FE@THERS -CoMETIK-')
        results=self.workspace.group_songs([song_joint,solo],self.workspace.get_person(co))
        self.assertEqual(results,[song_joint])
        row=self.workspace.song_row(song_joint,ExplorerLibrary(self.root))
        self.assertEqual([credit['title'] for credit in row['credits']],['アルストロメリア','コメティック'])
        guest=song('guest','LINKs (283 Production LIVE Performance Uka, Ver.)',
                   artists=['イルミネーションスターズ','黛冬優子'])
        row=self.workspace.song_row(guest,ExplorerLibrary(self.root))
        self.assertEqual([credit['title'] for credit in row['credits']],['イルミネーションスターズ','黛冬優子'])
        self.assertEqual({member['title'] for member in row['rosterPreview']},
                         {'櫻木真乃','風野灯織','八宮めぐる'})

    def test_live_color_cast_roles_and_stage_recording_are_separate(self):
        shows=self.workspace.dispatch('lives',dict(year='2026'))['items']
        finale=next(show for show in shows if show['id']=='imasdb-72006e30af7f25dc9ed3')
        self.assertGreaterEqual(finale['artistCount'],len(finale['credits']))
        self.assertLessEqual(len(finale['credits']),8)
        detail=self.workspace.dispatch('live',dict(id=finale['id']))
        song=detail['songs'][0]
        self.assertEqual(song['artists'],['川村玲奈'])
        self.assertEqual((song['credits'][0]['title'],song['credits'][0]['color']),('篠澤広','#00afcc'))
        self.assertEqual(detail['artistCount'],len(detail['credits']))
        self.assertTrue(any(credit.get('collective') for row in detail['songs'] for credit in row['credits']))
        self.assertFalse(any(credit.get('collective') for credit in detail['credits']))

        show=self.workspace.get_live('imasdb-c1c6202d1fe8d319363e')
        row=next(row for row in show['songs'] if row['title']=='銀翼のアレジアンス -blade of truth-')
        expected=[('アンティーカ',''),('シーズ','코러스 · 백댄스'),('コメティック','코러스 · 백댄스')]
        self.assertEqual([(item['title'],item['role']) for item in self.workspace.live_credits(row,show)],expected)

        show=self.workspace.dispatch('live',dict(id='valiv-letora-birthday-2026'))
        self.assertEqual(len(show['songs']),9)
        self.assertTrue(all(row['matchCount'] for row in show['songs']))
        cover=next(row for row in show['songs'] if row['title']=='理論武装して')
        self.assertEqual([credit['title'] for credit in cover['credits']],['レトラ'])
        result=self.workspace.dispatch('list',dict(liveId=show['id'],position=cover['position'],duplicates=False))
        self.assertEqual({item['id'] for item in result['items']},
            {'mb:cf9bc513-d166-43f4-b5fd-c29b677bc55b','spotify:2u88qtGamUHw1PKyMTVEZB'})
        self.assertTrue(all(item['artists']!=cover['artists'] for item in result['items']))
        for recording in result['items']:
            history=self.workspace.dispatch('detail',dict(id=recording['id']))['liveEvents']
            self.assertIn(show['id'],[event['id'] for event in history])


if __name__=='__main__':
    unittest.main()
