import ctypes as C
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import process_audio as native
from audio_capture_cache import CAPTURE_NAME, capture_frames
from audio_worker import capture
from media import Playback
from realtime_audio import LiveWindowWriter
from storage import read_json


class ProcessBufferTests(unittest.TestCase):
    def stream(self,flags=0):
        stream=object.__new__(native.ProcessLoopback)
        stream.capture=native.PTR(1);stream.clock_origin=100.;stream.first_packet=True
        stream.payload=C.create_string_buffer(b'\1\0'*8)
        stream.freed=[]
        def next_packet(this,count):C.cast(count,C.POINTER(native.DWORD))[0]=4;return 0
        def get(this,data,frames,out_flags,position,qpc):
            C.cast(data,C.POINTER(native.PTR))[0]=C.addressof(stream.payload)
            C.cast(frames,C.POINTER(native.DWORD))[0]=4
            C.cast(out_flags,C.POINTER(native.DWORD))[0]=flags
            C.cast(qpc,C.POINTER(C.c_uint64))[0]=25000000
            return 0
        def free(this,frames):stream.freed.append(frames.value);return 0
        stream._next=next_packet;stream._get=get;stream._free=free
        return stream

    def test_silent_packets_use_zeros_and_keep_audio_clock(self):
        stream=self.stream(flags=2)
        self.assertEqual(stream.read(),(bytes(16),4,102.5))
        self.assertEqual(stream.freed,[4])

    def test_packet_loss_aborts_and_releases_buffer_instead_of_shifting_sync(self):
        stream=self.stream(flags=1)
        stream.read()  # The initial discontinuity is permitted by WASAPI.
        with self.assertRaisesRegex(ValueError,'끊겼'):stream.read()
        self.assertEqual(stream.freed,[4,4])

    def test_unreliable_timestamp_is_not_used_as_a_song_start(self):
        stream=self.stream(flags=4)
        self.assertIsNone(stream.read()[2])

    def test_unsupported_windows_does_not_fall_back_to_other_apps_audio(self):
        with patch('process_audio.sys.getwindowsversion',return_value=SimpleNamespace(build=19045)),\
                patch('process_audio.spotify_process_id') as select:
            with self.assertRaisesRegex(ValueError,'Windows'):native.ProcessLoopback()
            select.assert_not_called()


class CapturePipelineTests(unittest.TestCase):
    def run_capture(self,folder,pause_at=None,writer_delay=0,packet_seconds=1,timestamp_jump=0,timestamps=True,
                    missing_timestamp_packets=(),startup_delay=0):
        clock=[100.];holder={};request={'key':Playback('Example','Artist','Album').key,'duration':10,'realtime':True}
        class Monitor:
            def __init__(self,messages):self.messages=messages;holder['monitor']=self;self.stopped=False
            def publish(self):
                elapsed=clock[0]-100
                self.messages.put(('media',Playback('Example','Artist','Album',elapsed,10,
                    pause_at is None or elapsed<pause_at,clock[0])))
            def start(self):self.publish()
            def stop(self):self.stopped=True
        class Stream:
            rate=8000;channels=1
            def __init__(self):holder['stream']=self;self.closed=False;self.frame_position=100.;self.sequence=0
            def start(self):
                clock[0]+=startup_delay;self.frame_position+=startup_delay
                holder['monitor'].publish()
            def read(self,timeout):
                at=self.frame_position;self.frame_position+=packet_seconds
                clock[0]=max(clock[0],self.frame_position);holder['monitor'].publish()
                frames=int(self.rate*packet_seconds)
                stamp=at+(timestamp_jump if at>=101 else 0) if timestamps and self.sequence not in missing_timestamp_packets else None
                self.sequence+=1
                return b'\1\0'*frames,frames,stamp
            def close(self):self.closed=True
            def __enter__(self):return self
            def __exit__(self,*exc):self.close()
        real_feed=LiveWindowWriter.feed
        def feed(writer,*args):
            real_feed(writer,*args)
            if writer_delay and writer.sequence and not holder.get('delayed'):
                clock[0]+=writer_delay;holder['delayed']=True
                holder['monitor'].publish()
        with patch('audio_worker.SpotifyMonitor',Monitor),patch('audio_worker.ProcessLoopback',Stream),\
                patch('audio_worker.time.monotonic',side_effect=lambda:clock[0]),patch('audio_worker.emit'),\
                patch.object(LiveWindowWriter,'feed',feed):
            try:return capture(request,Path(folder)/CAPTURE_NAME)
            finally:
                self.assertTrue(holder['stream'].closed)
                self.assertTrue(holder['monitor'].stopped)

    def test_spotify_capture_supplies_both_live_windows_and_full_song_with_same_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.run_capture(folder)
            self.assertEqual(result,{'start_position':0,'clip_tail':0,'capture_source':'spotify-process'})
            self.assertEqual(capture_frames(Path(folder)/CAPTURE_NAME), (80000,8000,1))
            live=read_json(Path(folder)/'live-window.json',{})
            self.assertEqual(live['end_position'],10)
            self.assertEqual(live['observed_at'],110)
            self.assertEqual(live['start_position'],7)

    def test_pause_cancels_collection_and_closes_spotify_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'일시정지'):self.run_capture(folder,pause_at=3)

    def test_slow_preview_write_preserves_contiguous_buffered_audio_and_original_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.run_capture(folder,writer_delay=4,packet_seconds=.25)
            self.assertEqual(result['start_position'],0)
            self.assertEqual(capture_frames(Path(folder)/CAPTURE_NAME), (80000,8000,1))
            live=read_json(Path(folder)/'live-window.json',{})
            self.assertEqual(live['observed_at'],110)
            self.assertEqual(live['end_position'],10)

    def test_actual_audio_clock_jump_still_aborts_when_frame_count_looks_contiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'재생 시간이 어긋나'):
                self.run_capture(folder,packet_seconds=.25,timestamp_jump=4)

    def test_subsecond_sample_clock_jump_is_rejected_before_writing_a_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'오디오와 재생 시간이 어긋나'):
                self.run_capture(folder,packet_seconds=.25,timestamp_jump=.75)

    def test_missing_device_clock_does_not_confuse_delayed_delivery_with_packet_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.run_capture(folder,writer_delay=4,packet_seconds=.25,timestamps=False)
            self.assertEqual(result['start_position'],0)
            self.assertEqual(capture_frames(Path(folder)/CAPTURE_NAME), (80000,8000,1))

    def test_first_reliable_device_timestamp_rebases_already_recorded_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.run_capture(folder,packet_seconds=.25,startup_delay=.6,missing_timestamp_packets=(0,1,2))
            self.assertAlmostEqual(result['start_position'],.6)
            frames,rate,_=capture_frames(Path(folder)/CAPTURE_NAME)
            self.assertAlmostEqual(frames/rate,9.4,places=3)
            live=read_json(Path(folder)/'live-window.json',{})
            self.assertAlmostEqual(live['observed_at']-live['end_position'],100,places=3)
            self.assertGreater(live['end_position'],9)
            self.assertLessEqual(live['end_position'],10)

    def test_later_invalid_timestamp_keeps_contiguous_sample_clock_after_delivery_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.run_capture(folder,writer_delay=4,packet_seconds=.25,missing_timestamp_packets=range(12,28))
            self.assertEqual(result['start_position'],0)
            self.assertEqual(capture_frames(Path(folder)/CAPTURE_NAME), (80000,8000,1))
