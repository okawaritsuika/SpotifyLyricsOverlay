"""Verify personal resource categories and the reversible include control."""
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from translation_resource_dialog import TranslationResourceDialog


class TranslationResourceDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.resources = [
            dict(id='song-a', title='輝き', artists=['Singer A'], album='First',
                 source='Spotify', total=2, translated=1, original=2,
                 manual_sync=0, performers=0, included=True),
            dict(id='song-b', title='The Forest', artists=['Singer B'], album='Second',
                 source='Spotify', total=1, translated=0, original=1,
                 manual_sync=0, performers=0, included=True),
            dict(id='song-c', title='수동 기록', artists=['Singer C'], album='Third',
                 source='직접 입력', total=0, translated=0, original=0,
                 manual_sync=2, performers=2, included=None, roster=['Singer C', 'Singer D'], roster_saved=True),
        ]
        self.rows = {
            'song-a': (
                dict(original='窓を開ける', reading='마도오 아케루', korean='창문을 연다', removable=['korean']),
                dict(original='光が差す'),
            ),
            'song-c': (
                dict(index=1, time=0.0, performers=['Singer C'], removable=['timing','singers']),
                dict(index=4, time=61.005, removable=['timing']),
            ),
        }
        self.app = SimpleNamespace(
            root=self.root, translation_resource_dialog=None,
            button=lambda parent, label, action: tk.Button(parent, text=label, command=action),
            list_translation_resources=Mock(side_effect=lambda: [item.copy() for item in self.resources]),
            translation_resource_details=Mock(side_effect=lambda ident: self.rows.get(ident, ())),
            translation_resource_actions=Mock(return_value={'original','reading','korean','timing','singers','roster'}),
            update_translation_resource=Mock(),
            set_translation_resource_included=Mock(side_effect=self.set_included),
        )
        self.dialog = TranslationResourceDialog(self.app)
        self.app.translation_resource_dialog = self.dialog
        self.wait_loaded()

    def tearDown(self):
        self.dialog.close()
        self.root.destroy()

    def set_included(self, ident, included):
        item = next(item for item in self.resources if item['id'] == ident)
        item['included'] = included

    def wait_loaded(self):
        deadline = time.monotonic() + 4
        while self.dialog.loading and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.dialog.loading)
        self.root.update()

    def select(self, ident):
        self.dialog.songs.selection_set(ident)
        self.root.update()

    def test_filter_and_preview_show_exact_saved_rows(self):
        self.assertEqual(self.dialog.window.title(), '내 자료 관리')
        self.assertEqual(self.dialog.songs.get_children(), ('song-a', 'song-b', 'song-c'))
        self.dialog.query.set('singer first')
        self.assertEqual(self.dialog.songs.get_children(), ('song-a',))
        song = self.dialog.songs.item('song-a', 'values')
        self.assertEqual(song[3:], ('번역 1 · 원문 2', 'Spotify', '포함'))
        self.select('song-a')
        self.app.translation_resource_details.assert_called_with('song-a')
        preview = self.dialog.preview
        self.assertEqual([preview.item(iid, 'values') for iid in preview.get_children()],
                         [('1', '窓を開ける', '마도오 아케루', '창문을 연다', '', ''),
                          ('2', '光が差す', '', '', '', '')])
        self.dialog.query.set('second')
        self.root.update()
        self.assertEqual(self.dialog.songs.get_children(), ('song-b',))
        self.assertEqual(preview.get_children(), ())
        self.assertEqual(self.dialog.inclusion_button.winfo_manager(), '')

    def test_category_filter_supports_manual_times_and_singers_without_lyrics(self):
        results = [('song-c',), ('song-a',), ('song-a', 'song-b'), ('song-c',)]
        for index, expected in enumerate(results, 1):
            self.dialog.category.current(index)
            self.dialog.category.event_generate('<<ComboboxSelected>>')
            self.root.update()
            self.assertEqual(self.dialog.songs.get_children(), expected)
        self.select('song-c')
        song = self.dialog.songs.item('song-c', 'values')
        self.assertEqual(song[3:], ('수동 싱크 2 · 가창자 2', '직접 입력', ''))
        self.assertIn('참여 가창자  Singer C, Singer D', self.dialog.selection_label.get())
        preview = self.dialog.preview
        self.assertEqual([preview.item(iid, 'values') for iid in preview.get_children()],
                         [('1', '', '', '', '00:00.000', 'Singer C'),
                          ('4', '', '', '', '01:01.005', '')])
        self.assertEqual(self.dialog.inclusion_button.winfo_manager(), '')
        self.dialog.toggle_inclusion()
        self.app.set_translation_resource_included.assert_not_called()

    def test_exclude_and_reinclude_preserve_selection_and_preview(self):
        self.select('song-a')
        self.dialog.inclusion_button.invoke()
        self.wait_loaded()
        self.app.set_translation_resource_included.assert_called_with('song-a', False)
        self.assertEqual(self.dialog.songs.selection(), ('song-a',))
        self.assertEqual(self.dialog.songs.item('song-a', 'values')[-1], '제외')
        self.assertEqual(len(self.dialog.preview.get_children()), 2)
        self.assertEqual(self.dialog.inclusion_button['text'], '번역 요청에 다시 포함')
        self.dialog.inclusion_button.invoke()
        self.wait_loaded()
        self.app.set_translation_resource_included.assert_called_with('song-a', True)
        self.assertEqual(self.dialog.songs.item('song-a', 'values')[-1], '포함')

    def test_failed_refresh_keeps_visible_list_and_close_cancels_poll(self):
        self.app.list_translation_resources.side_effect = OSError('자료를 읽을 수 없습니다.')
        self.dialog.refresh()
        self.wait_loaded()
        self.assertIn('자료를 읽을 수 없습니다.', self.dialog.status.get())
        self.assertEqual(self.dialog.songs.get_children(), ('song-a', 'song-b', 'song-c'))
        self.dialog.refresh()
        pending = self.dialog.poll_id
        self.assertIsNotNone(pending)
        self.dialog.close()
        self.assertIsNone(self.dialog.poll_id)
        self.assertIsNone(self.app.translation_resource_dialog)
        self.assertNotIn(pending, self.root.tk.call('after', 'info'))

    @staticmethod
    def editor_child(widget, predicate):
        for child in widget.winfo_children():
            if predicate(child):
                return child
            nested = TranslationResourceDialogTests.editor_child(child, predicate)
            if nested:
                return nested

    def test_edit_and_clear_use_stable_line_index_and_preserve_selection(self):
        self.select('song-c')
        self.dialog.preview.selection_set('4')
        self.dialog.field.set('수동 싱크')
        self.root.update()
        self.dialog.edit_button.invoke()
        editor = self.dialog.editor
        self.assertIsNotNone(editor)
        entry = self.editor_child(editor, lambda child: isinstance(child, tk.Entry))
        entry.delete(0, 'end')
        entry.insert(0, '62.125')
        save = self.editor_child(editor, lambda child: isinstance(child, tk.Button) and child['text'] == '저장')
        save.invoke()
        self.wait_loaded()
        self.app.update_translation_resource.assert_called_once_with('song-c', 3, 'timing', '62.125')
        self.assertEqual(self.dialog.preview.selection(), ('4',))
        self.assertIn('저장 완료', self.dialog.status.get())
        with patch('translation_resource_dialog.messagebox.askyesno', return_value=False):
            self.dialog.clear_button.invoke()
        self.assertEqual(self.app.update_translation_resource.call_count, 1)
        with patch('translation_resource_dialog.messagebox.askyesno', return_value=True):
            self.dialog.clear_button.invoke()
        self.wait_loaded()
        self.app.update_translation_resource.assert_called_with('song-c', 3, 'timing', None)

    def test_roster_input_parses_names_and_can_clear_explicit_roster(self):
        self.select('song-c')
        self.dialog.roster_button.invoke()
        editor = self.dialog.editor
        entry = self.editor_child(editor, lambda child: isinstance(child, tk.Text))
        entry.delete('1.0', 'end')
        entry.insert('1.0', 'Singer C\n  Singer E\n\n')
        save = self.editor_child(editor, lambda child: isinstance(child, tk.Button) and child['text'] == '저장')
        save.invoke()
        self.wait_loaded()
        self.app.update_translation_resource.assert_called_once_with('song-c', None, 'roster', ['Singer C','Singer E'])
        self.dialog.roster_button.invoke()
        editor = self.dialog.editor
        clear = self.editor_child(editor, lambda child: isinstance(child, tk.Button) and child['text'] == '직접 저장값 지우기')
        with patch('translation_resource_dialog.messagebox.askyesno', return_value=True):
            clear.invoke()
        self.wait_loaded()
        self.app.update_translation_resource.assert_called_with('song-c', None, 'roster', None)


if __name__ == '__main__':
    unittest.main()
