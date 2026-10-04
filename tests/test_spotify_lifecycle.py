from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import spotify_lifecycle as lifecycle


class SpotifySessionTests(unittest.TestCase):
    def test_only_process_exit_after_grace_stops_session(self):
        session = lifecycle.SpotifySession()
        self.assertEqual(session.observe(False, 0), '')
        self.assertEqual(session.observe(True, 2), 'started')
        self.assertEqual(session.observe(True, 4), '')
        self.assertEqual(session.observe(False, 6), '')
        self.assertEqual(session.observe(False, 8), '')
        self.assertEqual(session.observe(False, 10), 'stopped')
        self.assertEqual(session.observe(False, 12), '')
        self.assertEqual(session.observe(True, 14), 'started')

    def test_brief_disappearance_does_not_stop_or_start_again(self):
        session = lifecycle.SpotifySession()
        self.assertEqual(session.observe(True, 0), 'started')
        self.assertEqual(session.observe(False, 2), '')
        self.assertEqual(session.observe(True, 4), '')
        self.assertEqual(session.observe(False, 6), '')
        self.assertEqual(session.observe(False, 8), '')

    def test_failed_scan_does_not_count_towards_exit(self):
        session = lifecycle.SpotifySession()
        session.observe(True, 0)
        session.observe(False, 2)
        session.unknown()
        self.assertEqual(session.observe(False, 10), '')
        self.assertEqual(session.observe(False, 12), '')
        self.assertEqual(session.observe(False, 14), 'stopped')


class LifecycleMonitorTests(unittest.TestCase):
    def setUp(self):
        self.messages = queue.Queue()
        self.monitor = lifecycle.SpotifyLifecycleMonitor(self.messages, enabled=True)
        self.now = 0
        self.scan = self.start_patch(patch.object(lifecycle, 'spotify_running'))
        self.start_patch(patch.object(lifecycle.time, 'monotonic', side_effect=lambda: self.now))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def poll(self, running, at):
        self.scan.return_value = running
        self.now = at
        self.monitor.poll()

    def test_manual_launch_without_spotify_stays_open_until_observed_exit(self):
        self.poll(False, 0)
        self.poll(False, 20)
        self.assertTrue(self.messages.empty())
        self.poll(True, 22)
        self.poll(False, 24)
        self.poll(False, 28)
        self.assertEqual(self.messages.get_nowait(), ('spotify_exit', None))
        self.poll(False, 30)
        self.assertTrue(self.messages.empty())

    def test_disable_and_reenable_does_not_reuse_old_session(self):
        self.poll(True, 0)
        self.monitor.configure(False)
        self.poll(False, 20)
        self.monitor.configure(True)
        self.poll(False, 22)
        self.poll(False, 40)
        self.assertTrue(self.messages.empty())

    def test_auto_launch_closes_even_if_spotify_exits_before_first_poll(self):
        self.monitor = lifecycle.SpotifyLifecycleMonitor(self.messages,
            enabled=True, observed_running=True)
        self.monitor.configure(True)  # Startup registration reapplies settings.
        self.poll(False, 0)
        self.poll(False, 4)
        self.assertEqual(self.messages.get_nowait(), ('spotify_exit', None))

    def test_scan_error_reports_once_and_never_closes_overlay(self):
        self.poll(True, 0)
        self.poll(False, 2)
        self.scan.side_effect = OSError('unavailable')
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.messages.get_nowait(), ('spotify_lifecycle_error', 'unavailable'))
        self.assertTrue(self.messages.empty())
        self.scan.side_effect = None
        self.poll(False, 20)
        self.assertTrue(self.messages.empty())
        self.poll(False, 24)
        self.assertEqual(self.messages.get_nowait(), ('spotify_exit', None))

    def test_disabling_during_scan_discards_inflight_result(self):
        self.poll(True, 0)
        self.poll(False, 2)
        def changed():
            self.monitor.configure(False)
            return False
        self.scan.side_effect = changed
        self.now = 10
        self.monitor.poll()
        self.assertTrue(self.messages.empty())


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path('sample data').resolve()
        self.guard = self.start_patch(patch.object(lifecycle, 'SingleInstance'))
        self.guard.return_value.__enter__.return_value.acquired = True
        self.enabled = self.start_patch(patch.object(lifecycle, '_enabled'))
        self.scan = self.start_patch(patch.object(lifecycle, 'spotify_running'))
        self.exists = self.start_patch(patch.object(lifecycle, '_overlay_running', return_value=False))
        self.closing = self.start_patch(patch.object(lifecycle, '_closing_for_spotify', return_value=False))
        self.clear = self.start_patch(patch.object(lifecycle, 'clear_spotify_exit'))
        self.launch = self.start_patch(patch.object(lifecycle, '_launch'))
        self.sleep = self.start_patch(patch.object(lifecycle.time, 'sleep'))
        self.clock = self.start_patch(patch.object(lifecycle.time, 'monotonic'))
        self.start_patch(patch.object(lifecycle, 'LOGGER'))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def run_samples(self, samples):
        self.enabled.side_effect = [True] * len(samples) + [False]
        self.scan.side_effect = samples
        self.clock.side_effect = range(0, len(samples) * 2, 2)
        lifecycle.watch(self.directory)

    def test_launch_once_and_respect_manual_close_until_spotify_restarts(self):
        self.run_samples([True, True, True, False, False, False, True, True])
        self.assertEqual(self.launch.call_count, 2)
        self.launch.assert_called_with(self.directory)
        self.assertEqual(self.exists.call_count, 2)

    def test_existing_app_is_not_reopened_after_manual_close(self):
        self.exists.return_value = True
        self.run_samples([True, True, True])
        self.launch.assert_not_called()
        self.exists.assert_called_once_with(self.directory)

    def test_spotify_restart_waits_for_previous_overlay_cleanup(self):
        self.closing.side_effect = [False, False, False, True, True, True, False]
        self.exists.side_effect = [True, True, False]
        self.run_samples([True, False, False, False, True, True, True])
        self.launch.assert_called_once_with(self.directory)
        self.assertEqual(self.exists.call_count, 3)
        self.clear.assert_called_once_with(self.directory)

    def test_late_shutdown_marker_restarts_after_start_transition_was_consumed(self):
        self.closing.side_effect = [False, False, False, False, False, True, True, False]
        self.exists.side_effect = [True, True, True, False]
        self.run_samples([True, False, False, False, True, True, True, True])
        self.launch.assert_called_once_with(self.directory)
        self.assertEqual(self.exists.call_count, 4)

    def test_handoff_survives_watcher_missing_the_entire_spotify_restart(self):
        self.closing.side_effect = [False, True, True, False]
        self.exists.side_effect = [True, True, False]
        self.run_samples([True, True, True, True])
        self.launch.assert_called_once_with(self.directory)

    def test_new_manual_instance_clears_handoff_and_remains_respected(self):
        self.closing.side_effect = [True, False, False, False]
        self.exists.side_effect = [True, True]
        self.run_samples([True, True, True, True])
        self.launch.assert_not_called()
        self.assertEqual(self.exists.call_count, 2)

    def test_duplicate_watcher_exits_without_scanning(self):
        self.guard.return_value.__enter__.return_value.acquired = False
        lifecycle.watch(self.directory)
        self.enabled.assert_not_called()
        self.scan.assert_not_called()

    def test_disabled_setting_stops_resident_watcher(self):
        self.enabled.return_value = False
        lifecycle.watch(self.directory)
        self.scan.assert_not_called()
        self.sleep.assert_not_called()

    def test_launch_failure_can_retry_without_a_spotify_restart(self):
        self.launch.side_effect = [OSError('temporary'), Mock()]
        self.run_samples([True, True, True])
        self.assertEqual(self.launch.call_count, 2)

    def test_scan_error_does_not_create_a_new_session(self):
        self.run_samples([True, False, OSError('scan'), True, True])
        self.launch.assert_called_once_with(self.directory)


class StartupTests(unittest.TestCase):
    def test_source_command_preserves_data_dir_and_uses_windowless_python(self):
        with patch.object(lifecycle.sys, 'executable', r'C:\Installed App\.venv\Scripts\python.exe'), \
                patch.object(lifecycle.sys, 'frozen', False, create=True), \
                patch.object(lifecycle.Path, 'is_file', return_value=True):
            directory = Path('a directory with spaces').resolve()
            command = lifecycle.launch_command(directory, watcher=True)
        self.assertEqual(command[0], r'C:\Installed App\.venv\Scripts\pythonw.exe')
        self.assertEqual(Path(command[1]).name, 'spotify_lifecycle.py')
        self.assertEqual(command[2:], ['--watch', '--data-dir', str(directory)])

    def test_packaged_launch_uses_current_executable_and_silent_duplicate_flag(self):
        with patch.object(lifecycle.sys, 'executable', r'C:\Installed App\Lyrics.exe'), \
                patch.object(lifecycle.sys, 'frozen', True, create=True):
            directory = Path('sample data').resolve()
            self.assertEqual(lifecycle.launch_command(directory),
                [r'C:\Installed App\Lyrics.exe', '--data-dir', str(directory), '--spotify-auto-start'])
            self.assertEqual(lifecycle.launch_command(directory, watcher=True),
                [r'C:\Installed App\Lyrics.exe', '--watch-spotify', '--data-dir', str(directory)])

    def test_startup_registers_only_current_user_and_this_data_directory(self):
        registry = Mock()
        registry.CreateKeyEx.return_value = registry.OpenKey.return_value = Mock(
            __enter__=Mock(return_value='key'), __exit__=Mock(return_value=False))
        with patch.dict(sys.modules, {'winreg': registry}):
            lifecycle.configure_startup(Path('sample data'), True)
            lifecycle.configure_startup(Path('sample data'), False)
        registry.CreateKeyEx.assert_called_once_with(registry.HKEY_CURRENT_USER,
            lifecycle.RUN_KEY, 0, registry.KEY_SET_VALUE)
        expected_name = lifecycle.startup_name(Path('sample data'))
        registry.SetValueEx.assert_called_once_with('key', expected_name, 0, registry.REG_SZ,
            subprocess.list2cmdline(lifecycle.launch_command(Path('sample data'), watcher=True)))
        registry.DeleteValue.assert_called_once_with('key', expected_name)
        self.assertNotEqual(expected_name, lifecycle.startup_name(Path('other data')))

    def test_missing_startup_entry_can_be_disabled(self):
        registry = Mock()
        registry.OpenKey.side_effect = FileNotFoundError()
        with patch.dict(sys.modules, {'winreg': registry}):
            lifecycle.configure_startup(Path('sample data'), False)

    def test_watcher_reads_latest_setting_and_defaults_to_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertFalse(lifecycle._enabled(directory))
            settings = directory / 'settings.json'
            settings.write_text('{"spotify_lifecycle": true}', encoding='utf-8')
            self.assertTrue(lifecycle._enabled(directory))
            settings.write_text('{"spotify_lifecycle": false}', encoding='utf-8')
            self.assertFalse(lifecycle._enabled(directory))

    def test_ensure_watcher_does_not_spawn_if_already_running(self):
        with patch.object(lifecycle, 'SingleInstance') as guard, \
                patch.object(lifecycle, '_launch') as launch:
            guard.return_value.__enter__.return_value.acquired = False
            self.assertIsNone(lifecycle.ensure_watcher(Path('sample data')))
            launch.assert_not_called()

    def test_shutdown_marker_persists_until_new_instance_clears_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'app data'
            self.assertFalse(lifecycle._closing_for_spotify(directory))
            lifecycle.mark_spotify_exit(directory)
            self.assertTrue(lifecycle._closing_for_spotify(directory))
            lifecycle.clear_spotify_exit(directory)
            self.assertFalse(lifecycle._closing_for_spotify(directory))
            lifecycle.clear_spotify_exit(directory)


if __name__ == '__main__':
    unittest.main()
