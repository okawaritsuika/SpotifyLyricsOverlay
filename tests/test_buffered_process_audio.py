import threading
import time
import unittest
from unittest.mock import patch

from process_audio import BufferedProcessLoopback


class BufferedCaptureTests(unittest.TestCase):
    def native(self,packets=100,fail_at=None,missing_at=()):
        calls=[];holder={}
        class Stream:
            rate=1000;channels=1
            def __init__(self,pid):
                calls.append(('open',threading.get_ident()));holder['stream']=self
                self.index=0;self.started=False;self.closed=False
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
            def start(self):self.started=True;calls.append(('start',threading.get_ident()))
            def read(self,timeout=.1):
                calls.append(('read',threading.get_ident()))
                time.sleep(.001)
                if self.index==fail_at:raise ValueError('native discontinuity')
                if self.index>=packets:return None
                index=self.index;self.index+=1
                return bytes([index%255])*20,10,None if index in missing_at else 100+index*.01
            def close(self):self.closed=True;calls.append(('close',threading.get_ident()))
        return Stream,calls,holder

    def test_native_com_lifecycle_stays_on_one_non_consumer_thread(self):
        native,calls,holder=self.native()
        consumer=threading.get_ident()
        with patch('process_audio.ProcessLoopback',native):
            stream=BufferedProcessLoopback()
            self.assertFalse(holder['stream'].started)
            self.assertEqual([kind for kind,_ in calls],['open'])
            stream.start()
            self.assertIsNotNone(stream.read())
            stream.close();stream.close()
        self.assertFalse(stream.thread.is_alive())
        self.assertTrue(holder['stream'].closed)
        owners={owner for _,owner in calls}
        self.assertEqual(len(owners),1);self.assertNotIn(consumer,owners)
        self.assertEqual([kind for kind,_ in calls].count('start'),1)
        self.assertEqual([kind for kind,_ in calls].count('close'),1)

    def test_slow_consumer_does_not_shift_or_drop_native_packets(self):
        native,_,_=self.native(packets=60)
        with patch('process_audio.ProcessLoopback',native),BufferedProcessLoopback() as stream:
            stream.start();time.sleep(.08)
            packets=[stream.read(timeout=.1) for _ in range(60)]
        self.assertTrue(all(packet is not None for packet in packets))
        self.assertEqual([packet[0][0] for packet in packets],list(range(60)))
        self.assertEqual([packet[1] for packet in packets],[10]*60)
        self.assertEqual([packet[2] for packet in packets],[100+i*.01 for i in range(60)])

    def test_queue_overflow_fails_explicitly_and_closes_native_capture(self):
        native,_,holder=self.native()
        with patch('process_audio.ProcessLoopback',native):
            stream=BufferedProcessLoopback(buffer_seconds=.03)
            try:
                stream.start()
                self.assertTrue(stream.finished.wait(1))
                with self.assertRaisesRegex(ValueError,'대기 버퍼가 가득'):
                    stream.read()
                self.assertLessEqual(stream.queued_frames,30)
                self.assertTrue(holder['stream'].closed)
            finally:stream.close()
        self.assertEqual(stream.queued_frames,0)
        self.assertTrue(stream.packets.empty())
        self.assertFalse(stream.thread.is_alive())

    def test_native_discontinuity_is_propagated_instead_of_using_queued_prefix(self):
        native,_,holder=self.native(fail_at=2)
        with patch('process_audio.ProcessLoopback',native),BufferedProcessLoopback() as stream:
            stream.start();self.assertTrue(stream.finished.wait(1))
            with self.assertRaisesRegex(ValueError,'native discontinuity'):stream.read()
            self.assertTrue(holder['stream'].closed)

    def test_close_while_waiting_never_starts_collection(self):
        native,calls,holder=self.native()
        with patch('process_audio.ProcessLoopback',native):
            stream=BufferedProcessLoopback();stream.close()
        self.assertFalse(holder['stream'].started)
        self.assertEqual([kind for kind,_ in calls],['open','close'])
        self.assertFalse(stream.thread.is_alive())

    def test_missing_native_timestamps_are_preserved_for_worker_recovery(self):
        native,_,_=self.native(packets=3,missing_at=(0,2))
        with patch('process_audio.ProcessLoopback',native),BufferedProcessLoopback() as stream:
            stream.start()
            self.assertEqual([stream.read()[2] for _ in range(3)],[None,100.01,None])

    def test_native_open_error_does_not_leave_waiting_thread_alive(self):
        calls=[]
        def fail(pid):
            calls.append(threading.current_thread())
            raise ValueError('native activation failed')
        with patch('process_audio.ProcessLoopback',fail):
            with self.assertRaisesRegex(ValueError,'native activation failed'):BufferedProcessLoopback()
        self.assertFalse(calls[0].is_alive())

    def test_close_surfaces_failure_that_arrived_after_last_consumer_read(self):
        native,_,holder=self.native(fail_at=2)
        proceed=threading.Event();read=native.read
        def after_first(stream,timeout=.1):
            if stream.index:proceed.wait(1)
            return read(stream,timeout)
        with patch('process_audio.ProcessLoopback',native),patch.object(native,'read',after_first):
            stream=BufferedProcessLoopback()
            stream.start();self.assertIsNotNone(stream.read())
            proceed.set();self.assertTrue(stream.finished.wait(1))
            with self.assertRaisesRegex(ValueError,'native discontinuity'):stream.close()
            stream.close()  # The same failure must not mask cleanup again.
        self.assertFalse(stream.thread.is_alive())
        self.assertTrue(stream.packets.empty())
        self.assertTrue(holder['stream'].closed)

    def test_native_close_error_is_reported_after_owner_thread_has_exited(self):
        native,calls,_=self.native()
        def fail_close(stream):
            calls.append(('failed_close',threading.get_ident()))
            raise ValueError('native close failed')
        with patch('process_audio.ProcessLoopback',native),patch.object(native,'close',fail_close):
            stream=BufferedProcessLoopback()
            with self.assertRaisesRegex(ValueError,'native close failed'):stream.close()
            stream.close()
        self.assertFalse(stream.thread.is_alive())
        self.assertEqual([kind for kind,_ in calls].count('failed_close'),1)


if __name__=='__main__':unittest.main()
