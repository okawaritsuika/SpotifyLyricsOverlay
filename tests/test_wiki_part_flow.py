"""Color facts follow the validated lyric delivery, never changing its clock."""
from copy import deepcopy
from dataclasses import replace
import queue
import tempfile
import unittest
from unittest.mock import patch

from lyrics import Line
from lyric_parts import LyricParts
from media import Playback
from reader_bridge import ReaderBridge
from wiki_singers import extract_colored_parts
from wiki_part_store import save_wiki_parts
import test_background_overlay as background


TEXTS=background.TEXTS
LINES=tuple(Line(text,'','번역 '+str(i)) for i,text in enumerate(TEXTS))
HTML='''<table><tr><td colspan="2">캐릭터 이미지컬러</td></tr>
<tr><td style="background-color:#ee762e"></td><td>타마키</td></tr>
<tr><td style="background-color:#f7e78e"></td><td>이쿠</td></tr>
<tr><td style="background-color:white"></td><td>합창</td></tr>
<tr><td colspan="2" style="color:white">'''+''.join(
    f'<span style="color:{color}">{text}</span><br>발음 예시<br>번역 예시<br>'
    for text,color in zip(TEXTS,('#ee762e','#f7e78e','#ffffff')))+'''</td></tr></table>'''


class WikiPartFlowTests(unittest.TestCase):
    def setUp(self):
        self.messages=queue.Queue();self.bridge=ReaderBridge(messages=self.messages)
        self.playback=Playback('Test Song','TIntMe!','THE IDOLM@STER',duration=90,playing=True)
        self.bridge.observe(self.playback)
        self.bridge.set_reference(self.playback.key,TEXTS)

    def payload(self,**changes):
        state=self.bridge.snapshot()
        return dict(track=state['track'],token=state['token'],url='https://namu.wiki/w/Test%20Song',
                    text='\n'.join(f'{line.original}\n발음 예시\n{line.korean}' for line in LINES),
                    **{'table_html':HTML,**changes})

    def test_colored_source_delivered_only_after_normal_document_validation(self):
        self.assertEqual(self.bridge.receive(self.payload()),'queued')
        kind,payload=self.messages.get_nowait()
        self.assertEqual(kind,'reader_lyrics');self.assertEqual(len(payload),5)
        source=payload[-1][0]
        self.assertEqual(source['profile']['parts'][0]['singers'],['오오가미 타마키'])
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(save_wiki_parts(directory,self.playback,LINES,[source]))
            parts=LyricParts(directory,resource_path='missing-test-resource')
            self.assertEqual(parts.match(self.playback,LINES),
                (('오오가미 타마키',),('나카타니 이쿠',),('오오가미 타마키','나카타니 이쿠')))
            self.assertFalse(save_wiki_parts(directory,self.playback,LINES,[source]))
            self.assertEqual(parts.match(replace(self.playback,album='Other album'),LINES),((),(),()))

    def test_current_recording_credits_resolve_chorus_and_replace_incomplete_profile(self):
        playback=replace(self.playback,artist='ジュリア (CV.愛美)')
        self.bridge.observe(playback)
        self.bridge.set_reference(playback.key,TEXTS)
        html=HTML.replace('타마키','줄리아').replace('이쿠','메구미')
        self.bridge.receive(self.payload(table_html=html))
        before=self.messages.get_nowait()[1][-1][0]
        artists=['ジュリア (CV.愛美)','所 恵美 (CV.藤井ゆきよ)']
        self.bridge.set_overlay({'display_artists':artists})
        self.bridge.receive(self.payload(table_html=html))
        after=self.messages.get_nowait()[1][-1][0]
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(save_wiki_parts(directory,playback,LINES,[before],artists))
            self.assertTrue(save_wiki_parts(directory,playback,LINES,[after],artists))
            parts=LyricParts(directory,resource_path='missing-test-resource')
            self.assertEqual(len(parts.recordings),1)
            self.assertEqual(parts.match(playback,LINES),
                (('줄리아',),('토코로 메구미',),('줄리아','토코로 메구미')))
        # A track change must not reuse the previous track's display credits.
        changed=replace(playback,album='Different recording')
        self.bridge.observe(changed)
        self.bridge.set_reference(changed.key,TEXTS)
        self.bridge.receive(self.payload(table_html=html))
        stale=self.messages.get_nowait()[1][-1][0]
        self.assertEqual(stale['profile'],before['profile'])

    def test_mismatched_or_oversized_html_does_not_block_valid_text(self):
        for html in (HTML.replace(TEXTS[0],'別の曲を歌う'),'<table>'+'x'*200001):
            self.assertEqual(self.bridge.receive(self.payload(table_html=html)),'queued')
            _,payload=self.messages.get_nowait()
            self.assertEqual(len(payload),4)

    def test_wrong_originals_cannot_deliver_color_facts(self):
        self.bridge.set_reference(self.playback.key,['この原文だけを確かめる','もう一つの長い原文を見る'])
        bad=self.payload();bad['text']='別の曲を歌う\n다른 노래\n다른 노래야\n違う声が響く\n다른 목소리\n다른 목소리야'
        with self.assertRaises(ValueError):self.bridge.receive(bad)
        self.assertTrue(self.messages.empty())

    def test_optional_color_parse_failure_keeps_valid_lyrics(self):
        with patch('reader_bridge.extract_colored_parts',side_effect=RecursionError):
            self.assertEqual(self.bridge.receive(self.payload()),'queued')
        self.assertEqual(len(self.messages.get_nowait()[1]),4)

    def test_base_table_cannot_assign_group_parts_to_a_solo_or_other_version(self):
        source={'url':'https://namu.wiki/w/Test%20Song','lines':LINES,
                'profile':extract_colored_parts(HTML,LINES)}
        with tempfile.TemporaryDirectory() as directory:
            for playback in (replace(self.playback,title='Test Song (Solo Ver.)'),
                             replace(self.playback,artist='大神環 (CV.稲川英里)')):
                self.assertFalse(save_wiki_parts(directory,playback,LINES,[source]))

    def test_full_order_projects_singer_changes_across_split_rows(self):
        full=(Line(TEXTS[0]+TEXTS[1],'',''),LINES[2])
        source={'url':'https://namu.wiki/w/Test%20Song','lines':LINES,
                'profile':extract_colored_parts(HTML,LINES)}
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(save_wiki_parts(directory,self.playback,full,[source]))
            parts=LyricParts(directory,resource_path='missing-test-resource')
            self.assertEqual(parts.match(self.playback,LINES)[0],('오오가미 타마키',))
            self.assertEqual(parts.match(self.playback,full)[0],('오오가미 타마키','나카타니 이쿠'))

    def test_storage_limit_preserves_existing_credits(self):
        from pathlib import Path
        source={'url':'https://namu.wiki/w/Test%20Song','lines':LINES,
                'profile':extract_colored_parts(HTML,LINES)}
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(save_wiki_parts(directory,self.playback,LINES,[source]))
            path=Path(directory)/'wiki_lyric_parts.json';before=path.read_bytes()
            with patch('wiki_part_store.MAX_BYTES',len(before)+1):
                with self.assertRaises(ValueError):
                    save_wiki_parts(directory,replace(self.playback,album='THE IDOLM@STER extra release'),LINES,[source])
            self.assertEqual(path.read_bytes(),before)


class WikiPartOverlayTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive

    def test_live_delivery_shows_singers_and_retains_provider_clock(self):
        self.playback=replace(self.playback,artist='TIntMe!',album='THE IDOLM@STER')
        self.app.set_playback(self.playback)
        self.receive()
        before_clock=deepcopy(self.app.direct_result)
        before_timing=deepcopy(self.app.timing)
        original=tuple(line.original for line in self.app.display_lines)
        source={'url':'https://namu.wiki/w/Test%20Song','lines':LINES,
                'profile':extract_colored_parts(HTML,LINES)}
        state=self.app.reader_bridge.snapshot()
        self.app.receive_reader_lyrics(state['track'],LINES,False,state['token'],[source])
        self.assertEqual(self.app.current_line_singers(),('오오가미 타마키',))
        self.assertEqual(self.app.direct_result,before_clock)
        self.assertEqual(self.app.timing,before_timing)
        self.assertEqual(tuple(line.original for line in self.app.display_lines),original)
        self.assertEqual(self.app.translated_count,3)

    def test_stale_delivery_does_not_save_parts(self):
        self.receive();state=self.app.reader_bridge.snapshot()
        with patch('app.save_wiki_parts') as save:
            self.app.receive_reader_lyrics(state['track'],LINES,False,'old-token',[{}])
            save.assert_not_called()


if __name__=='__main__':unittest.main()
