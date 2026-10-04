"""Exercise batch navigation and clipboard intake without changing user data."""
import tkinter as tk
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from batch_translation_dialog import BatchTranslationDialog
from lyrics import Line


class BatchTranslationDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.documents = [dict(id=str(i), title=f'노래 {i}', artist='가수', album='', lines=(Line(f'原文 {i}', '', ''), Line('翻訳済み', '', '이미 번역'))) for i in range(7)]
        self.backend = SimpleNamespace(prepare=Mock(side_effect=self.make_batches), is_reply=Mock(side_effect=lambda text: text.startswith('REPLY:')),
                                       summary=Mock(return_value={'total_songs':10,'total_lines':524}))
        self.app = SimpleNamespace(root=self.root, translation_dialog=None, batch_translations=self.backend,
                                   collect_translation_documents=Mock(return_value=self.documents),
                                   apply_translation_reply=Mock(return_value={'batch_id': 'batch-5-0', 'songs': 5, 'lines': 5, 'remaining': 0,
                                       'received_songs':5,'received_lines':5,'already_lines':0,'preserved_lines':0,
                                       'total_songs':15,'total_lines':529}),
                                   button=lambda parent, label, action: tk.Button(parent, text=label, command=action),
                                   hotkeys=SimpleNamespace(suspended=False, register_window=Mock(), suspend=Mock()))
        self.clipboard_patch = patch.object(BatchTranslationDialog, 'read_clipboard', return_value='REPLY: copied before opening')
        self.clipboard = self.clipboard_patch.start()
        self.dialog = BatchTranslationDialog(self.app)
        self.app.translation_dialog = self.dialog
        self.wait_prepared()

    def tearDown(self):
        self.dialog.close()
        self.root.destroy()
        self.clipboard_patch.stop()

    @staticmethod
    def make_batches(documents, songs_per_batch):
        return [{'batch_id': f'batch-{songs_per_batch}-{i}', 'prompt': f'翻訳 지시문 {i}: ' + ', '.join(song['title'] for song in documents[i:i + songs_per_batch]), 'songs': documents[i:i + songs_per_batch]}
                for i in range(0, len(documents), songs_per_batch)]

    def poll(self):
        self.dialog.window.after_cancel(self.dialog._poll_id)
        self.dialog.poll_clipboard()

    def wait_prepared(self):
        deadline = time.monotonic() + 5
        while self.dialog.preparing and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.dialog.preparing, 'Background preparation did not finish')
        self.root.update()

    def test_default_groups_show_only_missing_counts_and_keep_main_hotkeys_active(self):
        self.backend.prepare.assert_called_once_with(self.documents, songs_per_batch=5)
        self.assertEqual(len(self.dialog.batches), 2)
        self.assertIn(f'{len(self.dialog.batches[0]["prompt"]):,}자', self.dialog.batch_choice.get())
        self.assertIn('7곡 · 7구절', self.dialog.status.get())
        self.assertEqual(len(self.dialog.songs.get_children()), 5)
        self.assertEqual(self.dialog.songs.item(self.dialog.songs.get_children()[0], 'values'), ('노래 0', '가수', '1'))
        self.assertEqual(str(self.dialog.prompt.cget('state')), 'disabled')
        self.assertIsNone(self.root.grab_current())
        self.assertIn('10곡 · 524구절', self.dialog.saved_status.get())
        self.app.hotkeys.register_window.assert_not_called()
        self.app.hotkeys.suspend.assert_not_called()

    def test_batch_size_regroups_and_invalid_size_preserves_requests(self):
        self.dialog.batch_size.set('2')
        self.dialog.prepare_button.invoke()
        self.wait_prepared()
        self.assertEqual(len(self.dialog.batches), 4)
        previous = self.dialog.batches
        for value in ('0', '51', 'abc', '1.5'):
            self.dialog.batch_size.set(value)
            self.dialog.prepare_button.invoke()
            self.assertIs(self.dialog.batches, previous)
            self.assertIn('1부터 50', self.dialog.status.get())
        self.assertEqual(self.backend.prepare.call_count, 2)

    def test_copy_prompt_and_navigation_preserve_complete_text(self):
        self.dialog.next_button.invoke()
        prompt = self.dialog.batches[1]['prompt']
        self.assertEqual(self.dialog.prompt.get('1.0', 'end-1c'), prompt)
        with patch.object(self.dialog.window, 'clipboard_clear') as clear, patch.object(self.dialog.window, 'clipboard_append') as append:
            self.dialog.copy_button.invoke()
        clear.assert_called_once_with()
        append.assert_called_once_with(prompt)
        self.assertEqual(self.dialog._clipboard_seen, prompt)
        self.assertEqual(str(self.dialog.next_button.cget('state')), 'disabled')
        self.dialog.previous_button.invoke()
        self.assertEqual(self.dialog.batch_choice.current(), 0)

    def test_automatic_intake_skips_old_unrelated_and_request_text(self):
        self.poll()
        self.app.apply_translation_reply.assert_not_called()
        for text in ('unrelated clipboard text', self.dialog.batches[0]['prompt']):
            self.clipboard.return_value = text
            self.poll()
        self.app.apply_translation_reply.assert_not_called()
        self.clipboard.return_value = 'REPLY: new answer'
        prompt = self.dialog.prompt.get('1.0', 'end-1c')
        self.poll()
        self.poll()
        self.app.apply_translation_reply.assert_called_once_with('REPLY: new answer')
        self.assertEqual(self.dialog.batch_choice.current(), 0)
        self.assertIn('완료', self.dialog.batch_choice.get())
        self.assertEqual(self.dialog.prompt.get('1.0', 'end-1c'), prompt)
        self.assertIn('자동 적용 · 새 번역 저장 완료 · 5곡 · 5구절', self.dialog.result_status.get())
        self.assertIn('15곡 · 529구절', self.dialog.saved_status.get())

    def test_disabled_auto_and_reenable_do_not_import_stale_clipboard(self):
        self.dialog.auto_button.invoke()
        self.clipboard.return_value = 'REPLY: while disabled'
        self.poll()
        self.dialog.auto_button.invoke()
        self.poll()
        self.app.apply_translation_reply.assert_not_called()
        self.clipboard.return_value = 'REPLY: after enabling'
        self.poll()
        self.app.apply_translation_reply.assert_called_once_with('REPLY: after enabling')

    def test_automatic_error_is_visible_and_editable_for_manual_retry(self):
        self.app.apply_translation_reply.side_effect = ValueError('구절 수를 확인하세요.')
        self.clipboard.return_value = 'REPLY: incomplete'
        self.poll()
        self.assertIn('구절 수를 확인하세요.', self.dialog.status.get())
        self.assertIn('이번 답변은 저장하지 않았습니다',self.dialog.result_status.get())
        self.assertIn('10곡 · 524구절',self.dialog.saved_status.get())
        self.assertEqual(self.dialog.reply.get('1.0', 'end-1c'), 'REPLY: incomplete')
        self.poll()
        self.assertEqual(self.app.apply_translation_reply.call_count, 1)
        self.app.apply_translation_reply.side_effect = None
        self.dialog.reply.delete('1.0', 'end')
        self.dialog.reply.insert('1.0', 'REPLY: repaired')
        self.dialog.buttons[2].invoke()
        self.app.apply_translation_reply.assert_called_with('REPLY: repaired')
        self.assertIn('새 번역 저장 완료', self.dialog.result_status.get())

    def test_repeat_manual_apply_reports_already_received_instead_of_zero_saved(self):
        self.clipboard.return_value='REPLY: saved automatically'
        self.poll()
        self.assertIn('자동 적용 · 새 번역 저장 완료',self.dialog.result_status.get())
        result=self.app.apply_translation_reply.return_value
        self.app.apply_translation_reply.return_value={**result,'songs':0,'lines':0,'already_lines':5}
        self.dialog.paste_reply();self.dialog.apply_manual()
        self.assertIn('직접 적용 · 이미 반영된 답변 · 5곡 · 5구절 확인',self.dialog.result_status.get())
        self.assertNotIn('0곡',self.dialog.result_status.get())
        self.assertIn('15곡 · 529구절',self.dialog.saved_status.get())

    def test_existing_different_translation_is_reported_as_preserved(self):
        result=self.app.apply_translation_reply.return_value
        self.app.apply_translation_reply.return_value={**result,'songs':0,'lines':0,'preserved_lines':5}
        self.dialog.apply_reply('REPLY: other translations')
        self.assertIn('기존 번역 유지 · 5곡 · 5구절 확인',self.dialog.result_status.get())
        self.assertNotIn('이미 반영된 답변',self.dialog.result_status.get())

    def test_result_and_total_survive_copy_navigation_and_regrouping(self):
        self.dialog.apply_reply('REPLY: saved')
        result=self.dialog.result_status.get();saved=self.dialog.saved_status.get()
        self.dialog.move(1)
        with patch.object(self.dialog.window,'clipboard_clear'),patch.object(self.dialog.window,'clipboard_append'):
            self.dialog.copy_prompt()
        self.dialog.prepare();self.wait_prepared()
        self.assertEqual(self.dialog.result_status.get(),result)
        self.assertEqual(self.dialog.saved_status.get(),saved)

    def test_reopened_dialog_shows_persisted_total_before_any_new_answer(self):
        self.dialog.close()
        self.backend.summary.return_value={'total_songs':15,'total_lines':529}
        self.dialog=BatchTranslationDialog(self.app);self.app.translation_dialog=self.dialog
        self.wait_prepared()
        self.assertIn('15곡 · 529구절',self.dialog.saved_status.get())
        self.assertIn('아직 이 창에서',self.dialog.result_status.get())

    def test_manual_paste_can_apply_reply_that_predates_opening(self):
        self.dialog.buttons[1].invoke()
        self.assertEqual(self.dialog.notebook.select(), str(self.dialog.reply_frame))
        self.assertEqual(self.dialog.reply.get('1.0', 'end-1c'), 'REPLY: copied before opening')
        self.dialog.buttons[2].invoke()
        self.app.apply_translation_reply.assert_called_once_with('REPLY: copied before opening')

    def test_empty_documents_disable_request_controls(self):
        self.app.collect_translation_documents.return_value = []
        self.dialog.prepare()
        self.wait_prepared()
        self.assertEqual(self.dialog.batches, [])
        self.assertEqual(str(self.dialog.copy_button.cget('state')), 'disabled')
        self.assertEqual(str(self.dialog.previous_button.cget('state')), 'disabled')
        self.assertEqual(str(self.dialog.next_button.cget('state')), 'disabled')
        self.assertIn('찾지 못했습니다', self.dialog.status.get())

    def test_all_actions_are_visible_at_minimum_window_size(self):
        self.dialog.apply_reply('REPLY: complete')
        self.dialog.window.geometry('640x600+20+20')
        self.root.update()
        window = self.dialog.window
        for widget in [self.dialog.prepare_button, self.dialog.size_input, self.dialog.previous_button,
                       self.dialog.next_button, self.dialog.auto_button, self.dialog.status_label,
                       self.dialog.result_label,self.dialog.saved_label,*self.dialog.buttons]:
            self.assertTrue(widget.winfo_viewable())
            self.assertGreaterEqual(widget.winfo_rootx(), window.winfo_rootx())
            self.assertGreaterEqual(widget.winfo_rooty(), window.winfo_rooty())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), window.winfo_rootx() + window.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), window.winfo_rooty() + window.winfo_height())
        self.assertGreater(self.dialog.prompt.winfo_height(), 70)

    def test_text_tab_moves_keyboard_focus_instead_of_inserting_a_tab(self):
        self.dialog.notebook.select(self.dialog.reply_frame)
        self.dialog.reply.focus_force()
        self.root.update()
        self.dialog.reply.event_generate('<Tab>')
        self.root.update()
        self.assertEqual(self.dialog.reply.get('1.0', 'end-1c'), '')
        self.assertIsNot(self.root.focus_get(), self.dialog.reply)

    def test_destroy_cancels_clipboard_poll_and_clears_app_reference(self):
        timer = self.dialog._poll_id
        self.dialog.window.destroy()
        self.assertTrue(self.dialog.closed)
        self.assertIsNone(self.app.translation_dialog)
        self.assertIsNone(self.dialog._poll_id)
        self.assertNotIn(timer, self.root.tk.call('after', 'info'))

    def test_background_prepare_keeps_ui_responsive_and_delays_clipboard_intake(self):
        release = threading.Event()
        owner = threading.get_ident()
        worker_threads = []

        def collect():
            worker_threads.append(threading.get_ident())
            release.wait(5)
            return self.documents

        self.app.collect_translation_documents.side_effect = collect
        self.dialog.prepare()
        try:
            self.assertTrue(self.dialog.preparing)
            self.assertEqual(str(self.dialog.prepare_button.cget('state')), 'disabled')
            self.assertEqual(str(self.dialog.size_input.cget('state')), 'disabled')
            heartbeat = Mock()
            self.root.after(0, heartbeat)
            self.root.update()
            heartbeat.assert_called_once_with()
            self.clipboard.return_value = 'REPLY: received during preparation'
            previous = self.dialog._clipboard_seen
            self.poll()
            self.assertEqual(self.dialog._clipboard_seen, previous)
            self.app.apply_translation_reply.assert_not_called()
            self.dialog.reply.insert('1.0', 'REPLY: manual while busy')
            self.dialog.apply_manual()
            self.app.apply_translation_reply.assert_not_called()
            self.assertIn('완료된 뒤', self.dialog.status.get())
        finally:
            release.set()
        self.wait_prepared()
        self.assertTrue(worker_threads)
        self.assertNotIn(owner, worker_threads)
        self.poll()
        self.app.apply_translation_reply.assert_called_once_with('REPLY: received during preparation')
        self.assertEqual(str(self.dialog.prepare_button.cget('state')), 'normal')
        self.assertEqual(str(self.dialog.size_input.cget('state')), 'normal')

    def test_background_error_preserves_previous_requests_and_restores_controls(self):
        previous = self.dialog.batches
        self.app.collect_translation_documents.side_effect = OSError('보관 파일 읽기 실패')
        self.dialog.prepare()
        self.wait_prepared()
        self.assertIs(self.dialog.batches, previous)
        self.assertIn('보관 파일 읽기 실패', self.dialog.status.get())
        self.assertEqual(str(self.dialog.prepare_button.cget('state')), 'normal')
        self.assertEqual(str(self.dialog.size_input.cget('state')), 'normal')

    def test_close_during_background_work_discards_result_and_cancels_both_timers(self):
        release = threading.Event()
        self.app.collect_translation_documents.side_effect = lambda: (release.wait(5), self.documents)[1]
        self.dialog.prepare()
        timers = (self.dialog._prepare_poll_id, self.dialog._poll_id)
        worker = self.dialog._prepare_thread
        try:
            self.dialog.close()
            self.assertIsNone(self.app.translation_dialog)
            for timer in timers:
                self.assertNotIn(timer, self.root.tk.call('after', 'info'))
            self.assertIsNone(self.dialog._prepare_poll_id)
            self.assertIsNone(self.dialog._poll_id)
        finally:
            release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.root.update()


if __name__ == '__main__':
    unittest.main()
