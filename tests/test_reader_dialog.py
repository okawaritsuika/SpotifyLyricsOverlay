import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from app import LyricsApp
from media import Playback
from storage import Store
from test_internal_reader import FakeJob,URL


class ReaderDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'transparent':False,'borderless':False,'reader_mode':'chrome'})
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
    def tearDown(self):self.app.close();self.temp.cleanup()

    def test_legacy_chrome_setting_uses_internal_reader_without_advanced_menu(self):
        self.assertEqual(self.app.reader_mode.get(),'internal')
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')
        self.assertFalse(hasattr(self.app,'open_reader_settings'))
        self.assertFalse(any('브라우저' in item['label'] or '고급' in item['label']
                             for item in self.app.menu_items() if item is not None))
        self.app.open_connection()
        self.assertFalse(hasattr(self.app.connection_dialog,'advanced_button'))

    def test_native_result_reaches_existing_overlay_without_browser_extension(self):
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        playback=Playback('Example','Artist','Album',10,100,True,time.monotonic())
        self.app.set_playback(playback)
        reader.observe(self.app.reader_bridge.snapshot());job=reader.job
        text='窓を開けて空を見る\n마도오 아케테 소라오 미루\n창문을 열어 하늘을 본다\n\n花を描いて道を歩く\n하나오 에가이테 미치오 아루쿠\n꽃을 그리고 길을 걷는다'
        reader.receive(job,{'kind':'lyrics','value':{'url':URL,'text':text,'match_mode':'title'}})
        self.app.tick()
        deadline=time.monotonic()+3
        while self.app.merge_reader_receipts and time.monotonic()<deadline:
            time.sleep(.005)
            self.app.tick()
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertIn('창문',self.app.korean_label.cget('text'))
        reader.observe(self.app.reader_bridge.snapshot())
        self.assertTrue(job.cancelled.is_set());self.assertIsNone(reader.job)
        self.assertFalse((self.store.directory/'library.json').exists())

    def test_fresh_install_defaults_to_internal_even_before_dependency_setup(self):
        self.app.close()
        self.store.save_settings({'online':False,'transparent':False,'borderless':False})
        self.root=tk.Tk();self.root.withdraw()
        with patch('internal_reader.available',return_value=False):
            self.app=LyricsApp(self.root,self.store,start_services=False)
        self.assertEqual(self.app.reader_mode.get(),'internal')
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')


if __name__=='__main__':unittest.main()
