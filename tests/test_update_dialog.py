from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import update_dialog
from update_dialog import UpdateController, UpdateDialog


class FakeRoot:
    def __init__(self):
        self.callbacks = {}
        self.serial = 0

    def after(self, delay, callback):
        self.serial += 1
        key = str(self.serial)
        self.callbacks[key] = (delay, callback)
        return key

    def after_cancel(self, key):
        self.callbacks.pop(key, None)


class UpdateControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = SimpleNamespace(root=FakeRoot(), store=SimpleNamespace(directory=Path(self.temp.name)),
                                   services_started=True, update_dialog=None, notify=Mock(), close=Mock())
        self.controller = UpdateController(self.app)
        self.app.update_controller = self.controller
        self.addCleanup(self.controller.close)
        self.release = SimpleNamespace(version='9.0.0', tag='v9.0.0', body='Changes', html_url='https://github.com/',
            assets={update_dialog.app_updates.EXE_ASSET_NAME: {}, update_dialog.app_updates.DATA_ASSET_NAME: {}})

    def finish(self):
        deadline = time.monotonic() + 3
        while self.controller.busy and time.monotonic() < deadline:
            time.sleep(.005)
            self.controller.poll()
        self.assertFalse(self.controller.busy, 'update worker did not finish')

    def test_check_does_not_block_or_download_and_records_quiet_check(self):
        waiting = threading.Event()
        self.addCleanup(waiting.set)
        with patch.object(update_dialog.app_updates, 'check_release',
                          side_effect=lambda:(waiting.wait(2), self.release)[1]), \
             patch.object(update_dialog.app_updates, 'download_asset') as download:
            self.assertTrue(self.controller.check(quiet=True))
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.controller.check())
            waiting.set()
            self.finish()
        download.assert_not_called()
        self.assertEqual(self.controller.release, self.release)
        self.assertTrue(self.controller.receipt.exists())
        self.app.notify.assert_called_once()

    def test_automatic_check_is_frozen_services_only_and_once_per_day(self):
        with patch.object(update_dialog, 'frozen', return_value=False):
            self.controller.schedule_automatic()
        self.assertIsNone(self.controller.auto_after)
        with patch.object(update_dialog, 'frozen', return_value=True):
            self.app.services_started = False
            self.controller.schedule_automatic()
            self.assertIsNone(self.controller.auto_after)
            self.app.services_started = True
            update_dialog.atomic_json(self.controller.receipt, {'checked_at': time.time()})
            self.controller.schedule_automatic()
            self.assertIsNone(self.controller.auto_after)
            update_dialog.atomic_json(self.controller.receipt, {'checked_at': time.time() - 86401})
            self.controller.schedule_automatic()
            self.assertIsNotNone(self.controller.auto_after)

    def test_data_stages_without_program_install_or_app_close(self):
        self.controller.release = self.release
        archive = Path(self.temp.name) / 'data.zip'
        with patch.object(update_dialog.app_updates, 'download_asset', return_value=archive) as download, \
             patch.object(update_dialog, 'stage_package', return_value={'status': 'staged', 'files': 3}) as stage, \
             patch.object(update_dialog.app_updates, 'prepare_executable_update') as install:
            self.assertTrue(self.controller.download('data'))
            self.finish()
        self.assertEqual(download.call_args.args[1], update_dialog.app_updates.DATA_ASSET_NAME)
        stage.assert_called_once_with(archive, self.app.store.directory)
        install.assert_not_called()
        self.app.close.assert_not_called()
        self.assertTrue(self.controller.data_staged)

    def test_corrupt_optional_check_receipt_cannot_prevent_startup(self):
        self.controller.receipt.write_text('{broken', encoding='utf-8')
        with patch.object(update_dialog, 'frozen', return_value=True):
            self.controller.schedule_automatic()
        self.assertIsNotNone(self.controller.auto_after)

    def test_download_failure_never_stages_data(self):
        self.controller.release = self.release
        with patch.object(update_dialog.app_updates, 'download_asset', side_effect=ValueError('Checksum mismatch')), \
             patch.object(update_dialog, 'stage_package') as stage:
            self.assertTrue(self.controller.download('data'))
            self.finish()
        stage.assert_not_called()
        self.assertFalse(self.controller.data_staged)
        self.app.close.assert_not_called()

    def test_program_restart_uses_verified_release_even_after_recheck(self):
        self.controller.release = self.release
        program = Path(self.temp.name) / 'app.exe'
        with patch.object(update_dialog, 'frozen', return_value=True), \
             patch.object(update_dialog.app_updates, 'download_asset', return_value=program):
            self.assertTrue(self.controller.download('program'))
            self.finish()
        original = self.release
        self.controller.release = SimpleNamespace(version='10.0.0')
        with patch.object(update_dialog.messagebox, 'askyesno', return_value=False), \
             patch.object(update_dialog.app_updates, 'prepare_executable_update') as install:
            self.assertFalse(self.controller.restart())
            install.assert_not_called()
        with patch.object(update_dialog.messagebox, 'askyesno', return_value=True), \
             patch.object(update_dialog.app_updates, 'prepare_executable_update') as install:
            self.assertTrue(self.controller.restart())
            self.finish()
        install.assert_called_once_with(program, original, self.app.store.directory)
        self.app.close.assert_called_once()

    def test_helper_failure_keeps_app_running(self):
        self.controller.data_staged = True
        with patch.object(update_dialog.messagebox, 'askyesno', return_value=True), \
             patch.object(update_dialog.app_updates, 'restart_app', side_effect=OSError('Cannot restart')):
            self.assertTrue(self.controller.restart())
            self.finish()
        self.app.close.assert_not_called()
        self.assertIn('Cannot restart', self.controller.status)

    def test_closed_controller_ignores_late_restart_and_cancels_callbacks(self):
        self.controller.messages.put(('done', ('restart', None), False))
        self.controller.close()
        self.controller.poll()
        self.app.close.assert_not_called()
        self.assertFalse(self.app.root.callbacks)


class UpdateDialogTests(unittest.TestCase):
    def test_dialog_shows_version_and_independent_actions_without_automatic_download(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = SimpleNamespace(root=root, store=SimpleNamespace(directory=Path(folder)),
                                      update_dialog=None, services_started=False, notify=Mock(), close=Mock())
                app.update_controller = controller = UpdateController(app)
                controller.checked = True
                controller.release = SimpleNamespace(version=update_dialog.VERSION, body='Updated lyrics',
                    html_url='https://github.com/', assets={update_dialog.app_updates.DATA_ASSET_NAME: {}})
                with patch.object(update_dialog.app_updates, 'download_asset') as download:
                    app.update_dialog = dialog = UpdateDialog(app)
                    root.update()
                    self.assertIn(update_dialog.VERSION, dialog.version.cget('text'))
                    self.assertEqual(str(dialog.program_button.cget('state')), 'disabled')
                    self.assertEqual(str(dialog.data_button.cget('state')), 'normal')
                    self.assertIn('Updated lyrics', dialog.notes.get('1.0', 'end'))
                    download.assert_not_called()
                    dialog.close()
                    self.assertIsNone(app.update_dialog)
                controller.close()
        finally:
            root.destroy()


class PendingUpdateStartupTests(unittest.TestCase):
    def test_second_instance_does_not_apply_pending_data(self):
        import app
        with tempfile.TemporaryDirectory() as folder:
            lock = Mock()
            lock.__enter__ = Mock(return_value=SimpleNamespace(acquired=False))
            lock.__exit__ = Mock(return_value=False)
            with patch('sys.argv', ['app.py', '--data-dir', folder, '--spotify-auto-start']), \
                 patch.object(app, 'SingleInstance', return_value=lock), \
                 patch('data_updates.apply_pending') as apply, patch.object(app, 'run') as run:
                app.main()
            apply.assert_not_called()
            run.assert_not_called()

    def test_pending_data_applies_after_lock_before_seed(self):
        import app
        with tempfile.TemporaryDirectory() as folder:
            order = []
            lock = Mock()
            lock.__enter__ = Mock(side_effect=lambda:(order.append('lock'), SimpleNamespace(acquired=True))[1])
            lock.__exit__ = Mock(return_value=False)
            with patch('sys.argv', ['app.py', '--data-dir', str(Path(folder)/'fresh'), '--no-media']), \
                 patch.object(app, 'SingleInstance', return_value=lock), \
                 patch('data_updates.apply_pending', side_effect=lambda _:order.append('apply')), \
                 patch.object(app, 'seed_data', side_effect=lambda _:order.append('seed')), \
                 patch.object(app, 'clear_spotify_exit'), \
                 patch.object(app, 'run', side_effect=lambda _:order.append('run')):
                app.main()
            self.assertEqual(order, ['lock', 'apply', 'seed', 'run'])

    def test_pending_data_failure_still_starts_app_with_visible_error(self):
        import app
        with tempfile.TemporaryDirectory() as folder:
            lock = Mock()
            lock.__enter__ = Mock(return_value=SimpleNamespace(acquired=True))
            lock.__exit__ = Mock(return_value=False)
            with patch('sys.argv', ['app.py', '--data-dir', folder, '--no-media']), \
                 patch.object(app, 'SingleInstance', return_value=lock), \
                 patch('data_updates.apply_pending', side_effect=ValueError('Invalid data package')), \
                 patch.object(app, 'clear_spotify_exit'), patch.object(app, 'run') as run:
                app.main()
            self.assertEqual(run.call_args.args[0].data_update_error, 'Invalid data package')


if __name__ == '__main__':
    unittest.main()
