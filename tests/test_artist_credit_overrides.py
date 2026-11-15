from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import unittest
from artist_credit_overrides import ArtistCreditOverrides
from background_lyrics import BackgroundLyrics
from group_credits import performer_options
from lyric_performers import performer_credit
from media import Playback
from work_reuse import _ORCHESTRA_ALBUM

ROOT=Path(__file__).resolve().parents[1]


class CreditOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.value=json.loads((ROOT/'data/artist_credit_overrides.json').read_text(encoding='utf-8'))
        self.path=self.directory/'artist_credit_overrides.json'
        self.path.write_text(json.dumps(self.value,ensure_ascii=False),encoding='utf-8')
        self.credits=ArtistCreditOverrides(self.directory)

    def playback(self,entry):
        track=entry['track']
        return Playback(track['title'],track['artists'][0],track['album'],duration=track['duration'])

    def test_both_confirmed_mislabeled_digital_issues_display_yayoi(self):
        for entry in self.value['entries']:
            playback=self.playback(entry);identity=playback.key
            self.assertEqual(self.credits.get(playback),('高槻やよい (CV.仁後真耶子)',))
            self.assertEqual(playback.key,identity)

    def test_other_editions_cuts_artists_and_lengths_keep_their_original_display(self):
        playback=self.playback(self.value['entries'][0])
        for changes in ({'album':''},{'album':'Other'},{'duration':220},
                        {'title':'オハヨ○サンシャイン (Live)'},{'artist':'高槻やよい(CV:仁後真耶子)'}):
            self.assertEqual(self.credits.get(replace(playback,**changes)),())

    def test_confirmed_user_credit_precedes_catalog_and_spotify_cache(self):
        playback=self.playback(self.value['entries'][0])
        service=BackgroundLyrics('.',self.directory,queue.Queue())
        self.assertTrue(service.artist_metadata.put(playback,self.value['entries'][0]['track']))
        self.assertEqual(service.display_artists(playback),('高槻やよい (CV.仁後真耶子)',))
        self.assertEqual(service.artist_metadata.get(playback),('水瀬伊織(CV:釘宮理恵)',))

    def test_removing_the_local_correction_restores_catalog_display(self):
        playback=self.playback(self.value['entries'][0])
        service=BackgroundLyrics('.',self.directory,queue.Queue())
        self.assertEqual(service.display_artists(playback),('高槻やよい (CV.仁後真耶子)',))
        self.path.unlink()
        self.assertEqual(service.display_artists(playback),('水瀬伊織',))

    def test_yuki_aira_has_sidem_color_and_concert_singer_credits(self):
        service=BackgroundLyrics('.',self.directory,queue.Queue())
        tracks=(
            ('FairyTaleじゃいられない','堀江晶太',284.666,('結城アイラ',)),
            ('夜空を煌めく星のように','桑原 聖（Arte Refact）',301,('結城アイラ',)),
            ('Because','本多友紀（Arte Refact）',322,('結城アイラ','桜庭 薫 (CV.内田雄馬)')),
        )
        for title,artist,duration,expected in tracks:
            with self.subTest(title=title):
                playback=Playback(f'{title}（Day 第2幕「BRILLIANT STAGE」より） - Live',
                                  artist,_ORCHESTRA_ALBUM,duration=duration)
                names=service.display_artists(playback)
                self.assertEqual(names,expected)
                options,solo=performer_options(names,playback.album)
                self.assertEqual(options,expected)
                self.assertEqual(solo,expected if len(expected)==1 else ())
                text,spans=performer_credit(names,playback.album)
                self.assertEqual([text[start:end] for start,end,color in spans if color=='#85dbb5'],
                                 ['結城アイラ'])
        self.assertEqual(service.display_artists(replace(playback,album='Other Concert')),())
        self.assertEqual(service.display_artists(replace(playback,title=playback.title.replace('Day','Night'))),())
        self.assertEqual(performer_credit(('結城アイラ',),'Other Concert')[1],())
        self.assertEqual(performer_options(('結城アイラ',),'Other Concert')[1],())


if __name__=='__main__':unittest.main()
