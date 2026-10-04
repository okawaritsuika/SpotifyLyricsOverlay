"""Every timed Spotify phrase remains reachable after a differing wiki/cache."""
import unittest
from tests import test_background_overlay as fixtures
from lyrics import Line
from lyric_completion import restore_spotify_rows


class MissingSpotifyRowsTests(unittest.TestCase):
    setUp=fixtures.BackgroundOverlayTests.setUp
    tearDown=fixtures.BackgroundOverlayTests.tearDown
    provider=fixtures.BackgroundOverlayTests.provider
    receive=fixtures.BackgroundOverlayTests.receive
    translations=fixtures.BackgroundOverlayTests.translations
    position=fixtures.BackgroundOverlayTests.position

    def originals(self):
        return tuple(f'遠い空を見ながら歩く物語{i}' for i in range(12))

    def prepare(self):
        source=self.originals();wiki=list(source)
        wiki[3]=wiki[3].replace('空','雲')
        self.translations(wiki)
        return source,wiki

    def test_provider_typo_difference_does_not_skip_a_timed_phrase(self):
        source,_=self.prepare()
        self.app.timing['times'][1]=14
        self.app.timing['offset']=.5
        self.receive(texts=source,times=[10+3*i for i in range(12)])
        self.assertEqual([line.original for line in self.app.display_lines],list(source))
        self.assertEqual(self.app.timing['times'][1],14)
        self.assertEqual(self.app.timing['offset'],.5)
        for i in range(12):
            self.position(12+3*i)
            self.assertEqual(self.app.original_label.cget('text'),source[i])
        self.assertEqual(self.app.display_lines[2].korean,'번역 2')
        self.assertEqual(self.app.display_lines[3].korean,'')
        self.assertEqual(sum(t is not None for t in self.app.direct_times()),12)

    def test_late_wiki_cannot_remove_a_provider_phrase(self):
        source=self.originals();wiki=list(source);wiki[3]=wiki[3].replace('空','雲')
        self.receive(texts=source,times=[10+3*i for i in range(12)])
        self.translations(wiki)
        self.position(20)
        self.assertEqual(self.app.original_label.cget('text'),source[3])
        self.assertEqual(self.app.direct_times(),[10+3*i for i in range(12)])

    def test_legacy_cache_repairs_on_next_load_without_network(self):
        source,wiki=self.prepare()
        direct=self.provider(texts=source,times=[10+3*i for i in range(12)])
        self.app.lyric_cache.save(self.playback,self.app.display_lines,direct)
        self.app.live_lyrics.clear();self.app.select_song(None)
        self.app.restore_cached_lyrics()
        self.assertTrue(self.app.cache_hit)
        self.position(20)
        self.assertEqual(self.app.original_label.cget('text'),source[3])

    def test_complete_public_clock_cannot_hide_an_unmapped_spotify_phrase(self):
        source,wiki=self.prepare()
        self.receive(source='LRCLIB',texts=wiki,times=[10+3*i for i in range(12)])
        self.receive(texts=source,times=[10+3*i for i in range(12)])
        self.position(20)
        self.assertEqual(self.app.direct_result[2]['source'],'Spotify')
        self.assertEqual(self.app.original_label.cget('text'),source[3])

    def test_typo_elsewhere_does_not_erase_exact_split_sentence_translation(self):
        source=list(self.originals());wiki=list(source);wiki[3]=wiki[3].replace('空','雲')
        wiki[7:9]=[source[7]+source[8]]
        self.translations(wiki)
        self.receive(texts=source,times=[10+3*i for i in range(12)])
        self.assertEqual(self.app.display_lines[7].korean,'번역 7')
        self.assertEqual(self.app.display_lines[8].korean,'번역 7')
        self.assertEqual(self.app.display_lines[3].korean,'')

    def test_unmatched_manual_correction_is_not_discarded(self):
        source,wiki=self.prepare()
        timing={**self.app.timing,'times':[None,None,None,18]+[None]*8}
        self.assertIsNone(restore_spotify_rows(self.app.display_lines,
            self.provider(texts=source,times=[10+3*i for i in range(12)]),timing))

    def test_manual_start_inside_a_regrouped_spotify_row_is_not_moved_earlier(self):
        source,wiki=self.prepare()
        saved=list(self.app.display_lines)
        saved[:2]=[Line(wiki[0]+wiki[1][:5],'',''),Line(wiki[1][5:],'','')]
        timing={**self.app.timing,'times':[None,14]+[None]*10}
        self.assertIsNone(restore_spotify_rows(saved,
            self.provider(texts=source,times=[10+3*i for i in range(12)]),timing))

    def test_other_provider_and_unrelated_song_are_not_rewritten(self):
        source,_=self.prepare()
        for provider in (self.provider(source='LRCLIB',texts=source,times=[10+3*i for i in range(12)]),
                         self.provider(texts=tuple(f'別々の歌詞{i}' for i in range(12)),times=[10+3*i for i in range(12)])):
            self.assertIsNone(restore_spotify_rows(self.app.display_lines,provider,self.app.timing))

    def test_exact_split_or_merged_document_does_not_rewrite_grouping(self):
        source=self.originals()
        saved=(Line(source[0]+source[1],'','첫 문장'),)+tuple(Line(x,'','') for x in source[2:])
        self.assertIsNone(restore_spotify_rows(saved,
            self.provider(texts=source,times=[10+3*i for i in range(12)]),
            {'times':[None]*len(saved)}))


if __name__=='__main__':unittest.main()
