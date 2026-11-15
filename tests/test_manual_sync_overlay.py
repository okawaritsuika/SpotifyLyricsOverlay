from dataclasses import replace
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import time

import test_background_overlay as background
from lyrics import Line
from lyric_merge import make_snapshot
from sync_reanalysis import connect, initialize, pack
from user_annotations import SingerRoster


class ManualSyncOverlayTests(unittest.TestCase):
    setUp = background.BackgroundOverlayTests.setUp
    tearDown = background.BackgroundOverlayTests.tearDown
    provider = background.BackgroundOverlayTests.provider
    receive = background.BackgroundOverlayTests.receive
    position = background.BackgroundOverlayTests.position

    def test_developer_table_shows_singers_and_updates_the_selected_credit(self):
        self.receive(times=[None] * 3)
        names = ('天海春香', '如月千早', '星井美希', '萩原雪歩', '高槻やよい',
                 '菊地真', '水瀬伊織', '四条貴音', '秋月律子', '三浦あずさ')
        self.app.singer_edits.save(0, names)
        self.app.singer_edits.save(1, ('如月千早',))
        self.app.singer_edits.save(2, (), keep_carry=True)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.assertEqual(dialog.rows.heading('singers', 'text'), '가창자')
        self.assertEqual(len(dialog.rows.set('0', 'singers').split(', ')), 10)
        self.assertEqual(len(dialog.selected_singers.cget('text').split(', ')), 10)
        self.assertIn('미우라 아즈사', dialog.selected_singers.cget('text'))
        dialog.select_for_edit(1)
        self.assertEqual(dialog.rows.set('1', 'singers'), '치하야')
        self.assertEqual(dialog.selected_singers.cget('text'), '선택 줄 가창자 · 키사라기 치하야')
        self.app.singer_edits.save(1, ('天海春香',))
        self.app.render()
        self.assertEqual(dialog.rows.set('1', 'singers'), '하루카')
        self.assertIn('아마미 하루카', dialog.selected_singers.cget('text'))
        dialog.select_for_edit(2)
        self.assertEqual(dialog.rows.set('2', 'singers'), '')
        self.assertFalse(dialog.selected_singers.winfo_manager())
        self.app.set_playback(replace(self.playback, title='Next Song'))
        self.app.render()
        self.assertEqual(dialog.rows.set('0', 'singers'), '')
        self.assertEqual(dialog.selected_singers.cget('text'), '')

    def test_developer_singer_roster_sets_number_keys_without_editing_lyric_rows(self):
        artists = patch.object(self.app, 'displayed_artists', return_value=())
        artists.start()
        self.addCleanup(artists.stop)
        self.receive(times=[None] * 3)
        self.app.singer_edits.save(0, ('如月千早',))  # A pick from the old editor.
        path = self.app.singer_edits.path
        original_rows = path.read_bytes()
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.select_for_edit(1)
        dialog.edit_singers_button.invoke()
        editor = dialog.singer_editor
        self.root.update_idletasks()
        self.assertEqual(editor.selected.get(0, 'end'), ('1   키사라기 치하야',))
        self.assertTrue(editor.singer.instate(('disabled',)))

        def choose_brand(brand):
            editor.brand.set(brand)
            editor.brand.event_generate('<<ComboboxSelected>>')
            return editor.singer.cget('values')

        self.assertNotIn('키사라기 치하야', choose_brand('765 ALLSTARS'))
        million = choose_brand('밀리언 라이브!')
        self.assertIn('카스가 미라이', million)
        self.assertNotIn('시마무라 우즈키', million)
        editor.singer.current(million.index('카스가 미라이'))
        editor.singer.event_generate('<<ComboboxSelected>>')
        editor.add_button.invoke()
        self.assertEqual(editor.selected.get(0, 'end'), ('1   키사라기 치하야', '2   카스가 미라이'))
        cinderella = choose_brand('신데렐라 걸즈')
        self.assertIn('시마무라 우즈키', cinderella)
        self.assertNotIn('카스가 미라이', cinderella)
        editor.singer.current(cinderella.index('시마무라 우즈키'))
        editor.singer.event_generate('<<ComboboxSelected>>')
        editor.add_button.invoke()
        dialog.select_for_edit(2)
        editor.save_button.invoke()
        self.assertIsNone(dialog.singer_editor)
        self.assertEqual(path.read_bytes(), original_rows)
        self.assertIn('2 미라이', dialog.singer_keys.cget('text'))
        self.assertIn('3 우즈키', dialog.singer_keys.cget('text'))

        event = SimpleNamespace(keysym='2', state=0, widget=dialog.rows)
        dialog.select_for_edit(1)
        dialog.key_release(event)
        self.assertEqual(dialog.rows.set('1', 'singers'), '미라이')
        event.keysym = '3'
        dialog.key_release(event)
        self.assertEqual(dialog.rows.set('1', 'singers'), '미라이, 우즈키')
        dialog.key_release(event)
        self.assertEqual(dialog.rows.set('1', 'singers'), '미라이')
        self.assertEqual(dialog.rows.set('0', 'singers'), '치하야')

        dialog.edit_singers_button.invoke()
        editor = dialog.singer_editor
        editor.selected.selection_set(0)
        editor.update_buttons()
        editor.remove_button.invoke()
        editor.save_button.invoke()
        self.assertIn('1 미라이', dialog.singer_keys.cget('text'))
        self.assertEqual(dialog.rows.set('0', 'singers'), '치하야')
        saved = SingerRoster(self.store.directory)
        saved.select(self.playback.key)
        self.assertEqual(saved.names, ('春日 未来', '島村卯月'))
        saved.select(replace(self.playback, album='Another recording').key)
        self.assertIsNone(saved.names)

        dialog.edit_singers_button.invoke()
        editor = dialog.singer_editor
        editor.selected.selection_set(0, 'end')
        editor.remove()
        editor.save_button.invoke()
        self.assertFalse(dialog.singer_keys.winfo_manager())
        saved.select(self.playback.key)
        self.assertEqual(saved.names, ())

    def test_developer_singer_editor_discards_changes_on_track_change(self):
        self.receive(times=[None] * 3)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.edit_singers_button.invoke()
        editor = dialog.singer_editor
        editor.names.append('天海春香')
        path = self.app.singer_roster.path
        self.app.set_playback(replace(self.playback, title='Next Song'))
        self.assertIsNone(dialog.singer_editor)
        self.assertFalse(path.exists())

    def test_developer_number_keys_assign_singers_without_enabling_singer_input(self):
        artists = ('First singer', 'Second singer', 'Third singer', 'Fourth singer', 'Fifth singer')
        with patch.object(self.app, 'displayed_artists', return_value=artists):
            self.receive(times=[None]*3)
            self.root.attributes('-alpha', 0)
            self.root.deiconify()
            self.app.open_manual_sync()
            dialog = self.app.manual_sync_dialog
            dialog.window.attributes('-alpha', 0)
            dialog.window.geometry('680x470')
            dialog.window.deiconify()
            self.root.update()
            self.assertIsNone(self.app.singer_input_index)
            self.assertIn('5 Fifth singer', dialog.singer_keys.cget('text'))

            dialog.select_for_edit(1)
            dialog.rows.focus_force()
            self.root.update()
            for key in ('1', '5', '1'):
                dialog.rows.event_generate(f'<KeyRelease-{key}>')
            self.assertEqual(self.app.singer_edits.rows, {1: ('Fifth singer',)})
            self.assertEqual(dialog.rows.set('1', 'singers'), 'Fifth singer')

            x, y, _, _ = dialog.rows.bbox('1', 'lyric')
            event = SimpleNamespace(x=x+8, y=y+5)
            dialog.remember_lyric_click(event)
            dialog.edit_lyric(event)
            entry = dialog.lyric_entry
            entry.focus_force()
            self.root.update()
            entry.event_generate('<KeyRelease-2>')
            self.assertEqual(self.app.singer_edits.rows, {1: ('Fifth singer',)})
            entry.event_generate('<Escape>')

            self.root.focus_force()
            self.root.update()
            self.root.event_generate('<KeyRelease-2>')
            self.assertEqual(self.app.singer_edits.rows, {1: ('Second singer', 'Fifth singer')})
            self.assertIsNone(self.app.singer_input_index)

            self.app.set_playback(replace(self.playback, title='Next Song'))
            self.root.event_generate('<KeyRelease-3>')
            self.assertFalse(dialog.singer_keys.winfo_manager())
            self.assertEqual(self.app.singer_edits.rows, {})

        standard = ttk.Button(dialog.window, text='이전')
        self.assertLess(dialog.previous_button.winfo_reqwidth(), standard.winfo_reqwidth())
        self.assertLess(dialog.previous_button.winfo_reqheight(), standard.winfo_reqheight())

    def test_developer_overlay_keeps_following_spotify_sync_without_saved_times(self):
        self.receive()
        self.assertEqual(self.app.timing['times'], [None] * 3)
        self.app.singer_edits.save(0, ('First singer',))
        self.app.singer_edits.save(1, ('Second singer',))
        self.app.singer_edits.save(2, ('Third singer',))
        self.position(12)
        self.app.manual_index = 0
        self.app.follow.set(False)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.app.toggle_singer_input()
        self.assertEqual(self.app.displayed_index(), 0)
        self.assertEqual(self.app.current_line_singers(), ('First singer',))
        self.position(22)
        self.assertEqual(self.app.displayed_index(), 1)
        self.assertEqual(self.app.original_label.cget('text'), background.TEXTS[1])
        self.assertEqual(self.app.current_line_singers(), ('Second singer',))
        self.assertEqual(dialog.rows.selection(), ('0',))
        self.assertIn('First singer', dialog.selected_singers.cget('text'))
        self.position(32)
        self.assertEqual(self.app.displayed_index(), 2)
        self.assertEqual(self.app.current_line_singers(), ('Third singer',))
        self.assertEqual(dialog.rows.selection(), ('0',))

    def test_recording_saves_each_line_immediately_and_survives_reopening(self):
        self.receive(times=[None] * 3)
        before = {**self.app.timing, 'times': [2, 4, 6], 'offset': 1,
                  'origins': ['audio'] * 3}
        self.app.save_current_timing(before)
        self.app.timing = before
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.root.update()
        self.assertIs(dialog.window.master, self.root)
        self.position(11)
        dialog.next_button.invoke()
        store = self.app.live_lyrics.timing_store
        song = self.app.song
        self.assertEqual(store.load(song)['times'], [10, None, None])
        self.position(21)
        dialog.next_button.invoke()
        self.assertEqual(store.load(song)['times'], [10, 20, None])
        self.position(31)
        dialog.next_button.invoke()
        self.assertEqual(dialog.index, 3)
        dialog.previous_button.invoke()
        self.position(30)
        dialog.next_button.invoke()
        self.assertEqual(store.load(song)['times'], [10, 20, 29])
        self.assertTrue(self.app.follow.get())
        self.assertIsNone(self.app.manual_index)
        self.assertEqual(self.app.effective_times()[0], [10, 20, 29])
        self.assertIn('3번 시작 00:30.000 저장', dialog.status.cget('text'))
        dialog.close()
        self.app.open_manual_sync(search=False)
        reopened = self.app.manual_sync_dialog
        reopened.load()
        self.assertEqual(reopened.baseline, ([10, 20, 29], ['manual']*3))

    def test_track_change_preserves_saved_line_and_rejects_stale_recording(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.position(12)
        dialog.next_button.invoke()
        song = self.app.song
        before = self.app.live_lyrics.timing_store.load(song)
        self.app.set_playback(replace(self.playback, title='Next Song'))
        self.app.render()
        self.assertEqual(str(dialog.next_button.cget('state')), 'disabled')
        with patch.object(self.app, 'save_current_timing') as save:
            dialog.record_next()
            save.assert_not_called()
        self.assertEqual(before['times'], [12, None, None])
        self.assertEqual(self.app.live_lyrics.timing_store.load(song), before)

    def test_failed_save_keeps_current_row_and_previously_saved_times(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.position(12)
        dialog.record()
        before = self.app.live_lyrics.timing_store.load(self.app.song)
        self.position(20)
        with patch.object(self.app, 'save_current_timing', side_effect=OSError('쓰기 실패')):
            dialog.record()
        self.assertEqual(dialog.index, 1)
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song), before)
        self.assertIn('쓰기 실패', dialog.status.cget('text'))
        self.position(10)
        dialog.record()
        self.assertEqual(dialog.index, 1)
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song), before)
        self.position(21)
        dialog.record()
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song)['times'], [12, 21, None])

    def test_clear_later_sync_to_fix_conflicting_earlier_row(self):
        self.receive(times=[None]*3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        store = self.app.live_lyrics.timing_store
        song = self.app.song
        before = {**self.app.timing, 'times': [10, None, 20], 'origins': ['manual', None, 'manual']}
        self.app.save_current_timing(before)
        self.app.timing = before
        dialog.update_times()
        dialog.select_for_edit(1)
        self.position(24)
        dialog.record()
        self.assertEqual(store.load(song)['times'], [10, None, 20])
        self.assertIn('3번 줄', dialog.status.cget('text'))
        dialog.select_for_edit(2)
        dialog.clear_button.invoke()
        self.assertEqual(store.load(song)['times'], [10, None, None])
        self.assertEqual(store.load(song)['cleared_rows'], [2])
        self.assertEqual(dialog.rows.set('2', 'time'), '싱크 없음')
        self.app.timing = store.load(song)
        dialog.close()
        self.app.open_manual_sync(search=False)
        dialog = self.app.manual_sync_dialog
        dialog.load()
        self.assertEqual(dialog.rows.set('2', 'time'), '싱크 없음')
        dialog.select_for_edit(1)
        dialog.record()
        self.assertEqual(store.load(song)['times'], [10, 24, None])
        self.position(30)
        dialog.record()
        self.assertEqual(store.load(song)['times'], [10, 24, 30])
        self.assertNotIn('cleared_rows', store.load(song))

    def test_finds_unsynced_work_and_rewinds_after_skipping_spotify_timing(self):
        self.receive()
        with (patch.object(self.app.monitor, 'request_skip', return_value=True) as skip,
              patch.object(self.app.monitor, 'request_seek', return_value=True) as seek):
            self.app.open_manual_sync(search=False)
            dialog = self.app.manual_sync_dialog
            dialog.searching = True
            dialog.advance()
            self.assertIsNone(dialog.song)
            skip.assert_called_once_with('next')
            self.app.finish_track_change('next', True, '')
            dialog.advance()
            skip.assert_called_once()

            self.playback = replace(self.playback, title='Needs Sync')
            self.app.set_playback(self.playback)
            self.receive(times=[None] * 3)
            dialog.advance()
            self.assertIs(dialog.song, self.app.song)
            self.assertEqual(dialog.index, 0)
            seek.assert_called_once_with('first', self.playback.key)
            self.assertEqual(str(dialog.next_button.cget('state')), 'disabled')
            self.app.finish_seek('first', True, '')
            self.position(.2)
            dialog.advance()
            self.assertEqual(str(dialog.next_button.cget('state')), 'normal')
            dialog.work_button.invoke()
            self.assertEqual(skip.call_count, 2)

    def test_alsong_modal_edited_search_preview_and_temporary_restore(self):
        self.receive(times=[None]*3)
        self.position(.5)
        before = self.app.song
        cache = self.app.lyric_cache.load(self.playback)
        self.root.deiconify()
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.root.update()
        dialog.alsong_button.invoke()
        modal = dialog.alsong_dialog
        self.root.update()
        self.assertEqual(tuple(value.get() for value in modal.query), ('Test Song', 'Singer', 'Album'))
        self.assertIs(modal.window.grab_current(), modal.window)
        modal.query[0].set('Edited')
        modal.query[1].set('')
        records = [{'lyricID':'123', 'title':'Edited', 'artist':'A', 'album':'B'}]
        result = {'provider_id':'123', 'lines':(Line('青い空を見る', '', ''), Line('二行', 'にぎょう', '두 번째 줄')),
                  'times':[2, 4], 'clock_mismatch':True, 'duration':120, 'last_onset':4}

        def wait_request():
            deadline = time.monotonic()+2
            while modal.waiting is not None and time.monotonic()<deadline:
                self.root.update()
                time.sleep(.01)
            self.assertIsNone(modal.waiting)

        with patch('alsong_dialog.search_alsong', return_value=records) as search:
            modal.search_button.invoke()
            wait_request()
            search.assert_called_once_with('Edited', '', 'Album')
        with patch('alsong_dialog.load_alsong_registration', return_value=result) as load:
            modal.preview_button.invoke()
            wait_request()
            load.assert_called_once_with('123', self.playback.duration)
        self.assertIn('青い空を見る\n', modal.text.get('1.0', 'end'))
        self.assertIn('二行\nにぎょう\n두 번째 줄', modal.text.get('1.0', 'end'))
        from lyric_annotations import readings_for_display
        reading = readings_for_display(result['lines'])[0]
        self.assertTrue(reading)
        self.assertIn(f'青い空を見る\n{reading}', modal.text.get('1.0', 'end'))
        self.assertIn('싱크 2줄 · 재생 길이 차이', modal.status.get())
        self.assertIn('번역 1줄', modal.status.get())
        self.assertIn('Spotify 길이 1:30 · 알송 곡 길이 2:00 · 마지막 가사 시작 0:04', modal.timing_info.get())
        modal.link_button.invoke()
        self.assertIsNone(dialog.alsong_dialog)
        self.assertEqual(dialog.originals, ('青い空を見る', '二行'))
        self.assertEqual(dialog.baseline[0], [2, 4])
        self.assertFalse(self.app.live_lyrics.timing_store.path(self.app.song).exists())
        self.position(5)
        self.assertEqual(self.app.original_label.cget('text'), '二行')
        self.assertEqual(self.app.reading_label.cget('text'), 'にぎょう')
        self.assertEqual(self.app.korean_label.cget('text'), '두 번째 줄')
        self.receive(times=[10, 20, 30])
        self.assertEqual(self.app.song.lines, result['lines'])
        dialog.select_for_edit(0)
        self.position(3)
        dialog.record()
        self.assertEqual(dialog.baseline[0], [3, 4])
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song)['times'], [3, None])
        self.assertEqual(self.app.lyric_cache.load(self.playback), cache)
        dialog.unlink_alsong_button.invoke()
        self.assertIs(self.app.song, before)
        self.assertEqual(dialog.originals, background.TEXTS)
        dialog.link_alsong(result)
        self.assertEqual(dialog.baseline[0], [3, 4])

    def test_alsong_search_reopens_with_saved_query_and_reset_restores_spotify_fields(self):
        import tkinter as tk
        from app import LyricsApp

        def open_search():
            self.app.open_manual_sync()
            dialog = self.app.manual_sync_dialog
            dialog.open_alsong()
            return dialog.alsong_dialog

        def wait_request(modal):
            deadline = time.monotonic()+2
            while modal.waiting is not None and time.monotonic()<deadline:
                self.root.update()
                time.sleep(.01)
            self.assertIsNone(modal.waiting)

        def fields(modal):
            return tuple(value.get() for value in modal.query)

        defaults = ('Test Song', 'Singer', 'Album')
        edited = ('Edited', '', 'Preferred Album')
        with patch('alsong_dialog.search_alsong', return_value=[]) as search:
            modal = open_search()
            for variable, value in zip(modal.query, edited):
                variable.set(value)
            modal.search_button.invoke()
            wait_request(modal)
            search.assert_called_once_with(*edited)
            modal.close()
            self.app.close()
            self.root = tk.Tk()
            self.root.withdraw()
            self.app = LyricsApp(self.root, self.store, start_services=False)

            self.app.set_playback(replace(self.playback, album='Another recording'))
            modal = open_search()
            self.assertEqual(fields(modal), ('Test Song', 'Singer', 'Another recording'))
            self.assertIsNone(modal.waiting)
            modal.close()

            self.app.set_playback(replace(self.playback, duration=120))
            modal = open_search()
            self.assertEqual(fields(modal), defaults)
            self.assertIsNone(modal.waiting)
            modal.close()

            self.app.set_playback(self.playback)
            modal = open_search()
            self.assertEqual(fields(modal), edited)
            wait_request(modal)
            self.assertEqual(search.call_count, 2)
            search.assert_called_with(*edited)

            modal.reset_button.invoke()
            self.assertEqual(fields(modal), defaults)
            wait_request(modal)
            search.assert_called_with(*defaults)
            self.assertFalse(self.store.settings()['alsong_search_queries'])
            modal.close()
            modal = open_search()
            self.assertEqual(fields(modal), defaults)
            self.assertIsNone(modal.waiting)

    def test_alsong_temporary_link_does_not_survive_track_change(self):
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.assertIsNone(self.app.song)
        dialog.link_alsong({'provider_id':'123', 'lines':(Line('一行', '', ''),), 'times':[2]})
        self.assertTrue(self.app.is_bound())
        self.app.set_playback(replace(self.playback, title='Other Song'))
        dialog.advance()
        self.assertIsNone(self.app.temporary_alsong)
        self.assertIsNone(self.app.song)
        self.assertFalse(dialog.rows.get_children())

    def test_remove_alsong_row_retains_timing_and_annotations_when_relinked(self):
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        result = {'provider_id':'123', 'lines':(Line('一行', '', ''), Line('削除', '', ''), Line('三行', '', '')),
                  'times':[2, 4, 6]}
        dialog.link_alsong(result)
        source = self.app.song
        self.app.singer_edits.save(1, ('Singer before',))
        self.app.singer_edits.save(2, ('Singer after',))
        self.app.lyric_text_edits.save(2, '三行を訂正')
        self.app.lyric_text_edits.save(2, '셋째 줄', field='korean')
        dialog.select_for_edit(2)
        self.position(7)
        dialog.record()
        dialog.select_for_edit(1)
        dialog.remove_lyric_button.invoke()
        self.assertEqual(dialog.originals, ('一行', '三行'))
        self.assertEqual(dialog.rows.selection(), ('1',))
        self.assertEqual(dialog.baseline[0], [2, 7])
        self.assertEqual(dialog.rows.set('1', 'lyric'), '三行を訂正')
        self.assertEqual(self.app.original_label.cget('text'), '三行を訂正')
        self.assertEqual(self.app.line_singers[1], ('Singer after',))
        timing = self.app.live_lyrics.timing_store.load(source)
        self.assertEqual(timing['removed_alsong_rows'], [1])
        self.assertEqual(timing['times'], [None, None, 7])
        dialog.unlink_alsong()
        dialog.link_alsong(result)
        self.assertEqual(dialog.baseline[0], [2, 7])
        self.assertEqual(dialog.originals, ('一行', '三行'))
        self.assertEqual(dialog.rows.set('1', 'lyric'), '三行を訂正')
        self.assertEqual(dialog.rows.set('1', 'translation'), '셋째 줄')
        self.assertEqual(self.app.line_singers[1], ('Singer after',))

    def test_insert_alsong_lyric_before_selection_preserves_existing_rows(self):
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        result = {'provider_id':'123', 'lines':(Line('一行', '', ''), Line('二行', '', '')),
                  'times':[2, 6]}
        dialog.link_alsong(result)
        self.app.singer_edits.save(1, ('Singer after',))
        self.app.lyric_text_edits.save(1, '二行を訂正')
        dialog.select_for_edit(1)
        with patch('manual_sync_dialog.askstring', return_value='追加する歌詞'):
            dialog.insert_lyric_button.invoke()
        self.assertEqual(dialog.originals, ('一行', '追加する歌詞', '二行'))
        self.assertEqual(dialog.rows.selection(), ('1',))
        self.assertEqual(dialog.baseline[0], [2, None, 6])
        self.assertEqual(dialog.rows.set('2', 'lyric'), '二行を訂正')
        self.assertEqual(self.app.line_singers[2], ('Singer after',))
        self.position(4)
        dialog.record()
        dialog.unlink_alsong()
        dialog.link_alsong(result)
        self.assertEqual(dialog.originals, ('一行', '追加する歌詞', '二行'))
        self.assertEqual(dialog.baseline[0], [2, 4, 6])
        self.assertEqual(dialog.rows.set('2', 'lyric'), '二行を訂正')
        self.assertEqual(self.app.line_singers[2], ('Singer after',))

    def test_spotify_lyrics_insert_and_delete_without_alsong(self):
        self.receive()
        self.position(20)
        with patch.object(self.app.monitor, 'request_skip') as skip:
            self.app.open_manual_sync()
            skip.assert_not_called()
        dialog = self.app.manual_sync_dialog
        self.assertEqual(dialog.baseline[0], [10, 20, 30])
        self.app.singer_edits.save(1, ('Singer',))
        self.app.lyric_text_edits.save(1, '修正')
        self.app.lyric_text_edits.save(1, '수정 번역', field='korean')
        self.app.lyric_text_edits.save(1, 'はなをえがく', field='reading')
        dialog.select_for_edit(1)
        with patch('manual_sync_dialog.askstring', return_value='新しい行'):
            dialog.insert_lyric_button.invoke()
        self.assertEqual(dialog.originals, (background.TEXTS[0], '新しい行', *background.TEXTS[1:]))
        self.assertEqual(dialog.rows.selection(), ('1',))
        self.assertEqual(dialog.baseline[0], [10, None, 20, 30])
        self.assertEqual(dialog.rows.set('2','lyric'), '修正')
        self.assertEqual(dialog.rows.set('2','translation'), '수정 번역')
        self.assertEqual(dialog.rows.set('2','reading'), 'はなをえがく')
        self.assertEqual(self.app.line_singers[2], ('Singer',))
        dialog.select_for_edit(0)
        dialog.remove_lyric_button.invoke()
        self.assertEqual(dialog.originals, ('新しい行', *background.TEXTS[1:]))
        self.assertEqual(dialog.baseline[0], [None, 20, 30])
        self.assertEqual(dialog.rows.set('1','lyric'), '修正')
        self.assertEqual(dialog.rows.set('1','translation'), '수정 번역')
        self.assertEqual(dialog.rows.set('1','reading'), 'はなをえがく')
        self.assertEqual(self.app.line_singers[1], ('Singer',))
        saved = self.store.library()[0]
        self.assertEqual(tuple(line.original for line in saved.lines), dialog.originals)
        self.assertEqual(self.store.timing(saved)['times'], [None, 20, 30])
        self.app.set_playback(replace(self.playback,title='Other Song'))
        self.app.set_playback(self.playback)
        self.assertEqual(self.app.song,saved)
        self.assertEqual(self.app.timing['times'], [None, 20, 30])

    def test_confirmed_alsong_restores_edited_layout_after_app_restart(self):
        import tkinter as tk
        from app import LyricsApp

        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        result = {'provider_id':'123', 'lines':(Line('一行', '', ''), Line('削除', '', ''), Line('三行', '', '')),
                  'times':[2, 4, 96]}
        dialog.link_alsong(result)
        self.assertEqual(dialog.baseline[0], [2, 4, 96])
        dialog.select_for_edit(1)
        dialog.remove_lyric_button.invoke()
        self.app.lyric_text_edits.save(2, '修正した三行')
        self.app.singer_edits.save(2, ('Singer after',))
        dialog.confirm_alsong_button.invoke()
        self.assertTrue(self.app.temporary_alsong.confirmed)
        self.assertIn('알송 확정', dialog.title.cget('text'))
        self.assertTrue(self.app.confirmed_alsong_path().is_file())
        self.position(7)
        dialog.select_for_edit(1)
        dialog.record()
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.app.set_playback(self.playback)
        self.assertTrue(self.app.temporary_alsong.confirmed)
        self.assertEqual(self.app.temporary_alsong.provider_times, (2, 4, 96))
        self.assertEqual(self.app.timing['times'], [2, 7])
        self.assertEqual(self.app.original_label.cget('text'), '修正した三行')
        self.assertEqual(self.app.line_singers[1], ('Singer after',))
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.assertEqual(dialog.originals, ('一行', '三行'))
        dialog.unlink_alsong_button.invoke()
        self.assertFalse(self.app.confirmed_alsong_path().exists())
        self.assertIsNone(self.app.temporary_alsong)

    def test_canceling_another_alsong_preview_restores_the_confirmed_registration(self):
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.link_alsong({'provider_id':'123', 'lines':(Line('一行', '', ''),), 'times':[2]})
        dialog.confirm_alsong_button.invoke()
        dialog.link_alsong({'provider_id':'456', 'lines':(Line('別の歌詞', '', ''),), 'times':[3]})
        self.assertFalse(self.app.temporary_alsong.confirmed)
        dialog.unlink_alsong_button.invoke()
        self.assertTrue(self.app.temporary_alsong.confirmed)
        self.assertEqual(dialog.originals, ('一行',))

    def test_third_party_timing_and_unknown_spotify_do_not_trigger_a_skip(self):
        self.receive(source='LRCLIB')
        self.position(.5)
        with patch.object(self.app.monitor, 'request_skip') as skip:
            self.app.open_manual_sync(search=False)
            dialog = self.app.manual_sync_dialog
            dialog.searching = True
            dialog.advance()
            self.assertIsNone(dialog.song)
            self.assertIn('확인 중', dialog.status.cget('text'))
            service = self.app.background_lyrics
            service.observe(self.playback, public_enabled=False)
            event = {'kind': 'status', 'key': service.request_key, 'state': 'not_found'}
            service.job = skip
            service.receive(skip, event)
            dialog.advance()
            self.assertIsNone(dialog.song)
            service.receive(skip, {**event, 'key': 'stale', 'lyrics_missing': True})
            dialog.advance()
            self.assertIsNone(dialog.song)
            service.receive(skip, {**event, 'lyrics_missing': True})
            dialog.advance()
            self.assertIs(dialog.song, self.app.song)
            skip.assert_not_called()

    def test_spotify_source_survives_another_provider_being_selected(self):
        self.receive(source='LRCLIB')
        self.app.lyric_merge_job.remember(make_snapshot('Spotify', self.app.song.lines,
                                                       [3, None, 9], recording_verified=True))
        with patch.object(self.app.monitor, 'request_skip', return_value=True) as skip:
            self.app.open_manual_sync(search=False)
            dialog = self.app.manual_sync_dialog
            dialog.searching = True
            dialog.advance()
            skip.assert_called_once_with('next')

    def test_failed_skip_stops_the_search_and_allows_manual_retry(self):
        self.receive()
        with patch.object(self.app.monitor, 'request_skip', return_value=True) as skip:
            self.app.open_manual_sync(search=False)
            dialog = self.app.manual_sync_dialog
            dialog.searching = True
            dialog.advance()
            self.app.finish_track_change('next', False, 'Spotify 이동 실패')
            dialog.advance()
            self.assertFalse(dialog.searching)
            self.assertEqual(dialog.status.cget('text'), 'Spotify 이동 실패')
            skip.assert_called_once()
            dialog.work_button.invoke()
            self.assertEqual(skip.call_count, 2)

    def test_ai_batch_candidate_is_auditioned_and_manual_edit_keeps_other_rows(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        song = self.app.song
        previous = {**self.app.timing, 'times': [8, 18, 28], 'origins': ['audio'] * 3}
        self.app.save_current_timing(previous)
        self.app.timing = previous
        recording = self.app.recording_contexts.capture(song, self.playback).key
        item = {'kind': 'song', 'target': song.id, 'recording': recording,
                'fingerprint': song.fingerprint, 'lines': list(background.TEXTS)}
        candidate = {'duration': 90, 'engine': 'romaji_ctc', 'times': [10, 20, 30],
                     'details': [{'quality': 'high'}] * 3}
        initialize(self.store.directory)
        with connect(self.store.directory, write=True) as db:
            db.execute("""INSERT INTO queue (id,item,status,candidate,mode,can_apply)
                          VALUES ('song-review',?,'done',?,'old',1)""", (pack(item), pack(candidate)))
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.assertEqual(dialog.baseline, ([10, 20, 30], ['audio'] * 3))
        self.assertIn('AI', dialog.rows.set('1', 'time'))
        dialog.select(1)
        with patch.object(self.app.monitor, 'request_seek', return_value=True) as seek:
            dialog.listen_button.invoke()
            seek.assert_called_once_with(18., self.playback.key)
            self.app.finish_seek(18., True, '')
        self.position(21)
        dialog.next_button.invoke()
        result = self.app.live_lyrics.timing_store.load(song)
        self.assertEqual(result['times'], [10, 21, 30])
        self.assertEqual(result['origins'], ['audio', 'manual', 'audio'])
        self.assertEqual(dialog.baseline, ([10, 21, 30], ['audio', 'manual', 'audio']))
        dialog.select_for_edit(2)
        dialog.clear_button.invoke()
        self.assertEqual(self.app.live_lyrics.timing_store.load(song)['times'], [10, 21, None])
        dialog.update_times()
        self.assertEqual(dialog.rows.set('2', 'time'), '싱크 없음')
        with connect(self.store.directory) as db:
            self.assertEqual(db.execute('select count(*) from overrides').fetchone()[0], 0)

    def test_arrow_seeks_to_selected_onset_and_marks_missing_rows(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        with patch.object(self.app,'manual_sync_baseline',return_value=([10,None,30],['audio',None,'manual'])):
            dialog.update_times()
        self.assertEqual(dialog.rows.set('1','time'),'싱크 없음')
        with patch.object(self.app.monitor,'request_seek',return_value=True) as seek:
            dialog.step_row(1)
            self.assertEqual(dialog.index,1)
            self.assertIn('싱크 없음',dialog.status.cget('text'))
            seek.assert_not_called()
            dialog.step_row(1)
            self.assertEqual(dialog.index,2)
            seek.assert_called_once_with(30.,self.playback.key)
            self.app.finish_seek(30.,True,'')
            self.position(31)
            dialog.advance()
            self.assertEqual(dialog.index,2)
            self.assertEqual(dialog.rows.set('2','line'),'▶ 3')
            self.assertIn('싱크 2/3줄',dialog.position.cget('text'))
            dialog.select(0)
            self.position(15)
            dialog.advance()
            self.assertEqual(dialog.rows.set('0','line'),'▶ 1')
            self.assertEqual(dialog.index,0)
            dialog.select(1)
            self.position(31)
            dialog.advance()
            self.assertEqual(dialog.rows.set('2','line'),'▶ 3')
            self.assertEqual(dialog.index,1)
            dialog.follow_button.invoke()
            self.assertIsNone(dialog.playhead)
            self.assertEqual(dialog.index,1)

    def test_workspace_has_independent_visible_row_and_uses_native_seek(self):
        self.receive(times=[10,None,30])
        self.position(31)
        workspace = self.app.workspace
        state = workspace.developer('get',{})
        dialog = self.app.manual_sync_dialog
        self.assertEqual(state['current'], self.app.visible_lyric_index())
        self.assertEqual((state['current'],state['selected']),(2,0))
        self.assertEqual([row['timingOrigin'] for row in state['rows']],['provider',None,'provider'])
        with patch.object(dialog,'row_stamp',side_effect=AssertionError('position polled all rows')):
            self.assertEqual(workspace.developer('position',{})['current'],2)
        with patch.object(self.app.monitor,'request_seek',return_value=True) as seek:
            row = workspace.developer('seek',{'track':state['track'],'index':0})
            self.assertEqual((row['current'],row['selected']),(2,0))
            seek.assert_called_once_with(10.,self.playback.key)
            row = workspace.developer('seek',{'track':state['track'],'index':1})
            self.assertEqual(row['selected'],1)
            self.assertIn('싱크 없음',row['status'])
            seek.assert_called_once()
            with self.assertRaisesRegex(ValueError,'재생 곡'):
                workspace.developer('seek',{'track':'stale','index':2})
        self.app.finish_seek(10.,True,'')
        self.position(10)
        self.assertEqual(workspace.developer('position',{})['current'],0)
        with patch.object(self.app.monitor,'request_seek',return_value=True) as seek:
            row = workspace.developer('seek',{'track':state['track'],'index':2})
            self.assertEqual((row['current'],row['selected']),(0,2))
            seek.assert_called_once_with(30.,self.playback.key)
        self.app.finish_seek(30.,True,'')
        self.position(31)
        row = workspace.developer('position',{})
        self.assertEqual((row['current'],row['selected']),(2,2))

    def test_workspace_line_edit_reuses_timing_and_native_roster(self):
        self.receive()
        workspace = self.app.workspace
        state = workspace.developer('get',{})
        with patch('manual_sync_dialog.performers',return_value=[]) as singer_catalog:
            workspace.developer('lineTool',{'track':state['track'],'index':0,'tool':'roster'})
        singer_catalog.assert_called_once()
        self.root.update_idletasks()
        roster = self.app.manual_sync_dialog.singer_editor
        self.assertTrue(roster.window.winfo_viewable())
        roster.close()
        added = workspace.developer('lineTool',{'track':state['track'],'index':1,'tool':'add','value':'歌詞を追加'})
        self.assertEqual(added['rows'][1]['original'],'歌詞を追加')
        self.assertEqual([row['time'] for row in added['rows']],[10,None,20,30])
        removed = workspace.developer('lineTool',{'track':added['track'],'index':1,'tool':'remove'})
        self.assertEqual([row['original'] for row in removed['rows']],list(background.TEXTS))

    def test_workspace_singer_input_needs_no_native_editor_window(self):
        self.receive()
        workspace = self.app.workspace
        state = workspace.developer('get',{})
        dialog = self.app.manual_sync_dialog
        row = {'track':state['track'],'index':1,'tool':'singerInput'}
        with self.assertRaisesRegex(ValueError,'찾지 못했습니다'):
            workspace.developer('open',{'tool':'native'})
        self.assertEqual(dialog.window.state(),'withdrawn')
        enabled = workspace.developer('lineTool',{**row,'enabled':True})
        self.assertTrue(enabled['singerInput'])
        self.assertEqual(self.app.singer_input_index,1)
        self.assertEqual(dialog.window.state(),'withdrawn')
        self.assertTrue(workspace.developer('lineTool',{**row,'enabled':True})['singerInput'])
        self.assertFalse(workspace.developer('lineTool',{**row,'enabled':False})['singerInput'])

    def test_qwer_offsets_accumulate_while_spotify_is_seeking(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.position(20)
        with patch.object(self.app.monitor,'request_seek',return_value=True) as seek:
            for key in 'qwerr':
                self.assertEqual(dialog.seek_key(SimpleNamespace(keysym=key,state=0)),'break')
            seek.assert_called_once_with(15.,self.playback.key)
            self.app.finish_seek(15.,True,'')
            self.assertEqual(seek.call_args.args,(24.,self.playback.key))
            self.app.finish_seek(24.,True,'')
            self.position(2)
            dialog.seek_key(SimpleNamespace(keysym='q',state=0))
            self.assertEqual(seek.call_args.args,(0.,self.playback.key))
        self.assertEqual(dialog.index,0)

    def test_both_windows_share_developer_keyboard_and_next_onset(self):
        self.receive(times=[None] * 3)
        self.position(.5)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        with patch.object(self.app, 'manual_sync_baseline', return_value=([10, 20, 30], ['audio'] * 3)):
            dialog.update_times()
        self.root.update()
        self.assertIs(self.root.focus_get(), self.root)
        store = self.app.live_lyrics.timing_store
        with patch.object(self.app.monitor, 'request_seek', return_value=True) as seek:
            self.root.event_generate('<Right>')
            self.assertIsNone(self.app.displayed_index())
            self.assertEqual(dialog.index, 1)
            seek.assert_called_once_with(20., self.playback.key)
            self.app.finish_seek(20., True, '')
            self.position(20)
            self.root.event_generate('<KeyRelease-w>')
            self.assertEqual(seek.call_args.args, (18., self.playback.key))
            self.app.finish_seek(18., True, '')
            dialog.rows.focus_force()
            self.root.update()
            dialog.rows.event_generate('<KeyRelease-w>')
            self.assertEqual(seek.call_args.args, (18., self.playback.key))
            self.app.finish_seek(18., True, '')
            dialog.rows.event_generate('<Left>')
            self.assertEqual(dialog.index, 0)
            self.assertEqual(seek.call_args.args, (10., self.playback.key))
            self.app.finish_seek(10., True, '')
            self.position(22.125)
            dialog.rows.event_generate('<KeyRelease-Down>')
            self.assertEqual(store.load(self.app.song)['times'], [10, 22.125, 30])
            self.assertEqual(self.app.displayed_index(), 1)
            self.root.focus_force()
            self.root.update()
            self.position(31.375)
            self.root.event_generate('<KeyRelease-Down>')
            self.assertEqual(store.load(self.app.song)['times'], [10, 22.125, 31.375])
            self.assertEqual(self.app.displayed_index(), 2)
            seek_count = seek.call_count
            self.root.event_generate('<KeyRelease-Down>')
            self.assertEqual(seek.call_count, seek_count)
        self.assertEqual(store.load(self.app.song)['origins'], ['audio', 'manual', 'manual'])

    def test_singer_assignment_and_immediate_sync_use_the_developer_selection(self):
        with patch.object(self.app, 'displayed_artists', return_value=('First singer', 'Second singer')):
            self.receive(times=[None]*3)
            self.position(.5)
            self.root.attributes('-alpha', 0)
            self.root.deiconify()
            self.root.update()
            self.app.open_manual_sync()
            self.root.update()
            dialog = self.app.manual_sync_dialog
            dialog.singer_input_button.invoke()
            self.assertIsNotNone(self.app.singer_input_index)
            self.assertIn('2 Second singer', self.app.mode_label.cget('text'))
            self.root.event_generate('<Escape>')
            self.assertIsNone(self.app.singer_input_index)
            label = self.app.original_label
            label.event_generate('<ButtonPress-1>', x=15, y=10)
            label.event_generate('<ButtonRelease-1>', x=15, y=10)
            self.assertIsNone(self.app.singer_input_index)
            dialog.singer_input_button.invoke()
            self.assertIsNotNone(self.app.singer_input_index)
            self.position(10)
            self.root.event_generate('<KeyRelease-1>')
            self.assertEqual(self.app.singer_edits.rows[0], ('First singer',))
            self.position(20)
            self.root.event_generate('<KeyRelease-Down>')
            self.assertEqual(dialog.index, 1)
            self.root.event_generate('<KeyRelease-2>')
            self.assertEqual(self.app.singer_edits.rows[1], ('Second singer',))
            self.root.event_generate('<KeyRelease-1>')
            self.assertEqual(self.app.singer_edits.rows[1], ('First singer', 'Second singer'))
            self.position(21)
            self.root.event_generate('<KeyRelease-Return>')
            store = self.app.live_lyrics.timing_store
            self.assertEqual(store.load(self.app.song)['times'], [None, 21, None])
            self.assertEqual(dialog.index, 2)
            self.root.event_generate('<KeyRelease-space>')
            self.assertIn(2, self.app.singer_edits.cleared)
            self.position(15)
            with patch.object(self.app.monitor, 'request_seek', return_value=True) as seek:
                self.root.event_generate('<Left>')
                seek.assert_called_once_with(21., self.playback.key)
                self.assertEqual(dialog.index, 1)
                self.assertIsNone(self.app.displayed_index())
                self.app.finish_seek(21., True, '')
                self.position(21)
                self.root.event_generate('<KeyRelease-e>')
                self.assertEqual(seek.call_args.args, (22., self.playback.key))
            dialog.rows.focus_force()
            self.root.update()
            dialog.rows.event_generate('<KeyRelease-space>')
            dialog.rows.event_generate('<KeyRelease-2>')
            self.assertEqual(self.app.singer_edits.rows[1], ('Second singer',))
            dialog.close()
            self.assertEqual(self.app.displayed_index(), 1)

    def test_up_in_both_windows_records_previous_end_without_moving_selection(self):
        self.receive(times=[None]*3)
        self.position(.5)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        self.root.update()
        timing = {**self.app.timing, 'times': [10, 20, 30], 'origins': ['manual']*3, 'offset': .5}
        self.app.save_current_timing(timing)
        self.app.timing = timing
        dialog.update_times()
        self.position(18.5)
        store = self.app.live_lyrics.timing_store
        song = self.app.song
        with patch.object(self.app.monitor, 'request_seek') as seek:
            self.root.event_generate('<KeyRelease-Up>')
            self.assertEqual(store.load(song)['times'], [10, 20, 30])
            dialog.select_for_edit(1)
            self.assertEqual(self.app.displayed_index(), 0)
            self.root.event_generate('<KeyRelease-Up>')
            self.assertEqual(store.load(song)['times'], [10, 18, 30])
            self.assertEqual(dialog.rows.selection(), ('1',))
            self.assertIn('1번 끝 → 2번 시작', dialog.status.cget('text'))
            self.position(31.5)
            self.root.event_generate('<KeyRelease-Up>')
            self.assertEqual(store.load(song)['times'], [10, 18, 30])
            self.assertIn('3번 줄', dialog.status.cget('text'))
            seek.assert_not_called()
        dialog.rows.focus_force()
        self.root.update()
        self.position(19.5)
        dialog.rows.event_generate('<KeyRelease-Up>')
        self.assertEqual(dialog.rows.selection(), ('1',))
        self.assertEqual(store.load(song)['times'], [10, 19, 30])

    def test_overlay_up_cannot_record_sync_outside_developer_mode(self):
        self.receive(times=[None]*3)
        timing = {**self.app.timing, 'times': [10, 20, 30], 'origins': ['manual']*3}
        self.app.save_current_timing(timing)
        self.app.timing = timing
        self.position(22)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.root.focus_force()
        self.root.update()
        self.assertEqual(self.app.displayed_index(), 1)
        self.root.event_generate('<KeyRelease-Up>')
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song)['times'], [10, 20, 30])
        self.assertEqual(self.app.displayed_index(), 1)

    def test_developer_selection_stays_centered_while_overlay_follows_saved_sync(self):
        count = 60
        self.receive(times=[None]*count, texts=tuple(f'行 {index}' for index in range(count)))
        times = list(range(2, 2*(count+1), 2))
        timing = {**self.app.timing, 'times': times, 'origins': ['audio']*count}
        self.app.save_current_timing(timing)
        self.app.timing = timing
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.deiconify()
        self.root.update()
        with patch.object(self.app, 'manual_sync_baseline', return_value=(list(range(1, count+1)), ['audio']*count)):
            dialog.update_times()
        def assert_centered(index):
            start, end = dialog.rows.yview()
            self.assertLess(end-start, .7)
            self.assertLessEqual(abs((start+end)*count/2-index), 1)
        self.position(27.5)
        dialog.update_playhead()
        self.assertEqual(dialog.playhead, 26)
        self.assertEqual(dialog.rows.selection(), ('0',))
        self.assertEqual(self.app.displayed_index(), 12)
        dialog.rows.selection_set('40')
        self.root.update()
        assert_centered(40)
        self.assertEqual(self.app.displayed_index(), 12)
        self.position(20.5)
        dialog.update_playhead()
        assert_centered(40)
        self.assertEqual(dialog.playhead, 19)
        self.assertEqual(dialog.rows.selection(), ('40',))
        self.assertEqual(self.app.displayed_index(), 9)
        with patch.object(self.app.monitor, 'request_seek', return_value=True) as seek:
            dialog.step_row(1)
            seek.assert_called_once_with(42., self.playback.key)
            self.assertEqual(dialog.rows.selection(), ('41',))
            self.assertEqual(self.app.displayed_index(), 9)
            self.app.finish_seek(42., True, '')
        self.position(42.5)
        self.assertEqual(self.app.displayed_index(), 20)
        self.assertEqual(dialog.rows.selection(), ('41',))

    def test_double_click_edits_lyric_without_changing_saved_time_or_singer(self):
        self.receive(times=[None]*3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.deiconify()
        self.root.update()
        self.position(10)
        dialog.record()
        dialog.select_for_edit(0)
        self.app.singer_edits.save(0, ('Singer',))
        timing = self.app.live_lyrics.timing_store.load(self.app.song)
        singer_file = self.app.singer_edits.path
        singers = singer_file.read_bytes()
        x, y, _, _ = dialog.rows.bbox('0', 'lyric')
        for moment in (1000, 1100):
            dialog.rows.event_generate('<ButtonPress-1>', x=x+8, y=y+5, time=moment)
            dialog.rows.event_generate('<ButtonRelease-1>', x=x+8, y=y+5, time=moment+10)
        entry = dialog.lyric_entry
        self.assertIsNotNone(entry)
        entry.focus_force()
        self.root.update()
        with patch.object(self.app.monitor, 'request_seek') as seek:
            entry.event_generate('<Right>')
            entry.event_generate('<KeyRelease-e>')
            entry.event_generate('<KeyRelease-Down>')
            seek.assert_not_called()
        entry.delete(0, 'end')
        entry.insert(0, '青い空を見上げる')
        entry.event_generate('<KeyRelease-Return>')
        self.assertIs(dialog.lyric_entry, entry)
        self.assertFalse(self.app.lyric_text_edits.path.exists())
        entry.event_generate('<KeyRelease-Return>')
        self.assertIsNone(dialog.lyric_entry)
        self.assertEqual(dialog.rows.set('0', 'lyric'), '青い空を見上げる')
        self.assertEqual(self.app.original_label.cget('text'), '青い空を見上げる')
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song), timing)
        self.assertEqual(singer_file.read_bytes(), singers)
        self.app.lyric_text_edits.select(None)
        self.app.render()
        self.assertEqual(self.app.original_label.cget('text'), '青い空を見上げる')

    def test_translation_edit_applies_on_outside_click_and_survives_reload(self):
        self.receive(times=[None]*3, texts=('Hello world', '二行', '三行'))
        self.position(.5)
        self.app.lyric_text_edits.save(0, 'Hello again')
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.deiconify()
        self.root.update()

        def edit(moment):
            x, y, _, _ = dialog.rows.bbox('0', 'translation')
            for offset in (0, 100):
                dialog.rows.event_generate('<ButtonPress-1>', x=x+8, y=y+5, time=moment+offset)
                dialog.rows.event_generate('<ButtonRelease-1>', x=x+8, y=y+5, time=moment+offset+10)
            entry = dialog.lyric_entry
            entry.focus_force()
            self.root.update()
            return entry

        self.assertEqual(dialog.rows.set('0', 'translation'), '')
        entry = edit(1000)
        entry.insert(0, '다시 안녕')
        entry.event_generate('<KeyRelease-KP_Enter>')
        self.assertIs(dialog.lyric_entry, entry)
        self.assertEqual(dialog.rows.set('0', 'translation'), '')
        entry.insert('end', '!')
        entry.event_generate('<KeyRelease-Return>')
        self.assertIs(dialog.lyric_entry, entry)
        dialog.title.event_generate('<ButtonPress-1>', x=5, y=5)
        self.assertIsNone(dialog.lyric_entry)
        self.assertEqual(dialog.rows.set('0', 'translation'), '다시 안녕!')
        self.assertEqual(self.app.korean_label.cget('text'), '다시 안녕!')
        self.app.lyric_text_edits.select(None)
        self.app.render()
        self.assertEqual(self.app.display_lyric_translation(0), '다시 안녕!')
        self.assertEqual(self.app.corrected_lyric_text(0), 'Hello again')

        entry = edit(2000)
        entry.delete(0, 'end')
        dialog.rows.focus_force()
        self.root.update()
        self.assertIsNone(dialog.lyric_entry)
        self.assertEqual(self.app.lyric_text_edits.translation(0), '')
        self.assertEqual(self.app.korean_label.cget('text'), '')

    def test_reading_edit_overrides_automatic_reading_and_can_restore_it(self):
        self.receive(times=[None]*3)
        self.position(.5)
        edits = self.app.lyric_text_edits
        edits.save(0, '明日を描く')
        edits.save(0, '내일을 그려', field='korean')
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.geometry('680x470')
        dialog.window.deiconify()
        self.root.update()
        automatic = self.app.reading_label.cget('text')
        self.assertTrue(automatic)
        self.assertEqual(dialog.rows.set('0', 'reading'), automatic)
        before = edits.path.read_bytes()

        def edit(moment):
            x, y, _, _ = dialog.rows.bbox('0', 'reading')
            for offset in (0, 100):
                dialog.rows.event_generate('<ButtonPress-1>', x=x+8, y=y+5, time=moment+offset)
                dialog.rows.event_generate('<ButtonRelease-1>', x=x+8, y=y+5, time=moment+offset+10)
            entry = dialog.lyric_entry
            entry.focus_force()
            self.root.update()
            return entry

        entry = edit(1000)
        self.assertEqual(entry.get(), automatic)
        entry.delete(0, 'end')
        entry.insert(0, 'あすをえがく')
        entry.event_generate('<KeyRelease-Return>')
        self.assertEqual(edits.path.read_bytes(), before)
        entry.event_generate('<KeyRelease-Return>')
        self.assertIsNone(dialog.lyric_entry)
        self.assertEqual(dialog.rows.set('0', 'reading'), 'あすをえがく')
        self.assertEqual(self.app.reading_label.cget('text'), 'あすをえがく')
        edits.select(None)
        self.app.render()
        self.assertEqual(self.app.display_lyric_reading(0), 'あすをえがく')
        self.assertEqual(self.app.corrected_lyric_text(0), '明日を描く')
        self.assertEqual(self.app.display_lyric_translation(0), '내일을 그려')

        entry = edit(2000)
        entry.delete(0, 'end')
        dialog.title.event_generate('<ButtonPress-1>', x=5, y=5)
        self.assertIsNone(dialog.lyric_entry)
        self.assertIsNone(edits.reading(0))
        self.assertEqual(dialog.rows.set('0', 'reading'), automatic)
        self.assertEqual(self.app.reading_label.cget('text'), automatic)

    def test_cancel_and_track_change_cannot_save_a_different_lyric(self):
        self.receive(times=[None]*3)
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.deiconify()
        self.root.update()
        x, y, _, _ = dialog.rows.bbox('0', 'lyric')
        event = SimpleNamespace(x=x+8, y=y+5)
        dialog.remember_lyric_click(event)
        dialog.edit_lyric(event)
        entry = dialog.lyric_entry
        entry.focus_force()
        self.root.update()
        entry.event_generate('<Escape>')
        self.assertIsNone(dialog.lyric_entry)
        self.assertFalse(self.app.lyric_text_edits.path.exists())
        dialog.remember_lyric_click(event)
        dialog.edit_lyric(event)
        layout = self.app.lyric_text_edits.key
        self.app.set_playback(replace(self.playback, title='Different Song'))
        with patch.object(self.app.lyric_text_edits, 'save') as save:
            dialog.save_lyric_entry(layout)
            save.assert_not_called()
        self.assertIsNone(dialog.lyric_entry)

    def test_double_click_keeps_target_when_selection_recenters_table(self):
        count = 60
        self.receive(times=[None]*count, texts=tuple(f'行 {index}' for index in range(count)))
        self.position(.5)
        self.app.open_manual_sync()
        dialog = self.app.manual_sync_dialog
        dialog.window.attributes('-alpha', 0)
        dialog.window.deiconify()
        self.root.update()
        dialog.rows.yview_moveto(.4)
        self.root.update()
        x, y, _, _ = dialog.rows.bbox('24', 'lyric')
        dialog.rows.event_generate('<ButtonPress-1>', x=x+8, y=y+5, time=1000)
        dialog.rows.event_generate('<ButtonRelease-1>', x=x+8, y=y+5, time=1010)
        self.root.update()
        self.assertNotEqual(dialog.rows.identify_row(y+5), '24')
        dialog.rows.event_generate('<ButtonPress-1>', x=x+8, y=y+5, time=1100)
        self.assertEqual(dialog.lyric_entry.get(), '行 24')

    def test_ordinary_overlay_arrows_seek_and_qwer_is_reserved_for_developer_mode(self):
        self.receive()
        self.position(12)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.root.focus_force()
        self.root.update()
        with patch.object(self.app.monitor, 'request_seek', return_value=True) as seek:
            self.root.event_generate('<Right>')
            self.assertEqual(self.app.displayed_index(), 0)
            self.assertTrue(self.app.follow.get())
            seek.assert_called_once_with(20., self.playback.key)
            self.app.finish_seek(20., True, '')
            self.position(20)
            self.assertEqual(self.app.displayed_index(), 1)
            self.root.event_generate('<KeyRelease-r>')
            seek.assert_called_once()

    def test_developer_mode_down_and_record_button_stamp_without_seeking(self):
        self.receive(times=[None] * 3)
        self.app.open_developer_mode()
        dialog=self.app.manual_sync_dialog
        self.app.toggle_singer_input()
        self.assertEqual(self.app.singer_input_index, 0)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.root.focus_force()
        self.root.update()
        with (
            patch.object(self.app.monitor, 'request_seek') as seek,
            patch.object(self.app, 'sync_capture_position', side_effect=(20.123, 31.456)),
        ):
            self.position(20, playing=True)
            self.root.event_generate('<KeyRelease-Down>', when='now')
            self.assertEqual(dialog.selected_index(), 1)
            self.assertFalse(self.app.singer_input_live)
            self.assertTrue(self.app.playback.playing)
            self.position(31, playing=True)
            dialog.record_next()
            self.assertEqual(dialog.selected_index(), 2)
            seek.assert_not_called()
        self.assertEqual(self.app.live_lyrics.timing_store.load(self.app.song)['times'], [None, 20.123, 31.456])
        self.assertTrue(self.app.follow.get())
        self.assertIsNone(self.app.manual_index)
