"""Transient WinRT failures recover without touching real Windows sessions."""
import asyncio
from datetime import datetime,timedelta,timezone
import queue
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from media import SpotifyMonitor,_wait_for_refresh


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
        with patch.object(self.messages,'put',side_effect=receive),patch('media._wait_for_refresh',new=AsyncMock()):
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
        self.assertEqual(timeouts,[4,4,4,4])
        self.assertEqual([bool(event[1].error) for event in events],[True,False])

    def test_stop_after_failure_does_not_retry_or_sleep(self):
        self.request.side_effect=OSError('temporary')
        with patch.object(self.messages,'put',side_effect=lambda event:self.monitor.stop()) as output,patch('media._wait_for_refresh',new=AsyncMock()) as sleep:
            self.monitor._run()
        self.assertEqual(self.request.await_count,1)
        self.assertEqual(output.call_count,1);sleep.assert_not_awaited()

    def test_stop_during_retry_delay_does_not_request_another_manager(self):
        self.request.side_effect=OSError('temporary')
        async def stop(changed,delay):self.monitor.stop()
        with patch('media._wait_for_refresh',side_effect=stop) as sleep:self.monitor._run()
        self.assertEqual([call.args[1] for call in sleep.await_args_list],[.5])
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

    def test_stable_metadata_keeps_normal_poll_interval_and_timeline(self):
        async def stop(changed,delay):self.monitor.stop()
        with patch('media._wait_for_refresh',side_effect=stop) as sleep:
            self.monitor._run()
        self.assertEqual([call.args[1] for call in sleep.await_args_list],[.5])
        self.assertEqual(self.session.try_get_media_properties_async.await_count,2)
        event,snapshot=self.messages.get_nowait()
        self.assertEqual(event,'media')
        self.assertEqual((snapshot.title,snapshot.artist,snapshot.album,snapshot.base,snapshot.duration),
                         ('Song','Artist','Album',12,180))
        self.assertFalse(snapshot.playing)
        self.assertTrue(self.messages.empty())

    def test_notifications_refresh_and_rebind_without_waiting_for_fallback(self):
        replacement=SimpleNamespace(**vars(self.session))
        replacement.try_get_media_properties_async=AsyncMock(
            return_value=SimpleNamespace(title='Replacement',artist='Artist',album_title='Album'))
        sources=[self.session]
        self.manager.get_sessions=lambda:sources[:]
        for source,names in ((self.manager,('sessions_changed',)),
                             (self.session,('media_properties_changed','playback_info_changed')),
                             (replacement,('media_properties_changed','playback_info_changed'))):
            for name in names:
                setattr(source,'add_'+name,Mock(return_value=name))
                setattr(source,'remove_'+name,Mock())

        async def run():
            updates=asyncio.Queue()
            async def wait_for_notification(changed,delay):
                await _wait_for_refresh(changed,30)
            with patch.object(self.messages,'put',side_effect=updates.put_nowait), \
                    patch('media._wait_for_refresh',side_effect=wait_for_notification):
                task=asyncio.create_task(self.monitor._poll())
                try:
                    self.assertEqual((await asyncio.wait_for(updates.get(),2))[1].title,'Song')
                    self.session.try_get_media_properties_async.return_value.title='Next'
                    notify=self.session.add_media_properties_changed.call_args.args[0]
                    await asyncio.to_thread(notify,None,None)
                    self.assertEqual((await asyncio.wait_for(updates.get(),2))[1].title,'Next')
                    self.session.add_media_properties_changed.assert_called_once()

                    self.session.get_playback_info=lambda:SimpleNamespace(playback_status='playing',playback_rate=1)
                    notify=self.session.add_playback_info_changed.call_args.args[0]
                    await asyncio.to_thread(notify,None,None)
                    self.assertTrue((await asyncio.wait_for(updates.get(),2))[1].playing)

                    sources[:]=[replacement]
                    notify=self.manager.add_sessions_changed.call_args.args[0]
                    await asyncio.to_thread(notify,None,None)
                    self.assertEqual((await asyncio.wait_for(updates.get(),2))[1].title,'Replacement')
                    self.session.remove_media_properties_changed.assert_called_once_with('media_properties_changed')
                    self.session.remove_playback_info_changed.assert_called_once_with('playback_info_changed')
                finally:
                    self.monitor.stop()
                    await asyncio.wait_for(task,2)

        asyncio.run(run())
        replacement.remove_media_properties_changed.assert_called_once_with('media_properties_changed')
        replacement.remove_playback_info_changed.assert_called_once_with('playback_info_changed')
        self.manager.remove_sessions_changed.assert_called_once_with('sessions_changed')

    def test_track_change_during_read_does_not_publish_old_identity_with_new_timeline(self):
        old=SimpleNamespace(title='Song',artist='Artist',album_title='Album')
        for field in ('title','artist','album_title'):
            with self.subTest(field=field):
                self.monitor.stop_event.clear()
                self.request.reset_mock()
                new=SimpleNamespace(**vars(old));setattr(new,field,'New '+field)
                self.session.try_get_media_properties_async.reset_mock()
                self.session.try_get_media_properties_async.side_effect=[old,new,new,new]
                emitted=[]
                def receive(event):
                    emitted.append(event);self.monitor.stop()
                with patch.object(self.messages,'put',side_effect=receive),patch('media._wait_for_refresh',new=AsyncMock()) as sleep:
                    self.monitor._run()
                self.assertEqual([call.args[1] for call in sleep.await_args_list],[.1])
                self.assertEqual(self.request.await_count,1)
                self.assertEqual(self.session.try_get_media_properties_async.await_count,4)
                self.assertEqual(len(emitted),1)
                snapshot=emitted[0][1]
                self.assertEqual((snapshot.title,snapshot.artist,snapshot.album),
                                 (new.title,new.artist,new.album_title))
                self.assertEqual((snapshot.base,snapshot.duration,snapshot.error),(12,180,''))

    def test_identity_is_copied_before_timeline_read(self):
        props=self.session.try_get_media_properties_async.return_value
        original_timeline=self.session.get_timeline_properties
        def changed_timeline():
            props.title='New song'
            return original_timeline()
        self.session.get_timeline_properties=changed_timeline
        events=self.run_until_success()
        self.assertEqual(self.session.try_get_media_properties_async.await_count,4)
        self.assertEqual([event[1].title for event in events],['New song'])

    def test_confirmation_failure_reconnects_and_recovers(self):
        props=self.session.try_get_media_properties_async.return_value
        self.session.try_get_media_properties_async.side_effect=[props,OSError('disconnected'),props,props]
        events=self.run_until_success()
        self.assertEqual(self.request.await_count,2)
        self.assertEqual([bool(event[1].error) for event in events],[True,False])
        self.assertEqual(events[-1][1].title,'Song')

    def test_confirmation_hang_is_bounded_and_recovers(self):
        props=self.session.try_get_media_properties_async.return_value
        actual_wait_for=asyncio.wait_for;timeouts=[];cancelled=[];attempts=0
        async def metadata():
            nonlocal attempts
            attempts+=1
            if attempts==2:
                try:await asyncio.Event().wait()
                finally:cancelled.append(True)
            return props
        async def shortened(awaitable,timeout):
            timeouts.append(timeout)
            return await actual_wait_for(awaitable,timeout=.02)
        self.session.try_get_media_properties_async.side_effect=metadata
        with patch('media.asyncio.wait_for',side_effect=shortened):events=self.run_until_success()
        self.assertEqual(cancelled,[True])
        self.assertEqual(timeouts,[4]*6)
        self.assertEqual(self.request.await_count,2)
        self.assertEqual([bool(event[1].error) for event in events],[True,False])

    def test_stop_during_confirmation_does_not_publish_or_sleep(self):
        props=self.session.try_get_media_properties_async.return_value
        attempts=0
        async def metadata():
            nonlocal attempts
            attempts+=1
            if attempts==2:self.monitor.stop()
            return props
        self.session.try_get_media_properties_async.side_effect=metadata
        with patch('media._wait_for_refresh',new=AsyncMock()) as sleep:self.monitor._run()
        self.assertEqual(attempts,2)
        self.assertTrue(self.messages.empty())
        sleep.assert_not_awaited()

    def test_stop_during_changed_track_retry_does_not_publish_or_read_again(self):
        old=self.session.try_get_media_properties_async.return_value
        new=SimpleNamespace(title='New',artist='Artist',album_title='Album')
        self.session.try_get_media_properties_async.side_effect=[old,new]
        async def stop(changed,delay):self.monitor.stop()
        with patch('media._wait_for_refresh',side_effect=stop) as sleep:self.monitor._run()
        self.assertEqual([call.args[1] for call in sleep.await_args_list],[.1])
        self.assertEqual(self.request.await_count,1)
        self.assertEqual(self.session.try_get_media_properties_async.await_count,2)
        self.assertTrue(self.messages.empty())


if __name__=='__main__':unittest.main()
