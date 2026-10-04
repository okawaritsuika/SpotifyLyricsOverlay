"""Isolated consumer flows; no real login, playback or existing user data."""
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from explorer_dialog import ExplorerDialog, readable_color
from storage import Store
from ui_theme import configure_theme


def song(identifier, kind='song'):
    return {'id': identifier, 'title': 'Song ' + identifier, 'artists': ['Artist'],
            'album': 'Album', 'series': ['shiny'], 'kind': kind,
            'source_url': 'https://example.test/music/' + identifier}


def track(identifier='a', playable=None):
    return {'id': identifier * 22, 'title': 'Song a', 'artists': ['Artist'], 'album': 'Album',
            'duration': 240, 'is_playable': playable,
            'checked_at': '2026-10-02T00:00:00+00:00'}


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class ExplorerDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.store.save_settings({'spotify_connected': True, 'opacity': .8})
        self.store.save_library([])
        (self.directory / 'timings').mkdir()
        (self.directory / 'timings' / 'existing.json').write_text(
            '{"times": [1.25, 4.5], "offset": 0.1}', encoding='utf-8')
        self.existing = {path: path.read_bytes() for path in self.directory.rglob('*.json')}
        self.root = tk.Tk();self.root.withdraw()
        configure_theme(self.root)
        self.songs = [song('a'), song('b'), song('instrumental', 'instrumental')]
        self.catalog = SimpleNamespace(songs=self.songs, sources=[{'name': 'MusicBrainz'}],
                                       generated_at='2026-10-02', reload=Mock())
        self.spotify = SimpleNamespace(connected=False, search=Mock(return_value=[]),
                                       current_track=Mock(return_value=None), open=Mock(),
                                       play=Mock(return_value=True), close=Mock())
        self.people = [{'id': 'group:group', 'name': 'Group', 'display': 'Group', 'kind': 'group',
                   'series': 'shiny', 'members': ['Idol'], 'aliases': [], 'source_urls': []},
                  {'id':'idol:one','name':'Idol','display':'아이돌','kind':'idol','series':'shiny',
                   'color':'#123456','aliases':[], 'voice_actors':[{'name':'声優','display':'성우 이름'}]}]
        self.live_events = [{'id':'concert-2024','title':'Live 2024','date':'2024-03-02','series':['shiny'],
                             'source_url':'https://example.test/live/2024','songs':[{'title':'Song a'}]},
                            {'id':'concert-2025','title':'Live 2025','date':'2025-05-03','series':['million'],
                             'source_url':'https://example.test/live/2025','songs':[{'title':'Another'}]}]
        def event_songs(event, songs):
            return [{'position':index, 'performance':item,
                     'matches':[recording for recording in songs if recording['title']==item['title']
                                and set(recording['series']).intersection(event['series'])]}
                    for index, item in enumerate(event['songs'], 1)]
        self.live_catalog = SimpleNamespace(events=self.live_events, for_song=Mock(return_value=[]),
                                            songs_for_event=Mock(side_effect=event_songs),reload=Mock(),
                                            generated_at='2026-10-02T18:30:00+09:00',verified_at='2026-10-02',
                                            collection_report={})
        original_toplevel = tk.Toplevel
        def invisible_toplevel(*args, **kwargs):
            window = original_toplevel(*args, **kwargs)
            window.attributes('-alpha', 0)
            return window
        self.patches = [patch('explorer_dialog.SongCatalog', return_value=self.catalog),
                        patch('explorer_dialog.DesktopSpotify', return_value=self.spotify),
                        patch('explorer_dialog.performers', return_value=self.people),
                        patch('explorer_dialog.LiveCatalog', return_value=self.live_catalog),
                        patch('explorer_dialog.refresh_people',return_value={'updated':False,'cancelled':False,
                                                                            'errors':[],'message':'Fixture data'}),
                        patch('explorer_dialog.work_area', return_value=(0, 0, 1920, 1080)),
                        patch('explorer_dialog.tk.Toplevel', side_effect=invisible_toplevel),
                        patch('explorer_dialog.messagebox.showerror')]
        for item in self.patches:item.start()
        self.app = SimpleNamespace(root=self.root, store=self.store, explorer_dialog=None,
                                   open_connection=Mock(), connect_spotify=Mock())
        self.dialog = ExplorerDialog(self.app)
        self.app.explorer_dialog = self.dialog
        self.root.update()
        self.dialog.choose_song(self.songs[0])

    def tearDown(self):
        self.dialog.close()
        self.root.destroy()
        for item in reversed(self.patches):item.stop()
        try:
            for path, contents in self.existing.items():
                self.assertEqual(path.read_bytes(), contents, f'Existing overlay data changed: {path.name}')
        finally:self.temp.cleanup()

    def capture_job(self, action):
        with patch.object(self.dialog, 'run_job') as launch:action()
        self.assertEqual(launch.call_count, 1)
        return launch.call_args.args[1:]

    def poll(self):
        self.dialog.window.after_cancel(self.dialog.poll_id)
        self.dialog.poll()

    def candidate_button(self, label):
        return next(widget for widget in descendants(self.dialog.candidate_window)
                    if isinstance(widget, tk.Button) and widget.cget('text') == label)

    def enter_url(self, identifier='a'):
        entry = next(widget for widget in descendants(self.dialog.candidate_window)
                     if widget.winfo_class() == 'TEntry')
        entry.insert(0, 'https://open.spotify.com/track/' + identifier * 22)

    def test_initialization_preserves_overlay_data_and_selects_dictionary_song(self):
        self.assertEqual(self.dialog.selected_song['id'], 'a')
        self.assertEqual(len(self.dialog.filtered), 3)
        self.assertEqual(len(self.dialog.notebook.tabs()), 5)
        self.assertEqual(self.dialog.play_button.cget('state'), 'normal')
        self.assertEqual(self.dialog.play_button.cget('text'), 'Spotify에서 찾아 재생')
        self.app.connect_spotify.assert_not_called()

    def test_dictionary_and_related_windows_do_not_pin_above_other_apps(self):
        self.root.attributes('-topmost', True)
        self.dialog.candidates_dialog(self.songs[0], [])
        self.dialog.local_help()
        self.root.update()
        help_window = next(window for window in self.dialog.window.winfo_children()
                           if window.winfo_class() == 'Toplevel'
                           and window.title() == 'Spotify에 로컬 음악 연결')
        for window in (self.dialog.window, self.dialog.candidate_window, help_window):
            with self.subTest(title=window.title()):
                self.assertFalse(window.attributes('-topmost'))
                self.assertFalse(window.transient())
                window.lift();self.root.update()
                self.assertFalse(window.attributes('-topmost'))
        self.assertTrue(self.root.attributes('-topmost'))

    def test_login_uses_existing_app_connection_once_per_explicit_click(self):
        self.dialog.login_button.invoke()
        self.app.connect_spotify.assert_called_once_with()
        self.app.open_connection.assert_not_called()
        self.assertIn('한 번 로그인', self.dialog.status.get())

    def test_connected_login_opens_existing_connection_settings(self):
        self.spotify.connected = True
        self.dialog.refresh_settings()
        self.assertEqual(self.dialog.login_button.cget('text'), '연결 상태 보기')
        self.dialog.login_button.invoke()
        self.app.open_connection.assert_called_once_with()
        self.app.connect_spotify.assert_not_called()

    def test_busy_operation_does_not_start_login(self):
        self.dialog.busy = True
        self.dialog.connect_spotify()
        self.app.connect_spotify.assert_not_called()
        self.assertIn('취소한 뒤', self.dialog.status.get())

    def test_settings_have_no_developer_account_setup(self):
        labels = [str(widget.cget('text')) for widget in descendants(self.dialog.settings_tab)
                  if 'text' in widget.keys()]
        for term in ('Client', 'Dashboard', 'OAuth', 'API', 'Premium', '개발자'):
            self.assertNotIn(term, ' '.join(labels))
        self.assertFalse(hasattr(self.dialog, 'client_id'))
        self.assertIn('MusicBrainz', self.dialog.source_status.get())

    def test_disconnected_search_uses_cache_without_starting_login(self):
        with patch('explorer_dialog.cached_candidates', return_value=[track()]) as cached:
            self.dialog.search_spotify()
        cached.assert_called_once_with(self.directory, self.songs[0])
        self.spotify.search.assert_not_called()
        self.app.connect_spotify.assert_not_called()
        self.candidate_button('Spotify 로그인').invoke()
        self.app.connect_spotify.assert_called_once_with()

    def test_unknown_link_remains_eligible_without_being_marked_playable(self):
        self.dialog.library.link_spotify('a', track())
        self.dialog.only_playable.set(True);self.dialog.filter_songs()
        self.assertEqual([item['id'] for item in self.dialog.filtered], ['a'])
        with patch.object(self.dialog, 'play_selected') as play:self.dialog.random_play()
        play.assert_called_once_with()
        self.assertIsNone(self.dialog.library.linked_track('a')['is_playable'])

    def test_unlinked_random_song_offers_connection_without_login(self):
        with patch.object(self.dialog, 'search_spotify') as search:self.dialog.random_play()
        search.assert_called_once_with(play_after=True)
        self.spotify.play.assert_not_called()
        self.app.connect_spotify.assert_not_called()

    def test_instrumental_spoken_and_explicit_unknown_are_excluded_from_discovery_and_random(self):
        self.dialog.filtered = [song('inst', 'instrumental'), song('talk', 'spoken'), song('bonus', 'unknown')]
        with patch.object(self.dialog, 'choose_song') as choose:
            self.dialog.discover();self.dialog.random_play()
        choose.assert_not_called()

    def test_legacy_missing_kind_remains_eligible_and_explicit_unknown_is_labelled(self):
        legacy=song('legacy');legacy.pop('kind')
        self.dialog.filtered=[legacy]
        with patch.object(self.dialog,'choose_song') as choose:self.dialog.discover()
        choose.assert_called_once_with(legacy)
        self.dialog.selected_song=song('bonus','unknown');self.dialog.show_song_detail()
        self.assertIn('분류 미확인',self.dialog.song_detail.get('1.0','end'))

    def test_random_prefers_eligible_link_over_known_restricted_recording(self):
        self.dialog.library.link_spotify('a', track(playable=False))
        self.dialog.library.link_spotify('b', track('b'))
        with patch.object(self.dialog, 'play_selected') as play:self.dialog.random_play()
        self.assertEqual(self.dialog.selected_song['id'], 'b')
        play.assert_called_once_with()

    def test_search_result_stays_bound_to_original_song_after_selection_changes(self):
        self.spotify.connected = True
        work, done = self.capture_job(self.dialog.search_spotify)
        self.dialog.selected_song = self.songs[1]
        with patch.object(self.dialog, 'candidates_dialog') as display:done(work())
        self.spotify.search.assert_called_once_with('Song a', 'Artist', 'Album', cancel=self.dialog.cancel)
        self.assertEqual(display.call_args.args[0]['id'], 'a')

    def test_cancelled_play_never_opens_spotify(self):
        self.dialog.library.link_spotify('a', track())
        work, _ = self.capture_job(self.dialog.play_selected)
        self.dialog.cancel.set()
        self.assertFalse(work())
        self.spotify.play.assert_not_called()

    def test_confirmed_play_marks_only_selected_recording_playable(self):
        self.dialog.library.link_spotify('a', track())
        requested = self.dialog.library.linked_track('a').copy()
        work, done = self.capture_job(self.dialog.play_selected)
        done(work())
        self.spotify.play.assert_called_once_with(requested, cancel=self.dialog.cancel)
        self.assertIs(self.dialog.library.linked_track('a')['is_playable'], True)
        self.assertIn('재생 중', self.dialog.status.get())

    def test_unconfirmed_play_preserves_unknown_and_explains_next_step(self):
        self.spotify.play.return_value = False
        self.dialog.library.link_spotify('a', track())
        work, done = self.capture_job(self.dialog.play_selected)
        done(work())
        self.assertIsNone(self.dialog.library.linked_track('a')['is_playable'])
        self.assertIn('재생을 확인하지 못했습니다', self.dialog.status.get())
        self.assertNotIn('재생 중', self.dialog.status.get())

    def test_late_play_status_does_not_replace_new_explicit_recording_link(self):
        self.dialog.library.link_spotify('a', track())
        work, done = self.capture_job(self.dialog.play_selected)
        result = work()
        self.dialog.library.link_spotify('a', track('b'))
        done(result)
        self.assertEqual(self.dialog.library.linked_track('a')['id'], 'b' * 22)
        self.assertIsNone(self.dialog.library.linked_track('a')['is_playable'])

    def test_manual_url_preserves_unknown_without_inventing_song_metadata(self):
        self.dialog.candidates_dialog(self.songs[0], [])
        self.enter_url()
        with patch('explorer_dialog.messagebox.askyesno', return_value=True):
            self.candidate_button('선택한 곡 연결').invoke()
        saved = self.dialog.library.linked_track('a')
        self.assertEqual(saved['id'], 'a' * 22)
        self.assertIsNone(saved['is_playable'])
        self.assertEqual(saved['artists'], [])
        self.assertNotEqual(saved['title'], self.songs[0]['title'])
        self.spotify.search.assert_not_called()

    def test_manual_url_uses_observed_metadata_only_for_same_id(self):
        self.spotify.current_track.return_value = track('b', playable=True)
        self.dialog.candidates_dialog(self.songs[0], [])
        self.enter_url('a')
        with patch('explorer_dialog.messagebox.askyesno', return_value=True):
            self.candidate_button('선택한 곡 연결').invoke()
        self.assertIsNone(self.dialog.library.linked_track('a')['is_playable'])
        self.assertEqual(self.dialog.library.linked_track('a')['artists'], [])
        self.dialog.candidates_dialog(self.songs[0], [])
        self.enter_url('b')
        with patch('explorer_dialog.messagebox.askyesno', return_value=True):
            self.candidate_button('선택한 곡 연결').invoke()
        self.assertEqual(self.dialog.library.linked_track('a')['artists'], ['Artist'])
        self.assertIs(self.dialog.library.linked_track('a')['is_playable'], True)

    def test_current_track_binding_stays_with_original_song(self):
        self.spotify.current_track.return_value = track('b')
        self.dialog.candidates_dialog(self.songs[0], [])
        self.dialog.selected_song = self.songs[1]
        with patch('explorer_dialog.messagebox.askyesno', return_value=True):
            self.candidate_button('지금 재생 중인 곡 연결').invoke()
        self.assertEqual(self.dialog.library.linked_track('a')['id'], 'b' * 22)
        self.assertIsNone(self.dialog.library.linked_track('b'))

    def test_candidate_external_search_keeps_reserved_characters_and_original_song(self):
        from urllib.parse import unquote, urlsplit
        original = {**self.songs[0], 'title': '1/3 空?#%'}
        self.dialog.candidates_dialog(original, [])
        self.dialog.selected_song = self.songs[1]
        with patch('explorer_dialog.open_url') as opened:
            self.candidate_button('Spotify 검색 열기').invoke()
        url = urlsplit(opened.call_args.args[0])
        self.assertEqual(url.netloc, 'open.spotify.com')
        self.assertEqual(url.query, '')
        self.assertEqual(url.fragment, '')
        self.assertEqual(len(url.path.split('/')), 3)
        self.assertEqual(unquote(url.path.split('/')[-1]), original['title'])

    def test_missing_current_track_leaves_mapping_unmodified(self):
        self.dialog.candidates_dialog(self.songs[0], [])
        self.candidate_button('지금 재생 중인 곡 연결').invoke()
        self.assertIsNone(self.dialog.library.linked_track('a'))
        self.assertIn('곡 링크를 직접', self.dialog.status.get())

    def test_closed_candidate_window_never_confirms_or_saves(self):
        self.dialog.candidates_dialog(self.songs[0], [])
        window = self.dialog.candidate_window
        window.destroy()
        save = Mock()
        with patch('explorer_dialog.messagebox.askyesno') as confirm:
            self.dialog.confirm_track(window, self.songs[0], track(), save)
        confirm.assert_not_called();save.assert_not_called()

    def test_window_closed_during_confirmation_never_saves(self):
        self.dialog.candidates_dialog(self.songs[0], [])
        window = self.dialog.candidate_window
        save = Mock()
        def confirm(*_, **__):
            window.destroy()
            return True
        with patch('explorer_dialog.messagebox.askyesno', side_effect=confirm):
            self.dialog.confirm_track(window, self.songs[0], track(), save)
        save.assert_not_called()

    def test_cancelled_worker_does_not_open_its_result_dialog(self):
        done = Mock()
        self.dialog.live_match_cache['old-catalog'] = []
        with patch('explorer_dialog.threading.Thread') as thread:
            self.dialog.run_job('Searching', lambda: [track()], done)
            run = thread.call_args.kwargs['target']
        self.dialog.cancel_job();run();self.poll()
        done.assert_not_called()
        self.assertFalse(self.dialog.busy)
        self.assertEqual(self.dialog.live_match_cache,{})

    def test_play_cancelled_after_request_does_not_save_late_confirmation(self):
        self.dialog.library.link_spotify('a', track())
        def play(*_, **__):
            self.dialog.cancel.set()
            return True
        self.spotify.play.side_effect = play
        with patch('explorer_dialog.threading.Thread') as thread:
            self.dialog.play_selected()
            thread.call_args.kwargs['target']()
        self.poll()
        self.assertIsNone(self.dialog.library.linked_track('a')['is_playable'])
        self.assertEqual(self.dialog.status.get(), '작업을 취소했습니다.')

    def test_catalog_partial_failure_keeps_explicit_source_notice(self):
        with patch('explorer_dialog.refresh_catalog', return_value={'updated': True, 'errors': ['source']}) as refresh:
            work, done = self.capture_job(self.dialog.update_catalog)
            done(work())
        self.assertEqual(refresh.call_args.args[0], self.directory / 'song_catalog.json')
        self.assertIs(refresh.call_args.kwargs['cancel'], self.dialog.cancel)
        self.catalog.reload.assert_called_once_with()
        self.assertIn('일부 출처 실패', self.dialog.status.get())
        self.assertIn('이전 내용 유지', self.dialog.status.get())
        from group_credits import group_member_lines
        roster = group_member_lines(('Group',),'THE IDOLM@STER',self.directory/'group_credits.json')
        self.assertIn('아이돌 (CV. 성우 이름)',roster[0])

    def test_music_failure_still_updates_people_and_reports_both_results(self):
        with (patch('explorer_dialog.refresh_catalog',side_effect=OSError('Music unavailable')) as music,
              patch('explorer_dialog.refresh_people',return_value={'updated':True,'characters':347,'errors':[]}) as people):
            work,done=self.capture_job(self.dialog.update_catalog)
            done(work())
        music.assert_called_once();people.assert_called_once()
        self.assertEqual(people.call_args.args[0],self.directory/'imas_db_people.json')
        self.assertIn('음악 자료 갱신 실패',self.dialog.status.get())
        self.assertIn('인물 자료 갱신 · 캐릭터 347명',self.dialog.status.get())
        self.assertFalse(self.dialog.music_update_active)

    def test_people_failure_does_not_discard_successful_music_update(self):
        with (patch('explorer_dialog.refresh_catalog',return_value={'updated':True,'errors':[]}),
              patch('explorer_dialog.refresh_people',side_effect=ValueError('Incomplete table'))):
            work,done=self.capture_job(self.dialog.update_catalog)
            done(work())
        self.assertIn('음악 자료를 갱신했습니다',self.dialog.status.get())
        self.assertIn('인물 자료 갱신 실패',self.dialog.status.get())
        self.assertIn('Incomplete table',self.dialog.status.get())

    def test_cancel_during_music_update_never_starts_people_collection(self):
        def cancel_music(*_,**__):
            self.dialog.cancel.set()
            return {'updated':False,'cancelled':True,'errors':[]}
        with (patch('explorer_dialog.refresh_catalog',side_effect=cancel_music),
              patch('explorer_dialog.refresh_people') as people):
            work,_=self.capture_job(self.dialog.update_catalog)
            stats=work()
        people.assert_not_called()
        self.assertTrue(stats['cancelled'])

    def test_roster_save_failure_preserves_successful_catalog_status(self):
        with patch('explorer_dialog.save_group_credits',side_effect=OSError('read only')):
            work, done = self.capture_job(self.dialog.update_catalog)
            done({'updated':True})
        self.assertIn('음악 자료를 갱신했습니다',self.dialog.status.get())
        self.assertIn('가사창 멤버 자료 저장 실패',self.dialog.status.get())

    def test_closing_during_catalog_update_cancels_and_ignores_completion(self):
        with patch('explorer_dialog.refresh_catalog', return_value={'cancelled': True}) as refresh:
            with patch('explorer_dialog.threading.Thread') as thread:
                self.dialog.update_catalog()
                run = thread.call_args.kwargs['target']
            self.dialog.close();run();self.dialog.poll()
        self.assertTrue(refresh.call_args.kwargs['cancel'].is_set())
        self.catalog.reload.assert_not_called()
        self.assertIsNone(self.app.explorer_dialog)

    def test_busy_worker_cannot_launch_a_second_request(self):
        with patch('explorer_dialog.threading.Thread') as thread:
            self.dialog.run_job('First', Mock(), Mock())
            self.dialog.run_job('Second', Mock(), Mock())
        self.assertEqual(thread.call_count, 1)

    def test_immediate_search_consumes_pending_debounce_and_close_stops_spotify(self):
        self.dialog.query.set('Song')
        pending = self.dialog.search_after
        self.assertIsNotNone(pending)
        self.dialog.filter_songs()
        self.assertIsNone(self.dialog.search_after)
        self.assertNotIn(pending,self.dialog.window.tk.call('after','info'))
        self.dialog.close()
        self.spotify.close.assert_called_once_with()

    def test_vocal_songs_lead_list_without_discarding_other_recordings(self):
        self.catalog.songs = [song('talk', 'spoken'), song('inst', 'instrumental'), song('vocal')]
        self.dialog.filter_songs()
        self.assertEqual([item['id'] for item in self.dialog.filtered], ['vocal', 'inst', 'talk'])

    def test_detail_uses_catalog_first_release_date(self):
        self.dialog.selected_song = {**self.songs[0], 'first_release_date': '2024-09-18'}
        self.dialog.show_song_detail()
        self.assertIn('발매  2024-09-18', self.dialog.song_detail.get('1.0', 'end'))

    def test_character_and_voice_actor_share_color_in_list_and_detail(self):
        self.dialog.person_kind.set('아이돌');self.dialog.filter_people();self.dialog.select_person()
        self.assertEqual(self.dialog.person_tree.set('idol:one','cv'), '성우 이름')
        self.root.update()
        canvas = self.dialog.person_tree.canvas
        # Both visible lines retain this character's color, independently of selection.
        for kind in ('performer_name', 'performer_voice'):
            items = canvas.find_withtag(kind)
            self.assertTrue(items)
            self.assertEqual(canvas.itemcget(items[0], 'fill'), readable_color('#123456'))
        tag = 'character_'+readable_color('#123456')[1:]
        self.assertIn('성우 이름',self.dialog.person_detail.get('1.0','end'))
        self.assertEqual(len(self.dialog.person_detail.tag_ranges(tag)),4)

    def test_voice_actor_search_matches_original_and_korean_names(self):
        self.dialog.person_kind.set('아이돌')
        for name in ('声優','성우 이름'):
            self.dialog.person_query.set(name);self.dialog.filter_people()
            self.assertEqual(self.dialog.person_tree.get_children(),('idol:one',))

    def test_unit_members_include_their_colors_and_voice_actors(self):
        self.dialog.person_kind.set('유닛');self.dialog.filter_people();self.dialog.select_person()
        text = self.dialog.person_detail.get('1.0','end')
        self.assertIn('아이돌  · CV: 성우 이름',text)
        self.assertTrue(any(self.dialog.person_detail.tag_ranges(tag)
            for tag in self.dialog.person_detail.tag_names() if tag.startswith('character_')))

    def test_group_only_song_automatically_shows_member_and_voice_without_changing_credits(self):
        group_song = {**song('group-song'), 'artists':['Group']}
        self.dialog.selected_song = group_song;self.dialog.show_song_detail()
        text = self.dialog.song_detail.get('1.0','end')
        self.assertIn('Group · 그룹 멤버',text)
        self.assertIn('아이돌  · CV: 성우 이름',text)
        self.assertIn('개별 가창 크레딧',text)
        self.assertEqual(group_song['artists'],['Group'])

    def test_hatsuboshi_song_preview_keeps_project_and_only_explicit_characters(self):
        from explorer_library import performers as catalog_performers
        from song_catalog import SongCatalog as installed_catalog
        gakuen = [person for person in catalog_performers(external=installed_catalog().performers)
                  if person.get('series') == 'gakuen']
        self.people += gakuen;self.dialog.index_people()
        project = next(person for person in gakuen if person['name'] == '初星学園')
        for artists, expected, cv_count in [(['初星学園'], ['하츠보시 학원'], 0),
                (['初星学園', '篠澤 広'], ['하츠보시 학원', '시노사와 히로'], 1)]:
            with self.subTest(artists=artists):
                original = {**song('school-credit'), 'title':'コントラスト', 'artists':artists[:], 'series':['gakuen']}
                before = {**original, 'artists':original['artists'][:]}
                self.catalog.songs=[original];self.dialog.filter_songs();self.root.update()
                preview, roster = self.dialog.song_preview(original)
                self.assertEqual([person['name'] for person in preview], expected)
                self.assertFalse(roster)
                self.assertFalse(any(person['roster'] for person in preview))
                self.assertEqual(sum(bool(person['voice']) for person in preview), cv_count)
                text = self.dialog.song_detail.get('1.0','end')
                self.assertIn('프로젝트 크레딧', text)
                self.assertNotIn('그룹 멤버', text)
                for member in self.dialog.group_members(project):
                    if member['display'] not in expected:self.assertNotIn(member['display'], text)
                self.assertEqual(len(self.dialog.song_tree.canvas.find_withtag('performer_name')),len(expected))
                self.assertEqual(len(self.dialog.song_tree.canvas.find_withtag('performer_voice')),cv_count)
                self.assertEqual(original, before)
        self.assertEqual(len(self.dialog.group_members(project)),13)
        self.dialog.person_query.set('하츠보시 학원');self.dialog.filter_people();self.dialog.select_person()
        self.assertIn('소속 아이돌',self.dialog.person_tree.item(project['id'],'subtitle'))
        self.assertEqual(self.dialog.person_tree.item(project['id'],'badge'),'프로젝트')
        text = self.dialog.person_detail.get('1.0','end')
        self.assertIn('소속 아이돌',text)
        self.assertIn('프로젝트 크레딧이 있는 곡',text)
        for member in self.dialog.group_members(project):self.assertIn(member['display'],text)

    def test_bare_character_in_song_detail_also_gets_confirmed_voice(self):
        character_song={**song('hiro'),'artists':['篠澤 広']}
        self.catalog.songs=[character_song];self.dialog.filter_songs()
        self.dialog.selected_song=character_song;self.dialog.show_song_detail()
        self.assertIn('시노사와 히로 (CV. 카와무라 레이나)',self.dialog.song_detail.get('1.0','end'))
        self.assertIn('시노사와 히로 (CV. 카와무라 레이나)',self.dialog.song_tree.set('hiro','artists'))
        self.assertEqual(character_song['artists'],['篠澤 広'])

    def test_multi_member_song_list_shows_independent_character_voice_colors(self):
        self.people.append({'id':'idol:two', 'name':'Another idol', 'display':'다른 아이돌',
            'kind':'idol', 'series':'shiny', 'color':'#ed4123', 'aliases':[],
            'voice_actors':[{'name':'Other actor', 'display':'다른 성우'}]})
        self.people[0]['members'].append('Another idol');self.dialog.index_people()
        group_song = {**song('group-preview'), 'artists':['Group']}
        self.catalog.songs = [group_song];self.dialog.filter_songs();self.root.update()
        canvas = self.dialog.song_tree.canvas
        names = canvas.find_withtag('performer_name');voices = canvas.find_withtag('performer_voice')
        self.assertEqual([canvas.itemcget(item,'text') for item in names], ['아이돌','다른 아이돌'])
        self.assertEqual([canvas.itemcget(item,'text') for item in voices], ['CV. 성우 이름','CV. 다른 성우'])
        for name, voice, original in zip(names, voices, ('#123456','#ed4123')):
            self.assertEqual(canvas.itemcget(name,'fill'),readable_color(original))
            self.assertEqual(canvas.itemcget(voice,'fill'),readable_color(original))
        rectangles = [canvas.itemcget(item,'fill') for item in canvas.find_all() if canvas.type(item)=='rectangle']
        self.assertNotIn('#123456',rectangles);self.assertNotIn('#ed4123',rectangles)
        self.assertIn('유닛 멤버 미리보기',self.dialog.song_tree.item('group-preview','subtitle'))
        self.assertEqual(group_song['artists'],['Group'])

    def test_explicit_member_credit_does_not_expand_list_to_every_group_member(self):
        self.people.append({'id':'idol:two','name':'Second','display':'다른 멤버',
                            'kind':'idol','series':'shiny','aliases':[],'voice_actors':[]})
        self.people[0]['members'].append('Second');self.dialog.index_people()
        original = {**song('subset'), 'artists':['Group','Idol']}
        preview, _ = self.dialog.song_preview(original)
        self.assertEqual([person['name'] for person in preview], ['아이돌'])
        self.assertEqual(original['artists'],['Group','Idol'])
        self.dialog.selected_song=original;self.dialog.show_song_detail()
        self.assertNotIn('다른 멤버',self.dialog.song_detail.get('1.0','end'))

    def test_explicit_recording_cv_is_visible_without_replacing_character_metadata(self):
        original = {**song('explicit-cv'),'artists':['Idol (CV. Specific actor)']}
        preview, _ = self.dialog.song_preview(original)
        self.assertEqual(preview[0]['voice'],'Specific actor')
        self.assertEqual(self.people[1]['voice_actors'][0]['display'],'성우 이름')
        self.assertEqual(original['artists'],['Idol (CV. Specific actor)'])

    def test_actual_character_and_separate_cv_credit_render_once_without_changing_search_identity(self):
        from artist_names import artist_label
        from explorer_library import artist_key, performers as catalog_performers
        from song_catalog import SongCatalog as installed_catalog
        originals = [('hello', '"HELLO!!" (M@STER VERSION)', '水谷絵理', '花澤香菜'),
                     ('wonderful', 'W"onderful Tactics!', '蒼井享介', '山谷祥生')]
        names = {artist_key(item[2]) for item in originals}
        self.people += [person for person in catalog_performers(external=installed_catalog().performers) if person['kind']=='idol'
                        and artist_key(person['name']) in names]
        self.dialog.index_people()
        for identifier, title, character, actor in originals:
            with self.subTest(title=title):
                original = {**song(identifier), 'title':title, 'artists':[character,actor]}
                self.dialog.query.set('')
                self.catalog.songs=[original];self.dialog.filter_songs();self.root.update()
                self.assertEqual(self.dialog.display_song_credits(original),(character,))
                preview,_=self.dialog.song_preview(original)
                self.assertEqual(len(preview),1)
                translated_actor=artist_label([actor],'THE IDOLM@STER')
                self.assertEqual(self.dialog.song_detail.get('1.0','end').count(translated_actor),1)
                self.assertEqual(self.dialog.song_tree.set(identifier,'artists').count(translated_actor),1)
                self.assertEqual(len(self.dialog.song_tree.canvas.find_withtag('performer_name')),1)
                self.assertEqual(len(self.dialog.song_tree.canvas.find_withtag('performer_voice')),1)
                self.dialog.query.set(actor);self.dialog.filter_songs()
                self.assertEqual([item['id'] for item in self.dialog.filtered],[identifier])
                self.spotify.connected=True
                work,_=self.capture_job(self.dialog.search_spotify);work()
                self.spotify.search.assert_called_with(title,character,'Album',cancel=self.dialog.cancel)
                self.assertEqual(original['artists'],[character,actor])

    def test_cv_fold_uses_whole_known_names_and_keeps_general_credit_names_without_cv_label(self):
        original={**song('composer'),'artists':['Idol','声優','真島ゆろ','声優 Studio']}
        self.catalog.songs=[original];self.dialog.filter_songs();self.root.update()
        self.assertEqual(self.dialog.display_song_credits(original),('Idol','真島ゆろ','声優 Studio'))
        canvas=self.dialog.song_tree.canvas
        rendered=[canvas.itemcget(item,'text') for item in canvas.find_withtag('performer_name')]
        self.assertEqual(rendered,['아이돌','真島ゆろ','声優 Studio'])
        voices=[canvas.itemcget(item,'text') for item in canvas.find_withtag('performer_voice')]
        self.assertEqual(voices,['CV. 성우 이름'])
        self.assertIn('真島ゆろ',self.dialog.song_detail.get('1.0','end'))
        self.assertEqual(original['artists'],['Idol','声優','真島ゆろ','声優 Studio'])

    def test_group_roster_or_conflicting_explicit_cv_does_not_hide_an_independent_person(self):
        for credits in (['Group','声優'],['Idol (CV. Different actor)','声優'],['Other singer','声優']):
            with self.subTest(credits=credits):
                original={**song('unconfirmed-cv'),'artists':credits}
                self.assertEqual(self.dialog.display_song_credits(original),tuple(credits))
                self.assertEqual(original['artists'],credits)

    def test_multiple_known_voice_actors_require_an_explicit_cv_before_folding(self):
        self.people[1]['voice_actors'].append({'name':'Another actor','display':'다른 성우'})
        self.dialog.index_people()
        bare={**song('multi-cv'),'artists':['Idol','声優','Another actor']}
        self.assertEqual(self.dialog.display_song_credits(bare),tuple(bare['artists']))
        explicit={**song('selected-cv'),'artists':['Idol (CV. 声優)','声優','Another actor']}
        self.assertEqual(self.dialog.display_song_credits(explicit),('Idol (CV. 声優)','Another actor'))
        self.assertEqual(explicit['artists'],['Idol (CV. 声優)','声優','Another actor'])

    def test_composite_credit_cv_fold_preserves_other_names_and_same_named_character(self):
        original={**song('composite'),'artists':['Idol, 声優, Composer']}
        self.assertEqual(self.dialog.display_song_credits(original),('Idol, Composer',))
        self.assertEqual(original['artists'],['Idol, 声優, Composer'])
        self.people.append({'id':'idol:shared-actor','name':'声優','display':'동명 캐릭터',
                            'kind':'idol','series':'shiny','aliases':[],'voice_actors':[]})
        self.dialog.index_people()
        distinct={**song('distinct'),'artists':['Idol','声優']}
        self.assertEqual(self.dialog.display_song_credits(distinct),('Idol','声優'))

    def test_unlinked_play_connects_then_plays_original_requested_song(self):
        self.dialog.play_selected()
        self.assertIsNotNone(self.dialog.candidate_window)
        self.enter_url()
        self.dialog.selected_song = self.songs[1]
        with patch.object(self.dialog,'play_selected') as play, patch('explorer_dialog.messagebox.askyesno',return_value=True):
            self.candidate_button('연결하고 재생').invoke()
        play.assert_called_once_with()
        self.assertEqual(self.dialog.selected_song['id'],'a')
        self.assertEqual(self.dialog.library.linked_track('a')['id'],'a'*22)
        self.assertIsNone(self.dialog.library.linked_track('b'))

    def test_group_search_includes_character_and_voice_actor_names(self):
        self.dialog.person_kind.set('유닛')
        for name in ('아이돌', '성우 이름', '声優'):
            self.dialog.person_query.set(name);self.dialog.filter_people()
            self.assertEqual(self.dialog.person_tree.get_children(),('group:group',))

    def test_ambiguous_group_and_character_name_still_resolves_character_in_roster(self):
        self.people.append({'id':'group:shadow','name':'Idol','display':'다른 그룹',
                            'kind':'group','series':'shiny','members':[],'aliases':[]})
        self.dialog.index_people()
        self.dialog.person_kind.set('유닛');self.dialog.person_query.set('Group')
        self.dialog.filter_people();self.dialog.person_tree.selection_set('group:group');self.dialog.select_person()
        self.assertIn('아이돌  · CV: 성우 이름',self.dialog.person_detail.get('1.0','end'))

    def test_unknown_voice_remains_explicitly_unconfirmed(self):
        self.people[1]['voice_actors']=[];self.dialog.index_people()
        self.dialog.person_kind.set('유닛');self.dialog.filter_people();self.dialog.select_person()
        self.assertIn('아이돌  · CV: 미확인',self.dialog.person_detail.get('1.0','end'))

    def test_duplicate_filter_keeps_preferred_link_and_restores_every_recording(self):
        first = {**song('first'),'title':'Same','duration':180,'album':'A'}
        second = {**song('second'),'title':'Same','duration':181,'album':'B'}
        live = {**song('live'),'title':'Same (Live)','duration':210}
        self.catalog.songs = [first,second,live]
        self.dialog.library.link_spotify('second',track())
        self.dialog.hide_duplicates.set(True);self.dialog.toggle_duplicates()
        self.assertEqual([item['id'] for item in self.dialog.filtered],['second','live'])
        self.assertEqual(self.dialog.hidden_duplicate_count,1)
        self.dialog.choose_song(second)
        self.assertIn('같은 제목 · 2개 녹음',self.dialog.song_detail.get('1.0','end'))
        self.dialog.hide_duplicates.set(False);self.dialog.toggle_duplicates()
        self.assertEqual(len(self.dialog.filtered),3)
        self.assertIsNone(self.dialog.library.linked_track('first'))
        self.assertEqual(self.dialog.library.linked_track('second')['id'],track()['id'])

    def test_duplicate_filter_applies_to_random_discovery(self):
        self.catalog.songs = [{**song('a'),'duration':180},
                              {**song('a2'),'title':'Song a','duration':181}, song('b')]
        self.dialog.hide_duplicates.set(True);self.dialog.toggle_duplicates()
        with patch.object(self.dialog.library,'choose',return_value=self.dialog.filtered[0]) as choose:
            self.dialog.discover()
        self.assertEqual(len(choose.call_args.args[0]),2)

    def test_automatic_search_cooldown_reports_unresolved_titles_without_claiming_completion(self):
        import time
        from catalog_link_queue import CatalogLinkQueue
        self.dialog.auto_links=CatalogLinkQueue(self.directory,self.songs,self.dialog.library,self.people)
        self.dialog.auto_links.checked={key:time.time() for key in self.dialog.auto_links.groups}
        self.spotify.connected=True
        self.dialog.window.after_cancel(self.dialog.auto_id)
        self.dialog.auto_link_tick()
        status=self.dialog.auto_status.get()
        self.assertIn('시도 후 미확정 3곡',status)
        self.assertIn('현재 자동검색 대기',status)
        self.assertNotIn('자동 확인 완료',status)
        self.spotify.search.assert_not_called()

    def test_duplicate_filter_preserves_selection_when_representative_changes(self):
        first = {**song('first'),'title':'Same','duration':180}
        second = {**song('second'),'title':'Same','duration':181}
        self.catalog.songs = [song('unrelated'),first,second]
        self.dialog.filter_songs();self.dialog.choose_song(second)
        self.dialog.library.toggle_favorite('first')
        self.dialog.hide_duplicates.set(True);self.dialog.toggle_duplicates()
        self.assertEqual(self.dialog.selected_song['id'],'first')
        self.assertEqual(self.dialog.song_tree.selection(),('first',))
        self.assertIn('같은 제목 · 2개 녹음',self.dialog.song_detail.get('1.0','end'))

    def test_title_group_variant_link_selects_other_artist_and_plays_its_own_track(self):
        first = {**song('first'), 'title':'Same', 'artists':['First singer'], 'album':'First album', 'duration':180}
        second = {**song('second'), 'title':'Same', 'artists':['Other singer'], 'album':'Other album', 'duration':300}
        self.catalog.songs = [first, second]
        self.dialog.library.link_spotify('first', track('a'))
        self.dialog.library.link_spotify('second', track('b'))
        self.dialog.library.toggle_favorite('first')
        before = self.dialog.library.path.read_bytes()
        self.dialog.hide_duplicates.set(True);self.dialog.filter_songs()
        self.assertEqual(len(self.dialog.filtered), 1)
        self.assertEqual(self.dialog.count_label.cget('text'), '제목 1  ·  녹음 2  ·  유닛·아이돌 2')
        text = self.dialog.song_detail
        position = text.search('Other singer · Other album · 5:00', '1.0')
        self.assertTrue(position)
        callback = next(text.navigation[tag] for tag in text.tag_names(position) if tag in text.navigation)
        callback();self.root.update()
        self.assertTrue(self.dialog.hide_duplicates.get())
        self.assertEqual([item['id'] for item in self.dialog.filtered], ['second'])
        self.assertEqual(self.dialog.song_tree.selection(), ('second',))
        self.assertEqual(self.dialog.selected_song['artists'], ['Other singer'])
        self.assertIn('같은 제목 · 2개 녹음', text.get('1.0', 'end'))
        self.assertEqual(self.dialog.library.path.read_bytes(), before)
        work, done = self.capture_job(self.dialog.play_selected)
        done(work())
        self.assertEqual(self.spotify.play.call_args.args[0]['id'], track('b')['id'])
        self.assertEqual(self.dialog.selected_song['id'], 'second')
        self.dialog.hide_duplicates.set(False);self.dialog.filter_songs()
        self.assertEqual([item['id'] for item in self.dialog.filtered], ['first', 'second'])

    def test_unlink_recomputes_duplicates_and_only_linked_random_pool(self):
        first = {**song('first'),'title':'Same','duration':180}
        second = {**song('second'),'title':'Same','duration':181}
        self.catalog.songs = [first,second]
        for item in (first,second):self.dialog.library.link_spotify(item['id'],track())
        self.dialog.hide_duplicates.set(True);self.dialog.only_playable.set(True)
        self.dialog.filter_songs();self.dialog.choose_song(first)
        self.dialog.candidates_dialog(first,[])
        self.dialog.remove_spotify_link(first,self.dialog.candidate_window)
        self.assertEqual([item['id'] for item in self.dialog.filtered],['second'])
        self.assertEqual(self.dialog.selected_song['id'],'second')
        with patch.object(self.dialog,'play_selected') as played, patch.object(self.dialog,'search_spotify') as searched:
            self.dialog.random_play()
        played.assert_called_once();searched.assert_not_called()

    def test_live_year_and_song_search_select_correct_event(self):
        self.dialog.live_year.set('2024');self.dialog.filter_lives()
        self.assertEqual(self.dialog.live_tree.get_children(),('concert-2024',))
        self.dialog.live_year.set('전체 연도');self.dialog.live_query.set('Another');self.dialog.filter_lives()
        self.assertEqual(self.dialog.live_tree.get_children(),('concert-2025',))
        with patch('explorer_dialog.open_url') as opened:self.dialog.open_live_source()
        opened.assert_called_once_with('https://example.test/live/2025')

    def test_live_scope_reports_dates_counts_update_and_collection_failures(self):
        self.live_catalog.collection_report={'index_urls':['one','two','three','summary'],
                                             'successful_urls':['one','two'],'failures':['three'],
                                             'non_event_pages':1,'skipped_urls':['summary']}
        self.dialog.index_lives();self.dialog.refresh_settings()
        for text in (self.dialog.live_scope.get(),self.dialog.live_update_status.get()):
            self.assertIn('2024-03-02 ~ 2025-05-03',text)
            self.assertIn('공연 2회 · 곡목 2개',text)
            self.assertIn('2026-10-02T18:30:00+09:00',text)
            self.assertIn('수집 대상 4개 주소 · 수집 성공 2개 · 실패 1개',text)
            self.assertIn('곡 집계 문서 1개 제외',text)
            self.assertNotIn('공식 확인 공연',text)

    def test_live_fan_source_and_independent_date_setlist_links_are_explicit(self):
        event={**self.live_events[0],'source_kind':'fan_database',
               'setlist_coverage':'official_setlist',
               'source_name':'アイマスDB (비공식 팬 데이터베이스)','series_names':['합동 공연'],
               'venue':'Example Hall','retrieved_at':'2026-10-02',
               'date_source_url':'https://example.test/date/2024',
               'setlist_source_url':'https://example.test/setlist/2024'}
        self.live_catalog.events=[event]
        self.dialog.live_query.set('팬 Example Hall');self.dialog.filter_lives()
        self.assertEqual(self.dialog.live_tree.get_children(),(event['id'],))
        self.assertEqual(self.dialog.live_tree.item(event['id'],'values')[2],'팬 기록')
        text=self.dialog.live_detail.get('1.0','end')
        self.assertIn('비공식 팬 데이터베이스',text)
        self.assertIn('합동 공연',text)
        self.assertIn('자료 수집·갱신일  2026-10-02',text)
        self.assertNotIn('공식 자료가 확인된',text)
        with patch('explorer_dialog.open_url') as opened:
            self.dialog.live_source_button.invoke()
            self.dialog.live_date_source_button.invoke()
            self.dialog.live_setlist_source_button.invoke()
        self.assertEqual([call.args[0] for call in opened.call_args_list],
                         [event['source_url'],event['date_source_url'],event['setlist_source_url']])

    def test_live_dance_performance_is_labeled_without_removing_candidate_navigation(self):
        event={**self.live_events[0],
               'songs':[{'title':'Song a','performance_kind':'dance_without_vocals','note':'Dance segment',
                         'artists':['Concert Dancer']}],
               'setlist_notes':['출처의 추가 공연 메모']}
        self.live_catalog.events=[event];self.dialog.show_live(event)
        text=self.dialog.live_detail.get('1.0','end')
        self.assertIn('댄스 · 가창 없음',text)
        self.assertIn('공연 출연 · Concert Dancer',text)
        self.assertNotIn('공연 가창',text)
        self.assertIn('Dance segment',text)
        self.assertIn('출처의 추가 공연 메모',text)
        self.assertIn('음원 후보 1개',text)
        list(self.dialog.live_detail.navigation.values())[0]()
        self.assertEqual([item['id'] for item in self.dialog.filtered],['a'])

    def test_live_credits_use_source_performers_without_replacing_recording_singers(self):
        import copy
        event={**self.live_events[0],'songs':[{'title':'Song a','artists':['中村繪里子','Unconfirmed Name']}],
               'source_kind':'fan_database'}
        before=copy.deepcopy((event,self.songs))
        self.live_catalog.events=[event];self.dialog.show_live(event)
        text=self.dialog.live_detail.get('1.0','end')
        self.assertIn('공연 가창 · 나카무라 에리코, Unconfirmed Name',text)
        self.assertNotIn('공연 가창 · Artist',text)
        self.assertIn('팬 기록',text)
        self.assertEqual((event,self.songs),before)
        self.assertIn('음원 후보 1개',text)

    def test_live_missing_credits_are_not_filled_from_recording_candidates(self):
        self.dialog.show_live(self.live_events[0])
        text=self.dialog.live_detail.get('1.0','end')
        self.assertIn('음원 후보 1개',text)
        self.assertNotIn('공연 가창',text)
        self.assertNotIn('공연 출연',text)

    def test_korean_song_search_preserves_original_title_and_labels_search_translation(self):
        import copy
        original={**self.songs[0],'title':'KAWAII♡めたもる交響曲','aliases':[]}
        snapshot=copy.deepcopy(original)
        self.catalog.songs=[original];self.dialog.query.set('귀여운');self.dialog.filter_songs()
        self.assertEqual(self.dialog.selected_song['title'],'KAWAII♡めたもる交響曲')
        self.assertEqual(original,snapshot)
        text=self.dialog.song_detail.get('1.0','end')
        self.assertIn('검색용 번역 (비공식)',text)
        self.assertIn('일본어 원제·버전 출처',text)
        self.assertNotIn('한국어 제목:',text)

    def test_live_song_search_accepts_korean_search_aids(self):
        event={**self.live_events[0],'songs':[{'title':'KAWAII♡めたもる交響曲'}]}
        self.live_catalog.events=[event]
        self.dialog.live_query.set('귀여운');self.dialog.filter_lives()
        self.assertEqual(self.dialog.live_tree.get_children(),(event['id'],))

    def test_live_search_uses_cached_index_and_every_page_is_reachable(self):
        from explorer_library import search_key
        events=[{**self.live_events[0],'id':f'event-{index}',
                 'date':f'{2000+index//12:04d}-{index%12+1:02d}-01','title':f'Concert needle {index}'}
                for index in range(307)]
        self.live_catalog.events=events;self.dialog.index_lives()
        self.dialog.live_query.set('needle')
        with patch('explorer_library.search_key',wraps=search_key) as normalize:
            self.dialog.filter_lives()
        self.assertEqual(normalize.call_count,1)
        self.assertEqual(len(self.dialog.live_filtered),307)
        self.assertEqual(len(self.dialog.live_tree.get_children()),100)
        visible=set(self.dialog.live_tree.get_children())
        for _ in range(3):
            self.dialog.change_live_page(1);visible.update(self.dialog.live_tree.get_children())
        self.assertEqual(visible,{event['id'] for event in events})
        self.assertEqual(self.dialog.live_next_page.cget('state'),'disabled')
        self.dialog.show_live(events[0])
        self.assertEqual(self.dialog.selected_live['id'],events[0]['id'])
        self.assertTrue(self.dialog.live_tree.exists(events[0]['id']))

    def test_song_live_history_does_not_hide_events_after_twentieth(self):
        events=[{**self.live_events[0],'id':f'event-{index}','title':f'History {index}'} for index in range(25)]
        self.live_catalog.for_song.return_value=events
        self.dialog.show_song_detail()
        detail = self.dialog.song_detail
        history_links = [tag for tag in detail.navigation
                         if 'History ' in detail.get(*detail.tag_ranges(tag))]
        self.assertEqual(len(history_links),25)
        self.assertIn('라이브 공연 기록 · 25회',self.dialog.song_detail.get('1.0','end'))
        self.assertIn('History 24',self.dialog.song_detail.get('1.0','end'))
        with patch.object(self.dialog,'show_live') as show:
            list(self.dialog.song_detail.navigation.values())[-1]()
        show.assert_called_once_with(events[-1])

    def test_empty_live_results_do_not_mean_unperformed_or_leave_source_actions_active(self):
        self.dialog.live_query.set('missing performance');self.dialog.filter_lives()
        self.assertIsNone(self.dialog.selected_live)
        self.assertIn('미공연을 뜻하지 않습니다',self.dialog.live_detail.get('1.0','end'))
        for button in (self.dialog.live_source_button,self.dialog.live_date_source_button,
                       self.dialog.live_setlist_source_button):self.assertEqual(button.cget('state'),'disabled')

    def test_live_update_uses_separate_store_and_refreshes_years_matches_and_scope(self):
        new={**self.live_events[0],'id':'new-concert','date':'2026-09-02','title':'New Concert'}
        def reload_events():self.live_catalog.events=[new,*self.live_events]
        self.live_catalog.reload.side_effect=reload_events
        self.dialog.live_match_cache['outdated']=[]
        refresh=Mock(return_value={'updated':True,'cancelled':False,'errors':['one event failed']})
        with patch.dict('sys.modules',{'live_catalog_update':SimpleNamespace(refresh_live_catalog=refresh)}):
            work,done=self.capture_job(self.dialog.update_live_catalog)
            done(work())
        self.assertEqual(refresh.call_args.args[0],self.directory/'live_catalog.json')
        self.assertIs(refresh.call_args.kwargs['cancel'],self.dialog.cancel)
        self.live_catalog.reload.assert_called_once_with()
        self.catalog.reload.assert_not_called()
        self.assertIn('2026',self.dialog.live_year_combo.cget('values'))
        self.assertNotIn('outdated',self.dialog.live_match_cache)
        self.assertIn('공연 3회 · 곡목 3개',self.dialog.live_scope.get())
        self.assertIn('일부 기록 수집 실패 1건',self.dialog.status.get())
        self.assertFalse(self.dialog.live_update_active)

    def test_cancelled_live_update_reloads_saved_live_records_without_running_late_callback(self):
        with patch('explorer_dialog.threading.Thread') as thread:
            self.dialog.update_live_catalog()
            run=thread.call_args.kwargs['target']
        self.dialog.cancel_job()
        refresh=Mock(return_value={'updated':False,'cancelled':True,'errors':[]})
        with patch.dict('sys.modules',{'live_catalog_update':SimpleNamespace(refresh_live_catalog=refresh)}):run()
        self.poll()
        self.live_catalog.reload.assert_called_once_with()
        self.assertFalse(self.dialog.live_update_active)
        self.assertEqual(set(self.dialog.live_tree.get_children()),{'concert-2024','concert-2025'})
        self.assertEqual(self.dialog.status.get(),'작업을 취소했습니다.')

    def test_song_live_history_does_not_claim_same_recording_version(self):
        self.live_catalog.for_song.return_value = [self.live_events[0]]
        self.dialog.show_song_detail()
        text = self.dialog.song_detail.get('1.0','end')
        self.assertIn('2024-03-02  ·  Live 2024',text)
        self.assertIn('같은 녹음 버전이라는 뜻은 아닙니다',text)

    def test_song_and_live_links_navigate_both_directions_without_guessing_recording(self):
        self.live_catalog.for_song.return_value = [self.live_events[0]]
        self.dialog.show_song_detail()
        detail = self.dialog.song_detail
        next(command for tag, command in detail.navigation.items()
             if 'Live 2024' in detail.get(*detail.tag_ranges(tag)))()
        self.assertEqual(self.dialog.notebook.select(),str(self.dialog.live_tab))
        self.assertEqual(self.dialog.selected_live['id'],'concert-2024')
        self.assertIn('음원 후보 1개',self.dialog.live_detail.get('1.0','end'))
        list(self.dialog.live_detail.navigation.values())[0]()
        self.assertEqual(self.dialog.notebook.select(),str(self.dialog.song_tab))
        self.assertEqual([item['id'] for item in self.dialog.filtered],['a'])
        self.assertIn('공연 녹음 버전은 별도로 확인',self.dialog.status.get())
        self.dialog.reset_filters()
        self.assertEqual(len(self.dialog.filtered),3)
        self.assertIsNone(self.dialog.live_song_filter)

    def test_live_setlist_without_same_series_match_remains_explicitly_unconfirmed(self):
        self.catalog.songs = [{**song('wrong-brand'),'title':'Another'}]
        self.dialog.live_match_cache.clear()
        self.dialog.show_live(self.live_events[1])
        self.assertEqual(self.dialog.live_detail.navigation,{})
        self.assertIn('사전 자료 미확인',self.dialog.live_detail.get('1.0','end'))
        self.assertIn('일부 기록',self.dialog.live_detail.get('1.0','end'))

    def test_detail_navigation_supports_keyboard_and_search_keeps_active_tab(self):
        self.live_catalog.for_song.return_value = [self.live_events[0]]
        self.dialog.show_song_detail()
        detail = self.dialog.song_detail
        detail.mark_set('insert','1.0')
        self.dialog.move_text_link(SimpleNamespace(widget=detail),1)
        self.assertTrue(detail.tag_ranges('active_link'))
        while 'Live 2024' not in detail.get(*detail.tag_ranges('active_link')):
            self.dialog.move_text_link(SimpleNamespace(widget=detail),1)
            self.assertTrue(detail.tag_ranges('active_link'))
        self.dialog.activate_text_link(SimpleNamespace(widget=detail))
        self.assertEqual(self.dialog.notebook.select(),str(self.dialog.live_tab))
        self.dialog.focus_search()
        self.assertEqual(self.dialog.notebook.select(),str(self.dialog.live_tab))

    def assert_buttons_fit(self, window):
        self.root.update()
        active = self.dialog.window.nametowidget(self.dialog.notebook.select()) if window is self.dialog.window else window
        expected = [widget for widget in descendants(active) if isinstance(widget, tk.Button)]
        if window is self.dialog.window:expected.append(self.dialog.cancel_button)
        for widget in expected:
            self.assertTrue(widget.winfo_ismapped(), f'Hidden action: {widget.cget("text")}')
        for widget in descendants(window):
            if isinstance(widget, tk.Button) and widget.winfo_viewable():
                with self.subTest(button=widget.cget('text')):
                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())
                    self.assertGreaterEqual(widget.winfo_rootx(), window.winfo_rootx())
                    self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),
                                         window.winfo_rootx()+window.winfo_width())
                    # The settings canvas is intentionally scrollable vertically.
                    if self.dialog.notebook.select() != str(self.dialog.settings_tab):
                        self.assertGreaterEqual(widget.winfo_rooty(), window.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),
                                             window.winfo_rooty()+window.winfo_height())

    def test_main_actions_fit_at_900_and_1280_width(self):
        for width in (900, 1280):
            self.dialog.window.geometry(f'{width}x620')
            for tab in (self.dialog.song_tab, self.dialog.person_tab, self.dialog.live_tab, self.dialog.local_tab,
                        self.dialog.settings_tab):
                self.dialog.notebook.select(tab)
                self.assert_buttons_fit(self.dialog.window)
            self.dialog.notebook.select(self.dialog.song_tab);self.root.update()
            self.assertGreater(self.dialog.search.winfo_width(), 350)
            self.assertGreater(self.dialog.song_tree.winfo_width(), 400)
            self.assertGreater(self.dialog.song_detail.winfo_width(), 300)
            self.assertTrue(self.dialog.status_label.winfo_ismapped())
            self.assertLessEqual(self.dialog.status_label.winfo_rooty()+self.dialog.status_label.winfo_height(),
                                 self.dialog.window.winfo_rooty()+self.dialog.window.winfo_height())

    def test_candidate_actions_fit_at_minimum_width(self):
        self.dialog.candidates_dialog(self.songs[0], [track()])
        self.dialog.candidate_window.geometry('720x450')
        self.assert_buttons_fit(self.dialog.candidate_window)


if __name__ == '__main__':unittest.main()
