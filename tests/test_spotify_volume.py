from contextlib import contextmanager
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import spotify_volume as volume


class FakeControl:
    def __init__(self, name='', kind='GroupControl', children=(), pid=42,
                 offscreen=False, enabled=True):
        self.Name = name
        self.ControlTypeName = kind
        self.ProcessId = pid
        self.IsOffscreen = offscreen
        self.IsEnabled = enabled
        self.AutomationId = ''
        self.children = list(children)
        self.range = None
        self.invoke = None

    def GetChildren(self):return self.children
    def GetRangeValuePattern(self):return self.range
    def GetInvokePattern(self):return self.invoke


class FakeVolume:
    def __init__(self, level=.7, minimum=0, maximum=1, muted=False, offscreen=False):
        self.writes = []
        self.slider = FakeControl('볼륨 변경', 'SliderControl', offscreen=offscreen)
        self.mute = FakeControl('음소거 해제' if muted else '음소거하기', 'ButtonControl',
                                offscreen=offscreen)
        self.range = SimpleNamespace(Minimum=minimum, Maximum=maximum, Value=level,
                                     IsReadOnly=False, SetValue=Mock(side_effect=self.set_volume))
        self.slider.range = self.range
        self.mute.invoke = SimpleNamespace(Invoke=Mock(side_effect=self.toggle_mute))
        self.saved_level = level if level > minimum else maximum * .7

    def set_volume(self, value, waitTime=0):
        self.writes.append(('volume', value))
        self.range.Value = value
        if value > self.range.Minimum:
            self.saved_level = value
            self.mute.Name = '음소거하기'
        else:self.mute.Name = '음소거 해제'
        return True

    def toggle_mute(self, waitTime=0):
        self.writes.append(('mute', None))
        if self.mute.Name == '음소거 해제':
            self.range.Value = self.saved_level
            self.mute.Name = '음소거하기'
        else:
            self.saved_level = self.range.Value
            self.range.Value = self.range.Minimum
            self.mute.Name = '음소거 해제'
        return True

    def window(self, *extra, pid=42):
        return FakeControl('Spotify', 'WindowControl', [*extra, self.slider, self.mute],
                           pid=pid, offscreen=self.slider.IsOffscreen)


class SpotifyControlsTests(unittest.TestCase):
    def find(self, *windows, stopped=None, deadline=None):
        auto = SimpleNamespace(GetRootControl=lambda: FakeControl(children=windows))
        with patch.object(volume, 'process_is_spotify', side_effect=lambda pid: pid == 42), \
                patch.object(volume, '_spotify_windows', return_value=windows):
            return volume._find_controls(auto, stopped or threading.Event(),
                                         time.monotonic() + 10 if deadline is None else deadline)

    def test_ignores_other_apps_and_playback_position_slider(self):
        spotify, other = FakeVolume(.7), FakeVolume(.2)
        seek = FakeControl('재생 위치', 'SliderControl')
        seek.range = SimpleNamespace(Minimum=0, Maximum=1, Value=.5, IsReadOnly=False)
        slider, mute = self.find(other.window(pid=99), spotify.window(seek))
        self.assertIs(slider, spotify.slider)
        self.assertIs(mute, spotify.mute)
        self.assertEqual(spotify.writes + other.writes, [])

    def test_minimized_spotify_controls_remain_available(self):
        spotify = FakeVolume(offscreen=True)
        self.assertEqual(self.find(spotify.window()), (spotify.slider, spotify.mute))

    def test_volume_label_on_non_slider_is_not_a_candidate(self):
        spotify = FakeVolume()
        spotify.slider.ControlTypeName = 'ProgressBarControl'
        with self.assertRaises(ValueError):self.find(spotify.window())

    def test_unrelated_volume_names_are_not_matched_by_substring(self):
        for name in ('재생 볼륨 변경 기록', 'Volume history', '음량 변경 내역'):
            with self.subTest(name=name):
                spotify = FakeVolume()
                spotify.slider.Name = name
                with self.assertRaises(ValueError):self.find(spotify.window())

    def test_multiple_spotify_volume_candidates_are_rejected(self):
        first, second = FakeVolume(.2), FakeVolume(.9)
        with self.assertRaises(ValueError):self.find(first.window(), second.window())
        self.assertEqual(first.writes + second.writes, [])

    def test_missing_native_controls_are_rejected(self):
        for missing in ('slider', 'mute'):
            with self.subTest(missing=missing):
                spotify = FakeVolume()
                window = spotify.window()
                window.children.remove(getattr(spotify, missing))
                with self.assertRaises(ValueError):self.find(window)

    def test_expired_deadline_does_not_return_a_candidate(self):
        spotify = FakeVolume()
        with self.assertRaises(ValueError):self.find(spotify.window(), deadline=time.monotonic() - 1)

    def test_read_normalizes_actual_range_and_rounds_percent(self):
        spotify = FakeVolume(-21.24, minimum=-60, maximum=0)
        self.assertEqual(volume._read_controls(spotify.slider, spotify.mute),
                         {'available': True, 'volume': 65, 'step': 1, 'muted': False, 'error': ''})

    def test_read_observes_changes_made_directly_in_spotify(self):
        spotify = FakeVolume(.7)
        self.assertEqual(volume._read_controls(spotify.slider, spotify.mute)['volume'], 70)
        spotify.range.Value = .237
        self.assertEqual(volume._read_controls(spotify.slider, spotify.mute)['volume'], 24)
        self.assertEqual(spotify.writes, [])

    def test_mute_and_unmute_names_are_distinct(self):
        spotify = FakeVolume(.4)
        for name, muted in [('음소거하기', False), ('음소거 해제', True),
                            ('Mute', False), ('Unmute', True)]:
            with self.subTest(name=name):
                spotify.mute.Name = name
                self.assertEqual(volume._read_controls(spotify.slider, spotify.mute)['muted'], muted)

    def test_invalid_range_or_value_is_rejected(self):
        for changes in ({'Minimum': 1, 'Maximum': 1}, {'Minimum': 2, 'Maximum': 1},
                        {'Minimum': float('nan')}, {'Maximum': float('inf')},
                        {'Value': float('nan')}, {'Value': float('inf')}, {'Value': -1}, {'Value': 2}):
            with self.subTest(changes=changes):
                spotify = FakeVolume()
                for key, value in changes.items():setattr(spotify.range, key, value)
                with self.assertRaises(ValueError):volume._read_controls(spotify.slider, spotify.mute)

    def test_disabled_slider_cannot_publish_an_available_state(self):
        spotify = FakeVolume()
        spotify.slider.IsEnabled = False
        with self.assertRaises(ValueError):volume._read_controls(spotify.slider, spotify.mute)


class ApplyTests(unittest.TestCase):
    @contextmanager
    def controls(self, spotify):
        order = []
        def initialized(mode):
            self.assertEqual(mode, 0)
            order.append('initialize')
        auto = SimpleNamespace(comtypes=SimpleNamespace(CoInitializeEx=initialized,
            CoUninitialize=lambda:order.append('uninitialize')))
        with patch.dict(sys.modules, {'uiautomation': auto}), \
                patch.object(volume, 'process_is_spotify', side_effect=lambda pid: pid == 42), \
                patch.object(volume, '_refresh_controls'), \
                patch.object(volume, '_find_controls', return_value=(spotify.slider, spotify.mute)) as find:
            yield order, find

    def test_refresh_only_reads_native_spotify_volume(self):
        spotify = FakeVolume(.71)
        with self.controls(spotify) as (order, find):
            state = volume._apply_native([], threading.Event())
            self.assertEqual(order, ['initialize', 'uninitialize'])
        self.assertEqual(state, {'available': True, 'volume': 71, 'step': 1, 'muted': False, 'error': ''})
        self.assertEqual(spotify.writes, [])
        self.assertTrue(find.called)

    def test_native_fallback_uses_the_spotify_slider_step(self):
        spotify = FakeVolume(.5)
        spotify.range.SmallChange = .10000000149
        with self.controls(spotify):state = volume._apply_native([('volume', 69)], threading.Event())
        self.assertEqual(state['volume'], 70)
        self.assertEqual(state['step'], 10)
        self.assertAlmostEqual(spotify.writes[0][1], .7)

    def test_percentage_maps_to_spotify_range(self):
        spotify = FakeVolume(150, minimum=100, maximum=200)
        with self.controls(spotify):state = volume._apply_native([('volume', 37)], threading.Event())
        spotify.range.SetValue.assert_called_once_with(137, waitTime=0)
        spotify.mute.invoke.Invoke.assert_not_called()
        self.assertEqual(state['volume'], 37)

    def test_command_order_uses_native_mute_and_restore(self):
        spotify = FakeVolume(.7)
        with self.controls(spotify):
            state = volume._apply_native([('mute', None), ('mute', None), ('volume', 30), ('mute', None)],
                                  threading.Event())
        self.assertEqual(spotify.writes, [('mute', None), ('mute', None), ('volume', .3), ('mute', None)])
        self.assertEqual(spotify.mute.invoke.Invoke.call_count, 3)
        self.assertTrue(state['muted'])
        self.assertEqual(state['volume'], 0)

    def test_successful_call_without_changed_native_state_is_rejected(self):
        spotify = FakeVolume()
        spotify.range.SetValue.side_effect = None
        spotify.range.SetValue.return_value = True
        with self.controls(spotify), patch.object(volume.time, 'monotonic', side_effect=[0, 0, 3]), \
                self.assertRaises(ValueError):
            volume._apply_native([('volume', 37)], threading.Event())

    def test_native_pattern_failure_is_reported(self):
        for kind in ('volume', 'mute'):
            with self.subTest(kind=kind):
                spotify = FakeVolume()
                method = spotify.range.SetValue if kind == 'volume' else spotify.mute.invoke.Invoke
                method.side_effect = None
                method.return_value = False
                with self.controls(spotify), self.assertRaises((ValueError, OSError)):
                    volume._apply_native([(kind, 30 if kind == 'volume' else None)], threading.Event())

    def test_read_only_slider_cannot_be_changed(self):
        spotify = FakeVolume()
        spotify.range.IsReadOnly = True
        with self.controls(spotify), self.assertRaises((ValueError, OSError)):
            volume._apply_native([('volume', 30)], threading.Event())
        spotify.range.SetValue.assert_not_called()

    def test_changed_process_or_label_prevents_writes(self):
        for attribute, value in [('ProcessId', 99), ('Name', '재생 위치')]:
            with self.subTest(attribute=attribute):
                spotify = FakeVolume()
                setattr(spotify.slider, attribute, value)
                with self.controls(spotify), self.assertRaises(ValueError):
                    volume._apply_native([('volume', 30)], threading.Event())
                self.assertEqual(spotify.writes, [])

    def test_cancellation_prevents_pending_writes(self):
        spotify = FakeVolume()
        stopped = threading.Event()
        stopped.set()
        with self.controls(spotify):
            self.assertIsNone(volume._apply_native([('volume', 30)], stopped))
        self.assertEqual(spotify.writes, [])

    def test_cancellation_after_first_command_prevents_later_commands(self):
        spotify = FakeVolume()
        stopped = threading.Event()
        def stop_after_write(value, waitTime=0):
            spotify.set_volume(value, waitTime)
            stopped.set()
            return True
        spotify.range.SetValue.side_effect = stop_after_write
        with self.controls(spotify):
            self.assertIsNone(volume._apply_native([('volume', 30), ('mute', None)], stopped))
        self.assertEqual(spotify.writes, [('volume', .3)])
        spotify.mute.invoke.Invoke.assert_not_called()


class SessionTests(unittest.TestCase):
    def test_local_volume_uses_exact_percentages_and_mute_preserves_level(self):
        session = SimpleNamespace(volume=.7, muted=False)
        def read_volume(session, level):
            level._obj.value = session.volume
            return 0
        def read_mute(session, muted):
            muted._obj.value = session.muted
            return 0
        def set_volume(session, level, _):
            session.volume = level
            return 0
        def set_mute(session, muted, _):
            session.muted = muted
            return 0
        methods = {3: set_volume, 4: read_volume, 5: set_mute, 6: read_mute}
        with patch.object(volume, '_method', side_effect=lambda _, index, *args: methods[index]):
            stopped = threading.Event()
            self.assertEqual(volume._apply_session(session, [('volume', 69)], stopped)['volume'], 69)
            muted = volume._apply_session(session, [('volume', 0)], stopped)
            self.assertTrue(muted['muted'])
            self.assertEqual(muted['volume'], 0)
            self.assertEqual(volume._apply_session(session, [('mute', None)], stopped)['volume'], 69)
            self.assertEqual(volume._apply_session(session, [('volume', 1)], stopped)['volume'], 1)


READY = {'available': True, 'volume': 75, 'muted': False, 'error': ''}


class ControllerTests(unittest.TestCase):
    def controller(self, callback=None):
        controller = volume.SpotifyVolumeController(callback)
        def cleanup():
            controller.close()
            if controller._thread:controller._thread.join(2)
        self.addCleanup(cleanup)
        return controller

    def test_invalid_values_never_queue_a_change(self):
        controller = self.controller()
        for value in (-1, 101, float('nan'), float('inf'), True, None, '10'):
            with self.subTest(value=value):self.assertFalse(controller.set_volume(value))
        self.assertEqual(controller.get_state()['revision'], 0)

    def test_rapid_slider_changes_coalesce_without_reordering_mute(self):
        results = queue.Queue()
        controller = self.controller(results.put)
        controller.set_volume(10)
        controller.set_volume(20)
        controller.toggle_mute()
        controller.set_volume(30)
        controller.set_volume(40)
        self.assertEqual(controller.get_state()['revision'], 5)
        self.assertTrue(controller.get_state()['busy'])
        with patch.object(volume, '_apply', return_value=READY) as apply:
            controller.start()
            state = results.get(timeout=2)
            self.assertEqual(apply.call_args.args[0], [('volume', 20), ('mute', None), ('volume', 40)])
        self.assertEqual(state['revision'], 5)
        self.assertFalse(state['busy'])

    def test_read_in_flight_cannot_overwrite_newer_slider_request(self):
        entered, release = threading.Event(), threading.Event()
        results = queue.Queue()
        calls = []
        def apply(commands, stopped):
            calls.append(commands)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                return {**READY, 'volume': 1}
            return {**READY, 'volume': 42}
        controller = self.controller(results.put)
        with patch.object(volume, '_apply', side_effect=apply):
            controller.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(controller.set_volume(42))
            release.set()
            state = results.get(timeout=2)
            self.assertEqual(state['volume'], 42)
            self.assertEqual(state['revision'], 1)
            self.assertTrue(results.empty())

    def test_close_returns_while_worker_is_busy_and_discards_queued_commands(self):
        entered, release = threading.Event(), threading.Event()
        results = queue.Queue()
        def apply(commands, stopped):
            entered.set()
            release.wait(2)
            return READY
        controller = self.controller(results.put)
        with patch.object(volume, '_apply', side_effect=apply) as apply_mock:
            controller.start()
            self.assertTrue(entered.wait(2))
            controller.set_volume(42)
            controller.close()
            self.assertTrue(controller._thread.is_alive())
            self.assertFalse(controller.set_volume(50))
            self.assertFalse(controller.toggle_mute())
            self.assertFalse(controller.start())
            self.assertFalse(controller.request_refresh())
            release.set()
            controller._thread.join(2)
            self.assertFalse(controller._thread.is_alive())
            self.assertEqual(apply_mock.call_count, 1)
            self.assertTrue(results.empty())

    def test_native_error_is_reported_and_refresh_can_recover(self):
        results = queue.Queue()
        controller = self.controller(results.put)
        with patch.object(volume, '_apply', side_effect=[OSError('device removed'), READY]):
            controller.start()
            state = results.get(timeout=2)
            self.assertFalse(state['available'])
            self.assertIn('device removed', state['error'])
            controller.request_refresh()
            self.assertTrue(results.get(timeout=2)['available'])

    def test_callbacks_run_on_worker_and_state_is_a_copy(self):
        results = queue.Queue()
        controller = self.controller(lambda state: results.put((threading.get_ident(), state)))
        with patch.object(volume, '_apply', return_value=READY):
            controller.start()
            thread_id, state = results.get(timeout=2)
        self.assertNotEqual(thread_id, threading.get_ident())
        state['volume'] = 3
        copy = controller.get_state()
        self.assertEqual(copy['volume'], 75)
        copy['volume'] = 7
        self.assertEqual(controller.get_state()['volume'], 75)


if __name__ == '__main__':unittest.main()
