"""First-run and connection choices through the app, without external services."""
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from app import LyricsApp
from connection_dialog import ConnectionDialog
from lyrics import Line
from media import Playback
from storage import Store


class FakeSpotifyJob:
    def __init__(self,base,profile,messages,visible=False):
        self.visible=visible;self.closed=False;self.commands=[]

    def send(self,command):
        self.commands.append(command)

    def close(self):
        self.closed=True


class ConnectionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'public_background':False,'spotify_authenticated':False,
                                  'auto_audio':False,'web_auto':False,'borderless':False,'transparent':False,
                                  'reader_mode':'internal'})
        self.root=tk.Tk();self.root.withdraw()
        self.app=LyricsApp(self.root,self.store,start_services=False)
        self.service=self.app.background_lyrics
        self.service.job_factory=FakeSpotifyJob
        self.service.fetch=Mock(side_effect=AssertionError('Unexpected external lyrics lookup'))

    def tearDown(self):
        self.service.fetch.assert_not_called()
        self.app.close();self.temp.cleanup()

    def test_first_startup_opens_spotify_login_and_remembers_closing_it(self):
        self.app.settings.pop('spotify_authenticated')
        self.app.show_startup()
        job=self.service.job
        self.assertIsInstance(job,FakeSpotifyJob)
        self.assertTrue(job.visible)
        self.assertIsNone(self.app.connection_dialog)
        self.assertIsNone(self.app.editor)
        self.assertFalse(self.app.public_background.get())
        self.assertFalse(self.store.settings()['spotify_authenticated'])
        self.app.receive_background_lyrics(job,{'kind':'status','state':'login_required',
                                                'authenticated':False,'login_cancelled':True})
        self.app.receive_background_lyrics(job,{'kind':'finished','exit_code':0})
        self.assertIsNone(self.service.job)
        self.assertFalse(self.service.spotify_retry_at)
        self.assertFalse(self.service.spotify_requested)
        self.app.settings=self.store.settings()
        self.assertTrue(self.app.settings['connection_setup_seen'])
        self.assertFalse(self.app.settings['spotify_connected'])
        self.app.show_startup()
        self.assertIsNone(self.service.job)

    def test_installer_can_be_dismissed_or_continue_directly_to_login(self):
        with patch('app.frozen',return_value=True),patch('optional_runtime.installed',return_value=False) as installed:
            self.app.show_startup()
            dialog=self.app.browser_install_dialog
            self.assertIsNone(self.service.job)
            dialog.done.invoke()
            self.app.settings=self.store.settings()
            self.app.show_startup()
            self.assertIsNone(self.app.browser_install_dialog)
            self.app.show_startup(connect=True)
            dialog=self.app.browser_install_dialog
            installed.return_value=True
            dialog.refresh()
            self.assertEqual(dialog.download.cget('text'),'Spotify 로그인')
            dialog.download.invoke()
            self.assertIsNone(self.app.browser_install_dialog)
            self.assertTrue(self.service.job.visible)

    def test_developer_connection_button_reuses_existing_dialog(self):
        self.app.open_developer_mode()
        button=self.app.manual_sync_dialog.connection_button
        button.invoke()
        first=self.app.connection_dialog
        self.assertIsInstance(first,ConnectionDialog)
        button.invoke()
        self.assertIs(self.app.connection_dialog,first)
        first.close()
        self.assertIsNone(self.app.connection_dialog)

    def test_spotify_choice_waits_for_authenticated_status_and_never_opts_into_public_lookup(self):
        self.app.open_connection();self.app.connection_dialog.spotify_button.invoke()
        job=self.service.job
        self.assertIsInstance(job,FakeSpotifyJob);self.assertTrue(job.visible)
        self.assertTrue(self.store.settings()['spotify_connected'])
        self.assertTrue(self.app.online.get())
        self.assertFalse(self.store.settings()['public_background'])
        self.assertFalse(self.store.settings()['spotify_authenticated'])
        self.app.receive_background_lyrics(job,{'kind':'status','state':'connecting','text':'연결 중'})
        self.assertFalse(self.store.settings()['spotify_authenticated'])
        self.app.receive_background_lyrics(job,{'kind':'status','state':'ready','authenticated':True,'text':'연결됨'})
        self.assertTrue(self.store.settings()['spotify_authenticated'])
        self.assertTrue(self.service.spotify_authenticated)
        self.assertFalse(self.store.settings()['public_background'])

    def test_late_status_from_replaced_worker_cannot_set_or_clear_authentication(self):
        self.app.connect_spotify();old=self.service.job
        self.service.disconnect();self.app.connect_spotify();current=self.service.job
        self.assertIsNot(old,current)
        self.app.receive_background_lyrics(old,{'kind':'status','state':'ready','authenticated':True})
        self.assertFalse(self.service.spotify_authenticated)
        self.assertFalse(self.store.settings()['spotify_authenticated'])
        self.app.receive_background_lyrics(current,{'kind':'status','state':'ready','authenticated':True})
        self.app.receive_background_lyrics(old,{'kind':'status','state':'login_required','authenticated':False})
        self.assertTrue(self.service.spotify_authenticated)
        self.assertTrue(self.store.settings()['spotify_authenticated'])
        self.assertEqual(self.service.spotify_state,'ready')

    def test_public_choice_saves_consent_keeps_spotify_and_starts_lookup_immediately(self):
        self.app.connect_spotify();job=self.service.job
        self.app.open_connection()
        with patch.object(self.service,'disconnect',wraps=self.service.disconnect) as disconnect, \
                patch.object(self.service,'observe') as observe:
            self.app.connection_dialog.public_button.invoke()
        disconnect.assert_not_called()
        self.assertFalse(job.closed);self.assertIs(self.service.job,job)
        saved=self.store.settings()
        self.assertTrue(saved['public_background']);self.assertTrue(saved['online'])
        self.assertTrue(saved['spotify_connected'])
        observe.assert_called_once_with(self.app.playback,enabled=True,public_enabled=True,cached_sync=False,spotify_sync_present=False)

    def test_reconnecting_spotify_keeps_existing_public_consent(self):
        self.app.public_background.set(True)
        self.app.connect_spotify()
        self.assertTrue(self.store.settings()['public_background'])
        self.assertTrue(self.store.settings()['spotify_connected'])

    def test_connect_with_public_enabled_waits_for_spotify_before_external_lookup(self):
        self.app.set_playback(Playback('Song','Singer','Album',0,180,False,time.monotonic()))
        self.app.public_background.set(True)
        self.app.connect_spotify()
        self.assertTrue(self.service.spotify_pending)
        self.assertFalse(self.service.public_running)
        self.service.fetch.assert_not_called()

    def test_disabling_public_keeps_spotify_and_saved_lyrics(self):
        self.app.connect_spotify();job=self.service.job
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.receive_direct_lyrics(self.app.playback.key,{
            'source':'LRCLIB','lines':(Line('青い空を見る','',''),),'times':[10],'provider_id':'test'})
        self.app.public_background.set(True)
        self.app.stop_public_lyrics()
        self.assertFalse(self.store.settings()['public_background'])
        self.assertTrue(self.store.settings()['spotify_connected'])
        self.assertIs(self.service.job,job);self.assertFalse(job.closed)
        self.assertTrue(self.app.has_direct_sync())
        self.assertTrue(self.app.lyric_cache.load(self.app.playback))

    def test_no_song_prompts_user_to_play_music_without_technical_setup(self):
        self.app.online.set(True)
        headline,detail=self.app.connection_status()
        self.assertIn('노래',headline)
        self.assertIn('자동',detail)
        for term in ('API','세션','브라우저','Chrome','확장'):
            self.assertNotIn(term,headline+detail)

    def test_exhausted_public_error_is_not_shown_as_no_lyrics(self):
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.online.set(True);self.app.public_background.set(True)
        self.service.public_done=True;self.service.public_retry=2
        self.service.status='LRCLIB 조회 실패 · 싱크 존재 여부 미확인'
        headline,detail=self.app.connection_status()
        self.assertIn('실패',headline)
        self.assertIn('미확인',detail)
        self.assertNotIn('찾지 못',headline+detail)

    def test_public_rejection_is_visible_even_with_spotify_signed_in(self):
        self.app.connect_spotify()
        self.service.spotify_authenticated=True;self.service.spotify_state='not_found'
        self.app.public_background.set(True)
        self.service.status='LRCLIB 후보 시각 충돌'
        self.app.open_connection()
        self.assertEqual(self.app.connection_dialog.public_status.cget('text'),self.service.status)

    def test_lyricsplus_consent_is_separate_and_stopping_preserves_spotify(self):
        self.app.connect_spotify();job=self.service.job
        self.assertFalse(self.store.settings()['lyricsplus_background'])
        with patch.object(self.app.lyricsplus_job,'observe') as observe:
            self.app.start_lyricsplus()
        saved=self.store.settings()
        self.assertTrue(saved['lyricsplus_background']);self.assertFalse(saved['public_background'])
        self.assertTrue(observe.call_args.kwargs['enabled'])
        self.app.stop_lyricsplus()
        self.assertFalse(self.store.settings()['lyricsplus_background'])
        self.assertIs(self.service.job,job);self.assertFalse(job.closed)

    def test_authenticated_song_without_lyrics_does_not_request_another_login(self):
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.connect_spotify()
        self.app.receive_background_lyrics(self.service.job,{
            'kind':'status','state':'not_found','authenticated':True,'text':'이 곡의 가사를 찾지 못했습니다.'})
        headline,detail=self.app.connection_status()
        self.assertIn('가사를 찾지 못',headline)
        self.assertIn('다른 곡',detail)
        self.assertNotIn('로그인해',headline+detail)
        self.assertNotIn('로그인이 필요',headline+detail)
        self.assertTrue(self.store.settings()['spotify_authenticated'])

    def test_received_originals_stay_visible_while_connection_error_is_reported_in_detail(self):
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.connect_spotify()
        for stamp in (None,10):
            with self.subTest(existing_sync=stamp is not None):
                self.app.receive_direct_lyrics(self.app.playback.key,{
                    'source':'LRCLIB','lines':(Line('青い空を見る','',''),),'times':[stamp],'provider_id':'test'})
                self.assertTrue(self.app.is_bound())
                self.app.receive_background_lyrics(self.service.job,{
                    'kind':'status','state':'error','authenticated':True,'text':'곡 검색 응답 시간이 초과됐습니다.'})
                headline,detail=self.app.connection_status()
                self.assertIn('원문 가사',headline)
                self.assertIn('표시',headline)
                self.assertIn('로그인은 완료',detail)
                self.assertIn('곡 검색 응답 시간이 초과',detail)
                self.assertNotIn('로그인해',headline+detail)
                self.assertNotIn('로그인이 필요',headline+detail)
                self.assertEqual(self.app.original_label.cget('text'),'青い空を見る')

    def test_received_originals_do_not_need_translation_or_sync_or_a_running_worker(self):
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.connect_spotify()
        job=self.service.job
        self.app.receive_background_lyrics(job,{'kind':'lyrics','key':self.service.request_key,
            'lines':[{'text':'青い空を見る','time':None},{'text':'遠い星を探す','time':None}]})
        self.app.receive_background_lyrics(job,{'kind':'finished','exit_code':1})
        kind,result=self.app.messages.get(timeout=3)
        self.assertEqual(kind,'merged_lyrics')
        self.app.receive_merged_lyrics(*result)
        self.app.render()
        headline,detail=self.app.connection_status()
        self.assertIn('원문 가사',headline)
        self.assertEqual(self.app.original_label.cget('text'),'青い空を見る')
        self.assertEqual(self.app.korean_label.cget('text'),'')
        self.assertFalse(self.app.has_direct_sync())
        self.assertTrue(detail)

    def test_authenticated_connecting_status_does_not_request_login_again(self):
        self.app.set_playback(Playback('Test Song','Singer','Album',12,90,True,time.monotonic()))
        self.app.connect_spotify()
        self.app.receive_background_lyrics(self.service.job,{
            'kind':'status','state':'connecting','authenticated':True,'text':'현재 곡 확인 중'})
        headline,detail=self.app.connection_status()
        self.assertIn('곡',detail)
        self.assertNotIn('로그인을 마쳐',headline+detail)
        self.assertNotIn('로그인해',headline+detail)


if __name__=='__main__':unittest.main()
