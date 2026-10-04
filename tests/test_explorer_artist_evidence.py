import copy
import unittest
from unittest.mock import patch

import test_explorer_dialog as fixtures


class ArtistEvidenceUITests(unittest.TestCase):
    setUp=fixtures.ExplorerDialogTests.setUp
    tearDown=fixtures.ExplorerDialogTests.tearDown

    def corrected(self):
        song=self.songs[0]
        song.update(artists=['new generations'],artist_credit='new generations',
            artists_original=['田中秀和'],artist_credit_original='田中秀和',
            artist_source_kind='official_label',artist_source_urls=[
                'https://columbia.jp/prod-info/COZX-1151-4/',
                'https://columbia.jp/idolmaster/COZX-1151.html'])
        return song

    def evidence_callbacks(self):
        panel=self.dialog.song_detail
        for tag,callback in panel.navigation.items():
            ranges=panel.tag_ranges(tag)
            if ranges and '가창자 확인 근거' in panel.get(ranges[0],ranges[1]):yield callback

    def test_corrected_artist_has_explicit_original_credit_and_exact_source_links(self):
        song=self.corrected();before=copy.deepcopy(song)
        self.dialog.choose_song(song);self.root.update()
        text=self.dialog.song_detail.get('1.0','end')
        self.assertIn('이전 자료의 표기: 田中秀和',text)
        self.assertEqual(text.count('가창자 확인 근거 · columbia.jp'),2)
        with patch('explorer_dialog.open_url') as opened:
            for callback in self.evidence_callbacks():callback()
        self.assertEqual([call.args[0] for call in opened.call_args_list],song['artist_source_urls'])
        self.assertEqual(song,before)
        self.spotify.search.assert_not_called();self.spotify.play.assert_not_called()

    def test_other_songs_have_no_correction_rows_or_stale_source_links(self):
        self.dialog.choose_song(self.corrected())
        self.dialog.choose_song(self.songs[1]);self.root.update()
        text=self.dialog.song_detail.get('1.0','end')
        self.assertNotIn('이전 자료의 표기:',text)
        self.assertNotIn('가창자 확인 근거',text)
        self.assertEqual(list(self.evidence_callbacks()),[])

    def test_original_artist_list_fallback_and_invalid_sources_are_safe(self):
        song=self.corrected();song['artist_credit_original']=''
        song['artists_original']=['Previous One','Previous Two']
        song['artist_source_urls']=['http://example.test/source','javascript:alert(1)',
            'https://user:password@example.test/','https://[broken',None,
            'https:///missing-host','https://columbia.jp/idolmaster/COZX-1151.html']
        self.dialog.choose_song(song);self.root.update()
        text=self.dialog.song_detail.get('1.0','end')
        self.assertIn('이전 자료의 표기: Previous One · Previous Two',text)
        self.assertEqual(text.count('가창자 확인 근거'),1)
        with patch('explorer_dialog.webbrowser.open') as opened:
            for callback in self.evidence_callbacks():callback()
        opened.assert_called_once_with('https://columbia.jp/idolmaster/COZX-1151.html')

    def test_missing_official_kind_does_not_claim_verified_singer_evidence(self):
        song=self.corrected();song.pop('artist_source_kind')
        self.dialog.choose_song(song)
        self.assertNotIn('가창자 확인 근거',self.dialog.song_detail.get('1.0','end'))
        self.assertNotIn('이전 자료의 표기:',self.dialog.song_detail.get('1.0','end'))

    def test_no_valid_sources_does_not_add_an_empty_evidence_heading(self):
        song=self.corrected();song['artist_source_urls']=['http://example.test/source',None]
        self.dialog.choose_song(song)
        self.assertIn('이전 자료의 표기: 田中秀和',self.dialog.song_detail.get('1.0','end'))
        self.assertNotIn('가창자 확인 근거',self.dialog.song_detail.get('1.0','end'))
        self.assertEqual(list(self.evidence_callbacks()),[])


if __name__=='__main__':unittest.main()
