"""Transient WinRT failures recover without touching real Windows sessions."""
import asyncio
from datetime import datetime,timedelta,timezone
import queue
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from media import SpotifyMonitor


class MediaMonitorTests(unittest.TestCase):
    def setUp(self):
        self.messages=queue.Queue();self.monitor=SpotifyMonitor(self.messages)
        self.session=SimpleNamespace(
            source_app_user_model_id='Spotify.exe',
            get_playback_info=lambda:SimpleNamespace(playback_status='paused',playback_rate=1),
            try_get_media_properties_async=AsyncMock(return_value=SimpleNamespace(title='Song',artist='Artist',album_title='Album')),
            get_timeline_properties=lambda:SimpleNamespace(end_time=timedelta(seconds=180),position=timedelta(seconds=12),last_updated_time=datetime.now(timezone.utc)))
        self.manager=SimpleNamespace(get_sessions=lambda:[self.session])
        self.request=AsyncMock(return_value=self.manager)
        module=ModuleType('winrt.windows.media.control')
        module.GlobalSystemMediaTransportControlsSessionManager=SimpleNamespace(request_async=self.request)
        module.GlobalSystemMediaTransportControlsSessionPlaybackStatus=SimpleNamespace(PLAYING='playing')
        modules={name:ModuleType(name) for name in ('winrt','winrt.windows','winrt.windows.media')}
        modules[module.__name__]=module
        self.modules=patch.dict(sys.modules,modules);self.modules.start();self.addCleanup(self.modules.stop)
        self.addCleanup(self.monitor.stop)

    def run_until_success(self):
        emitted=[]
        def receive(item):
            emitted.append(item)
            if not item[1].error or len(emitted)>=4:self.monitor.stop()
        with patch.object(self.messages,'put',side_effect=receive),patch('media.asyncio.sleep',new=AsyncMock()):
            self.monitor._run()
        return emitted

    def test_startup_failure_retries_then_delivers_normal_playback(self):
        self.request.side_effect=[OSError('synthetic detail'),self.manager]
        events=self.run_until_success()
        self.assertEqual(self.request.await_count,2)
        self.assertEqual(len(events),2)
        self.assertIn('재시도',events[0][1].error)
        self.assertNotIn('synthetic detail',events[0][1].error)
        self.assertEqual((events[1][1].title,events[1][1].base,events[1][1].error),('Song',12,''))

    def test_failed_manager_is_replaced_after_a_successful_connection(self):
        broken=SimpleNamespace(get_sessions=lambda:(_ for _ in ()).throw(OSError('disconnected')))
        self.request.side_effect=[broken,self.manager]
        events=self.run_until_success()
        self.assertEqual(self.request.await_count,2)
        self.assertEqual([bool(event[1].error) for event in events],[True,False])

    def test_manager_hang_has_four_second_bound_and_recovers(self):
        actual_wait_for=asyncio.wait_for;timeouts=[];cancelled=[];attempts=0
        async def request():
            nonlocal attempts
            attempts+=1
            if attempts==1:
                try:await asyncio.Event().wait()
                finally:cancelled.append(True)
            return self.manager
        async def shortened(awaitable,timeout):
            timeouts.append(timeout)
            return await actual_wait_for(awaitable,timeout=.02)
        self.request.side_effect=request
        with patch('media.asyncio.wait_for',side_effect=shortened):events=self.run_until_success()
        self.assertEqual(cancelled,[True])
        self.assertEqual(timeouts,[4,4,4])
        self.assertEqual([bool(event[1].error) for event in events],[True,False])

    def test_stop_after_failure_does_not_retry_or_sleep(self):
        self.request.side_effect=OSError('temporary')
        with patch.object(self.messages,'put',side_effect=lambda event:self.monitor.stop()) as output,patch('media.asyncio.sleep',new=AsyncMock()) as sleep:
            self.monitor._run()
        self.assertEqual(self.request.await_count,1)
        self.assertEqual(output.call_count,1);sleep.assert_not_awaited()

    def test_stop_during_retry_delay_does_not_request_another_manager(self):
        self.request.side_effect=OSError('temporary')
        async def stop(delay):self.monitor.stop()
        with patch('media.asyncio.sleep',side_effect=stop) as sleep:self.monitor._run()
        sleep.assert_awaited_once_with(.5)
        self.assertEqual(self.request.await_count,1)
        self.assertEqual(self.messages.qsize(),1)

    def test_stop_during_manager_request_does_not_read_session_or_publish(self):
        async def stop():
            self.monitor.stop();return self.manager
        self.request.side_effect=stop
        self.monitor._run()
        self.session.try_get_media_properties_async.assert_not_awaited()
        self.assertTrue(self.messages.empty())

    def test_stop_during_metadata_request_does_not_publish_late_playback(self):
        props=self.session.try_get_media_properties_async.return_value
        async def stop():
            self.monitor.stop();return props
        self.session.try_get_media_properties_async.side_effect=stop
        self.monitor._run()
        self.assertTrue(self.messages.empty())


if __name__=='__main__':unittest.main()
