"""Transient Windows replace failures must never expose a partial saved file."""
import copy
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from explorer_library import ExplorerLibrary
import storage


def denied(code):
    error = PermissionError(errno.EACCES, 'simulated Windows replace denial')
    if code is not None:
        error.winerror = code
    return error


class AtomicJsonRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        storage.atomic_json(self.path, {'value': 'old'})
        self.before = self.path.read_bytes()
        self.replace = os.replace
        self.fsync = os.fsync

    def check_old(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_successful_write_never_waits(self):
        with patch('storage.os.replace', wraps=self.replace) as replace, patch('time.sleep') as sleep:
            storage.atomic_json(self.path, {'value': 'new'})
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(storage.read_json(self.path, {}), {'value': 'new'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_known_windows_denials_retry_same_closed_complete_temp(self):
        for failures, code in enumerate((5, 32, 33), 1):
            with self.subTest(winerror=code):
                self.path.write_bytes(self.before)
                attempts = []
                flushed = []
                fake_os = Mock(wraps=os)
                fake_os.name = 'nt'

                def fsync(fd):
                    self.fsync(fd)
                    flushed.append(fd)

                def replace(source, target):
                    self.assertEqual(len(flushed), 1)
                    # Reopening and reading the source verifies complete JSON;
                    # success of real Windows replace below requires it closed.
                    payload = json.loads(Path(source).read_text(encoding='utf-8'))
                    self.assertEqual(payload, {'value': 'new', 'unicode': '가사'})
                    attempts.append((source, target))
                    if len(attempts) <= failures:
                        raise denied(code)
                    self.replace(source, target)

                fake_os.fsync.side_effect = fsync
                fake_os.replace.side_effect = replace
                with patch('storage.os', fake_os), patch('time.sleep') as sleep, \
                        patch('storage.json.dump', wraps=json.dump) as dump:
                    storage.atomic_json(self.path, {'value': 'new', 'unicode': '가사'})
                self.assertEqual(attempts, [attempts[0]] * (failures + 1))
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02, .04][:failures])
                self.assertEqual(dump.call_count, 1)
                self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_persistent_windows_denial_is_bounded_and_preserves_original(self):
        failure = denied(5)
        fake_os = Mock(wraps=os)
        fake_os.name = 'nt'
        fake_os.replace.side_effect = failure
        with patch('storage.os', fake_os), patch('time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                storage.atomic_json(self.path, {'value': 'new'})
        self.assertIs(caught.exception, failure)
        self.assertEqual(fake_os.replace.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.01, .02, .04])
        self.check_old()

    def test_unrelated_or_unknown_errors_do_not_retry(self):
        for failure in (denied(None), denied(123), OSError(errno.ENOSPC, 'disk full')):
            with self.subTest(failure=repr(failure)):
                fake_os = Mock(wraps=os)
                fake_os.name = 'nt'
                fake_os.replace.side_effect = failure
                with patch('storage.os', fake_os), patch('time.sleep') as sleep:
                    with self.assertRaises(OSError) as caught:
                        storage.atomic_json(self.path, {'value': 'new'})
                self.assertIs(caught.exception, failure)
                self.assertEqual(fake_os.replace.call_count, 1)
                sleep.assert_not_called()
                self.check_old()

    def test_non_windows_permission_error_is_not_retried(self):
        failure = denied(5)
        fake_os = Mock(wraps=os)
        fake_os.name = 'posix'
        fake_os.replace.side_effect = failure
        with patch('storage.os', fake_os), patch('time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                storage.atomic_json(self.path, {'value': 'new'})
        self.assertIs(caught.exception, failure)
        self.assertEqual(fake_os.replace.call_count, 1)
        sleep.assert_not_called()
        self.check_old()

    def test_serialization_error_keeps_original_without_replace_or_retry(self):
        for value in ({'bad': object()}, {'bad': float('nan')}):
            with self.subTest(value=repr(value)):
                with patch('storage.os.replace') as replace, patch('time.sleep') as sleep:
                    with self.assertRaises((TypeError, ValueError)):
                        storage.atomic_json(self.path, value)
                replace.assert_not_called()
                sleep.assert_not_called()
                self.check_old()

    def test_fsync_permission_failure_is_not_mistaken_for_replace_failure(self):
        failure = denied(5)
        with patch('storage.os.fsync', side_effect=failure), \
                patch('storage.os.replace') as replace, patch('time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                storage.atomic_json(self.path, {'value': 'new'})
        self.assertIs(caught.exception, failure)
        replace.assert_not_called()
        sleep.assert_not_called()
        self.check_old()

    def test_flush_permission_failure_is_not_retried(self):
        failure = denied(5)
        fdopen = os.fdopen

        class FailOnFlush:
            def __init__(self, *args, **kwargs):
                self.handle = fdopen(*args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def write(self, value):
                return self.handle.write(value)
            def flush(self):
                raise failure

        with patch('storage.os.fdopen', side_effect=FailOnFlush), \
                patch('storage.os.replace') as replace, patch('time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                storage.atomic_json(self.path, {'value': 'new'})
        self.assertIs(caught.exception, failure)
        replace.assert_not_called()
        sleep.assert_not_called()
        self.check_old()

    def test_failed_library_commit_keeps_memory_and_file(self):
        library = ExplorerLibrary(self.path.parent)
        library.toggle_favorite('existing')
        before_value = copy.deepcopy(library.value)
        before_file = library.path.read_bytes()
        fake_os = Mock(wraps=os)
        fake_os.name = 'nt'
        fake_os.replace.side_effect = denied(32)
        with patch('storage.os', fake_os), patch('time.sleep'):
            with self.assertRaises(PermissionError):
                library.toggle_favorite('new')
        self.assertEqual(fake_os.replace.call_count, 4)
        self.assertEqual(library.value, before_value)
        self.assertEqual(library.path.read_bytes(), before_file)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
