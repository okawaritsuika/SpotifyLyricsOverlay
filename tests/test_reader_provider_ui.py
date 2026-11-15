"""Queued provider tokens are checked before display and document learning."""
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyric_merge import merge_lyrics
from lyrics import Line
from media import Playback
from storage import Store


class ReaderProviderUITests(unittest.TestCase):
    def test_reader_switch_excludes_document_already_in_merge_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.save_settings({'online':False,'auto_audio':False,'web_auto':False,'reader_mode':'chrome'})
            root=tk.Tk();root.withdraw();app=LyricsApp(root,store,start_services=False)
            entered,release=threading.Event(),threading.Event()
            title='Provider Lifecycle Fixture'
            old_url='https://namu.wiki/w/Provider%20Lifecycle%20Fixture#s-1'
            new_url='https://namu.wiki/w/Provider%20Lifecycle%20Fixture#s-2'

            def submit(url,translation):
                state=app.reader_bridge.snapshot()
                lines=(Line('窓を開けて空を見る','',translation),)
                app.receive_reader_lyrics(state['track'],lines,False,state['token'],(),((url,title,lines),))

            def delay_first_merge(*args):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(5):raise ValueError('The reader test never released its merge')
                return merge_lyrics(*args)

            try:
                app.set_playback(Playback(title=title,artist='Singer',album='Album',
                                          duration=90,playing=True,captured=time.monotonic()))
                with patch('lyric_merge.merge_lyrics',side_effect=delay_first_merge):
                    submit(old_url,'이전 제공자 번역')
                    self.assertTrue(entered.wait(3))
                    app.reader_mode.set('internal')
                    with patch('internal_reader.available',return_value=True):app.change_reader_mode()
                    submit(new_url,'현재 제공자 번역')
                    release.set()
                    deadline=time.monotonic()+3
                    while app.merge_reader_receipts and time.monotonic()<deadline:
                        app.tick()
                        time.sleep(.005)
                    self.assertEqual(app.display_lines[0].korean,'현재 제공자 번역')
                    self.assertEqual([value['provider_id'] for value in app.current_lyric_merge['snapshots']],[new_url])
            finally:
                release.set()
                app.close()

    def test_old_provider_queue_cannot_apply_or_consume_new_provider_document(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.save_settings({'online':False,'auto_audio':False,'reader_mode':'chrome'})
            root=tk.Tk();root.withdraw();app=LyricsApp(root,store,start_services=False)
            try:
                app.set_playback(Playback(title='Provider Lifecycle Fixture',artist='Singer',album='Album',
                                          duration=90,playing=True,captured=time.monotonic()))
                bridge=app.reader_bridge
                def receive(url,translation):
                    bridge.observe(app.playback)
                    state=bridge.snapshot()
                    with patch('reader_bridge.parse_table_text',return_value=(Line('窓を開けて空を見る','',translation),)):
                        self.assertEqual(bridge.receive({'token':state['token'],'track':state['track'],'text':'synthetic',
                                                        'url':url,'user_selected':True}),'queued')
                receive('https://namu.wiki/w/Old','이전 제공자 번역')
                app.reader_mode.set('internal')
                with patch('internal_reader.available',return_value=True):app.change_reader_mode()
                receive('https://namu.wiki/w/New','현재 제공자 번역')
                with patch.object(app.live_lyrics,'receive',wraps=app.live_lyrics.receive) as accepted:
                    app.tick()
                    deadline=time.monotonic()+3
                    while app.merge_reader_receipts and time.monotonic()<deadline:
                        time.sleep(.005)
                        app.tick()
                self.assertEqual(accepted.call_count,1)
                self.assertEqual(app.display_lines[0].korean,'현재 제공자 번역')
                state=bridge.snapshot()
                self.assertEqual(state['receipt']['status'],'displayed')
                self.assertEqual(bridge.document_urls[state['track']],'https://namu.wiki/w/New')
                self.assertEqual(bridge.pending_documents,[])
            finally:app.close()


if __name__=='__main__':unittest.main()
