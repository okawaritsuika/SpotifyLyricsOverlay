import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from connection_dialog import ConnectionDialog


class ConnectionDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.app=SimpleNamespace(root=self.root,safe=lambda action:action(),connect_spotify=Mock(),start_public_lyrics=Mock(),
                                 stop_public_lyrics=Mock(),public_background=tk.BooleanVar(value=False),
                                 background_lyrics=SimpleNamespace(status='LRCLIB 검색 대기'),
                                 start_lyricsplus=Mock(),stop_lyricsplus=Mock(),lyricsplus_background=tk.BooleanVar(value=False),
                                 lyricsplus_job=SimpleNamespace(status='LyricsPlus 조회 꺼짐'),
                                 start_alsong=Mock(),stop_alsong=Mock(),alsong_background=tk.BooleanVar(value=False),
                                 alsong_job=SimpleNamespace(status='알송 조회 꺼짐'),
                                 open_reader_settings=Mock(),connection_status=Mock(
                                     return_value=('연결 방법을 선택하세요','Spotify에서 음악을 재생하면 곡을 확인합니다.')),
                                 connection_dialog=None)
        self.dialog=ConnectionDialog(self.app);self.app.connection_dialog=self.dialog
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_open_and_refresh_only_read_status_without_starting_a_connection(self):
        self.app.connection_status.return_value=('가사 연결됨','지금 재생 중인 곡의 가사를 표시합니다.')
        self.dialog.refresh()
        self.app.connect_spotify.assert_not_called()
        self.app.start_public_lyrics.assert_not_called()
        self.assertEqual(self.dialog.status_headline.cget('text'),'가사 연결됨')
        self.assertTrue(self.dialog.window.winfo_exists())

    def test_spotify_button_starts_only_spotify_connection(self):
        self.dialog.spotify_button.invoke()
        self.app.connect_spotify.assert_called_once_with()
        self.app.start_public_lyrics.assert_not_called()

    def test_public_button_discloses_consent_and_starts_only_public_lyrics(self):
        disclosure=self.dialog.public_disclosure.cget('text')
        self.assertIn('곡명과 가수를 LRCLIB(lrclib.net)에 보내 가사를 찾습니다.',disclosure)
        self.assertIn('동의',disclosure)
        self.dialog.public_button.invoke()
        self.app.start_public_lyrics.assert_called_once_with()
        self.app.connect_spotify.assert_not_called()

    def test_actions_and_disclosure_fit_at_minimum_size(self):
        self.dialog.window.geometry('560x780+20+20')
        self.root.update();self.root.update()
        window=self.dialog.window
        for widget in (self.dialog.status_headline,self.dialog.status_detail,
                       self.dialog.spotify_button,self.dialog.public_button,self.dialog.public_disclosure,
                       self.dialog.lyricsplus_button,self.dialog.lyricsplus_disclosure,self.dialog.lyricsplus_status,
                       self.dialog.alsong_button,self.dialog.alsong_disclosure,self.dialog.alsong_status,
                       self.dialog.advanced_button,self.dialog.close_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreater(widget.winfo_height(),10)
            self.assertGreaterEqual(widget.winfo_rootx(),window.winfo_rootx())
            self.assertGreaterEqual(widget.winfo_rooty(),window.winfo_rooty())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),window.winfo_rootx()+window.winfo_width())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),window.winfo_rooty()+window.winfo_height())
        self.assertGreaterEqual(self.dialog.public_disclosure.winfo_height(),self.dialog.public_disclosure.winfo_reqheight())
        self.assertGreaterEqual(self.dialog.lyricsplus_disclosure.winfo_height(),self.dialog.lyricsplus_disclosure.winfo_reqheight())

    def test_lyricsplus_button_discloses_destination_and_does_not_enable_other_sources(self):
        self.assertIn('lyricsplus.prjktla.my.id',self.dialog.lyricsplus_disclosure.cget('text'))
        self.dialog.lyricsplus_button.invoke()
        self.app.start_lyricsplus.assert_called_once_with()
        self.app.connect_spotify.assert_not_called();self.app.start_public_lyrics.assert_not_called()
        self.app.lyricsplus_background.set(True);self.dialog.refresh()
        self.dialog.lyricsplus_button.invoke()
        self.app.stop_lyricsplus.assert_called_once_with()

    def test_short_window_keeps_footer_visible_and_keyboard_can_reach_last_provider(self):
        self.dialog.window.geometry('560x500+20+20')
        self.root.update();self.root.update()
        canvas=self.dialog.canvas
        self.assertGreater(self.dialog.body.winfo_height(),canvas.winfo_height())
        self.dialog.reveal_focus(SimpleNamespace(widget=self.dialog.alsong_button))
        self.root.update()
        button=self.dialog.alsong_button
        self.assertGreaterEqual(button.winfo_rooty(),canvas.winfo_rooty())
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())
        footer=self.dialog.close_button
        self.assertGreater(footer.winfo_rooty(),canvas.winfo_rooty()+canvas.winfo_height())
        self.assertLessEqual(footer.winfo_rooty()+footer.winfo_height(),self.dialog.window.winfo_rooty()+500)

    def test_alsong_button_discloses_http_destination_and_toggles_independently(self):
        self.assertIn('lyrics.alsong.co.kr',self.dialog.alsong_disclosure.cget('text'))
        self.assertIn('HTTP',self.dialog.alsong_disclosure.cget('text'))
        self.dialog.alsong_button.invoke();self.app.start_alsong.assert_called_once_with()
        self.app.start_lyricsplus.assert_not_called();self.app.start_public_lyrics.assert_not_called()
        self.app.alsong_background.set(True);self.dialog.refresh()
        self.dialog.alsong_button.invoke();self.app.stop_alsong.assert_called_once_with()

    def test_enabled_public_button_can_stop_only_public_lookup(self):
        self.app.public_background.set(True);self.dialog.refresh()
        self.assertEqual(self.dialog.public_button.cget('text'),'공개 싱크 끄기')
        self.dialog.public_button.invoke()
        self.app.stop_public_lyrics.assert_called_once_with()
        self.app.start_public_lyrics.assert_not_called()
        self.app.connect_spotify.assert_not_called()

    def test_close_releases_dialog_reference(self):
        self.dialog.close_button.invoke()
        self.assertIsNone(self.app.connection_dialog)
        self.assertFalse(self.dialog.window.winfo_exists())


if __name__=='__main__':unittest.main()
