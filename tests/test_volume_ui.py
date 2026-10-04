"""Volume panel integration without changing real audio or stored settings."""
import tkinter as tk
import unittest
from unittest.mock import patch

from volume_popup import VolumePopup


class FakeController:
    def __init__(self, on_state):
        self.on_state = on_state
        self.calls = []
        self.revision = 0

    def emit(self, volume=42, muted=False, available=True, error='', revision=None):
        self.on_state(dict(volume=volume, muted=muted, available=available, error=error,
                           revision=self.revision if revision is None else revision))

    def start(self):self.emit()
    def request_refresh(self):self.calls.append('refresh')
    def close(self):self.calls.append('close')
    def get_state(self):return {'revision':self.revision}

    def set_volume(self, value):
        self.calls.append(('volume', value))
        self.revision += 1
        return True

    def toggle_mute(self):
        self.calls.append('mute')
        self.revision += 1
        return True


class VolumeUITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.anchor = tk.Button(self.root, text='음량')
        self.anchor.pack()
        self.root.update()
        self.patch = patch('volume_popup.SpotifyVolumeController', FakeController)
        self.patch.start()
        self.panel = VolumePopup(self.root, self.anchor)
        self.backend = self.panel.controller
        self.drain()
        self.root.update()

    def tearDown(self):
        self.panel.close()
        self.root.destroy()
        self.patch.stop()

    def drain(self):
        self.panel.window.after_cancel(self.panel.poll_timer)
        self.panel.poll()

    def test_opening_and_external_updates_do_not_change_audio(self):
        self.assertEqual(self.panel.volume.get(), 42)
        self.assertEqual(self.backend.calls, [])
        self.backend.emit(68, muted=True)
        self.drain()
        self.root.update()
        self.assertEqual(self.panel.volume.get(), 68)
        self.assertEqual(self.panel.mute_button.cget('text'), '음소거 해제')
        self.assertEqual(self.backend.calls, [])

    def test_drag_keeps_latest_value_and_sends_it_on_release(self):
        self.panel.start_drag()
        for value in (40, 25, 10):
            self.panel.volume.set(value)
            self.panel.change_volume(str(value))
        self.backend.emit(42)
        self.drain()
        self.assertEqual(self.panel.volume.get(), 10)
        self.panel.end_drag()
        self.assertEqual(self.backend.calls, [('volume', 10)])
        self.assertIsNone(self.panel.pending)

    def test_mute_flushes_pending_volume_before_toggling(self):
        self.panel.volume.set(23)
        self.panel.change_volume('23')
        self.panel.mute_button.invoke()
        self.assertEqual(self.backend.calls, [('volume', 23), 'mute'])

    def test_native_step_is_used_for_drag_and_keyboard(self):
        self.panel.events.put(dict(available=True,volume=70,step=10,muted=False,error='',revision=0))
        self.drain()
        self.panel.change_volume('69')
        self.assertEqual(self.panel.volume.get(),70)
        self.panel.step_volume(-1)
        self.assertEqual(self.panel.volume.get(),60)
        self.panel.flush_volume()
        self.assertEqual(self.backend.calls,[('volume',60)])

    def test_queued_old_read_cannot_undo_a_new_slider_request(self):
        self.backend.emit(42)
        self.panel.volume.set(23)
        self.panel.change_volume('23')
        self.panel.flush_volume()
        self.drain()
        self.assertEqual(self.panel.volume.get(), 23)
        self.backend.emit(23)
        self.drain()
        self.assertEqual(self.panel.volume.get(), 23)

    def test_unavailable_session_disables_controls_then_recovers(self):
        self.backend.emit(available=False, error='Spotify에서 곡을 재생해 주세요.')
        self.drain()
        self.assertTrue(self.panel.slider.instate(['disabled']))
        self.assertTrue(self.panel.mute_button.instate(['disabled']))
        self.assertIn('재생', self.panel.status.cget('text'))
        self.backend.emit(15)
        self.drain()
        self.assertFalse(self.panel.slider.instate(['disabled']))
        self.assertEqual(self.panel.volume.get(), 15)

    def test_close_discards_debounced_change_and_stops_worker_once(self):
        self.panel.volume.set(5)
        self.panel.change_volume('5')
        self.panel.close()
        self.panel.close()
        self.assertEqual(self.backend.calls, ['close'])
        self.assertFalse(self.panel.window.winfo_exists())


if __name__ == '__main__':unittest.main()
