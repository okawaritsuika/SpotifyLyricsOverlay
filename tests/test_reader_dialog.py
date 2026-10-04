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

    def test_mode_is_saved_and_dialog_controls_fit(self):
        self.app.open_reader_settings();dialog=self.app.reader_dialog
        dialog.window.geometry('520x350+20+20')
        self.app.reader_mode.set('internal')
        with patch('internal_reader.available',return_value=True):self.app.change_reader_mode()
        self.root.update();self.root.update()
        self.assertEqual(self.store.settings()['reader_mode'],'internal')
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')
        for button in (dialog.retry,dialog.original,dialog.choose,dialog.url_entry,dialog.url_connect,dialog.sidem_find):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),dialog.window.winfo_rootx()+dialog.window.winfo_width())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),dialog.window.winfo_rooty()+dialog.window.winfo_height())
        self.assertGreater(dialog.choices.winfo_height(),45)
        dialog.close();self.assertIsNone(self.app.reader_dialog)

    def test_exact_document_url_can_be_connected_when_discovery_has_no_candidates(self):
        self.app.reader_mode.set('internal');self.app.reader_bridge.backend='internal'
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        self.app.set_playback(Playback('ESPADA','SHADE OF SPADE','Album',10,214,True,time.monotonic()))
        self.app.open_reader_settings();dialog=self.app.reader_dialog
        url='https://namu.wiki/w/ESPADA(%EC%95%84%EC%9D%B4%EB%8F%8C%20%EB%A7%88%EC%8A%A4%ED%84%B0)#s-3'
        self.assertEqual(reader.choices,[])
        dialog.document_url.set('  '+url+'  ');dialog.connect_url()
        self.assertEqual(reader.connection_options,(False,url))
        self.assertIsNotNone(reader.job)
        self.assertEqual(self.app.reader_bridge.snapshot()['document_url'],'')

    def test_empty_or_foreign_document_url_does_not_start_a_reader_job(self):
        self.app.reader_mode.set('internal')
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        self.app.set_playback(Playback('ESPADA','Artist','Album',10,214,True,time.monotonic()))
        self.app.open_reader_settings();dialog=self.app.reader_dialog
        for url in ('','https://example.com/w/ESPADA'):
            with self.subTest(url=url):
                dialog.document_url.set(url)
                with self.assertRaises(ValueError):dialog.connect_url()
                self.assertIsNone(reader.job)

    def test_chrome_mode_disables_internal_document_url_controls(self):
        self.app.open_reader_settings();dialog=self.app.reader_dialog
        self.assertEqual(dialog.url_entry.cget('state'),'disabled')
        self.assertEqual(dialog.url_connect.cget('state'),'disabled')
        self.assertEqual(dialog.sidem_find.cget('state'),'disabled')

    def test_sidem_button_searches_current_title_without_approving_unknown_lyrics(self):
        self.app.reader_mode.set('internal');self.app.reader_bridge.backend='internal'
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        self.app.set_playback(Playback('Example Song','Artist','Album',10,100,True,time.monotonic()))
        self.app.open_reader_settings();dialog=self.app.reader_dialog
        with patch.object(reader,'connect') as connect:dialog.find_sidem()
        state=connect.call_args.args[0]
        self.assertEqual(state['suggested_document_url'],'https://sidem-ko.fandom.com/ko/wiki/Example_Song')
        self.assertEqual(state['document_url'],'')
        self.assertNotIn('selected_url',connect.call_args.kwargs)
        self.assertEqual(self.app.reader_bridge.snapshot()['document_url'],'')

    def test_native_result_reaches_existing_overlay_without_browser_extension(self):
        self.app.reader_mode.set('internal');self.app.reader_bridge.backend='internal'
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        playback=Playback('Example','Artist','Album',10,100,True,time.monotonic())
        self.app.set_playback(playback)
        reader.observe(self.app.reader_bridge.snapshot());job=reader.job
        text='窓を開けて空を見る\n마도오 아케테 소라오 미루\n창문을 열어 하늘을 본다\n\n花を描いて道を歩く\n하나오 에가이테 미치오 아루쿠\n꽃을 그리고 길을 걷는다'
        reader.receive(job,{'kind':'lyrics','value':{'url':URL,'text':text,'match_mode':'title'}})
        self.app.tick()
        self.assertEqual(self.app.reader_bridge.snapshot()['receipt']['status'],'displayed')
        self.assertIn('창문',self.app.korean_label.cget('text'))
        reader.observe(self.app.reader_bridge.snapshot())
        self.assertTrue(job.cancelled.is_set());self.assertIsNone(reader.job)
        self.assertFalse((self.store.directory/'library.json').exists())

    def test_switch_to_chrome_cancels_native_worker_and_preserves_saved_choice(self):
        reader=self.app.internal_reader;reader.enabled=True;reader.job_factory=FakeJob
        self.app.set_playback(Playback('Example','Artist','Album',10,100,True,time.monotonic()))
        reader.observe(self.app.reader_bridge.snapshot());job=reader.job
        self.app.reader_mode.set('chrome');self.app.change_reader_mode()
        self.assertTrue(job.cancelled.is_set());self.assertFalse(reader.enabled)
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'chrome')
        self.assertEqual(self.store.settings()['reader_mode'],'chrome')

    def test_uninstalled_native_mode_preserves_native_choice_and_reports_setup(self):
        self.app.reader_mode.set('internal')
        with patch('internal_reader.available',return_value=False):
            with self.assertRaises(ValueError):self.app.change_reader_mode()
        self.assertEqual(self.app.reader_mode.get(),'internal')
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')
        self.assertEqual(self.store.settings()['reader_mode'],'internal')
        self.assertIn('setup-browser.ps1',self.app.internal_reader.status)
        self.assertFalse(self.app.internal_reader.enabled)

    def test_fresh_install_defaults_to_internal_even_before_dependency_setup(self):
        self.app.close()
        self.store.save_settings({'online':False,'transparent':False,'borderless':False})
        self.root=tk.Tk();self.root.withdraw()
        with patch('internal_reader.available',return_value=False):
            self.app=LyricsApp(self.root,self.store,start_services=False)
        self.assertEqual(self.app.reader_mode.get(),'internal')
        self.assertEqual(self.app.reader_bridge.snapshot()['reader_backend'],'internal')


if __name__=='__main__':unittest.main()
