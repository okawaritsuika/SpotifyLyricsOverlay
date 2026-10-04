"""Sources may fill verified gaps without changing rows, clocks or old annotations."""
from copy import deepcopy
import gc
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from internal_reader import InternalReader
from lyric_annotations import merge_verified_annotations
from lyrics import Line
from media import Playback
from reader_bridge import ReaderBridge
from storage import Store
from web_lyrics import parse_table_text


ORIGINALS=('窓を開けて空を見る','花を描いて歩き出す','星を探して進んで行く')
PRIMARY='https://sidem-ko.fandom.com/ko/wiki/Example'
SECONDARY='https://namu.wiki/w/Example'


def lines(*translations):
    return tuple(Line(text,'',value) for text,value in zip(ORIGINALS,translations))


def table(rows,source):
    result=[]
    for row in rows:
        result.append(row.original)
        if row.korean:
            if source=='namu':result.append('한글 발음')
            result.append(row.korean)
    return '\n'.join(result)


class SourceComplementTests(unittest.TestCase):
    def test_multiple_sources_fill_only_blanks_and_report_contributors(self):
        current=lines('사용자 번역','','')
        merged,sources=merge_verified_annotations(current,[lines('다른 번역','꽃 번역',''),lines('','','별 번역')])
        self.assertEqual([row.korean for row in merged],['사용자 번역','꽃 번역','별 번역'])
        self.assertEqual(sources,(0,1))
        self.assertEqual([row.original for row in merged],list(ORIGINALS))
        self.assertEqual(current,lines('사용자 번역','',''))

    def test_conflicting_new_translations_remain_blank(self):
        merged,sources=merge_verified_annotations(lines('','',''),[lines('','꽃 번역',''),lines('','다른 꽃 번역','')])
        self.assertFalse(any(row.korean for row in merged))
        self.assertEqual(sources,())

    def test_missing_or_reordered_originals_cannot_supply_a_translation(self):
        supplied=lines('하늘','꽃','별')
        for candidate in (supplied[:-1],tuple(reversed(supplied))):
            merged,sources=merge_verified_annotations(lines('','',''),[candidate])
            self.assertFalse(any(row.korean for row in merged))
            self.assertEqual(sources,())

    def test_split_original_never_receives_a_whole_sentence_reading(self):
        source=(Line(ORIGINALS[0]+ORIGINALS[1],'whole reading','두 구절 번역'),Line(ORIGINALS[2],'last reading','별'))
        merged,_=merge_verified_annotations(lines('','',''),[source])
        self.assertEqual([row.reading for row in merged],['','','last reading'])
        self.assertEqual([row.korean for row in merged],['두 구절 번역','두 구절 번역','별'])

    def receive(self,extras):
        messages=queue.Queue();bridge=ReaderBridge(messages=messages)
        bridge.observe(Playback('Example','Unit','Album',playing=True))
        state=bridge.snapshot()
        value={'token':state['token'],'track':state['track'],'url':PRIMARY,
               'text':table(lines('기존 번역','',''),'sidem'),'match_mode':'title','supplements':extras}
        with patch('reader_bridge.parse_table_text',side_effect=lambda text,strict,source:
                   parse_table_text(text,reader=lambda row:'',strict=strict,source=source)):
            bridge.receive(value)
        _,payload=messages.get_nowait();track,received=payload[:2]
        bridge.set_receipt(track,'displayed',len(received),sum(bool(row.korean) for row in received))
        return bridge,received

    def test_bridge_parses_each_source_convention_and_retains_provenance(self):
        bridge,received=self.receive([{'url':SECONDARY,'text':table(lines('바꿀 번역','꽃 번역','별 번역'),'namu')}])
        self.assertEqual([row.korean for row in received],['기존 번역','꽃 번역','별 번역'])
        self.assertEqual(bridge.snapshot()['receipt']['translation_sources'],[PRIMARY,SECONDARY])
        self.assertEqual(bridge.snapshot()['receipt']['translated_lines'],3)

    def test_bridge_revalidates_url_title_and_whole_original_not_worker_claims(self):
        for url,rows in [('https://example.com/Example',lines('','꽃','별')),
                         ('https://namu.wiki/w/Other',lines('','꽃','별')),
                         (SECONDARY,lines('','꽃','별')[:-1])]:
            _,received=self.receive([{'url':url,'text':table(rows,'namu'),'match_mode':'matched','translated_lines':999}])
            self.assertEqual([row.korean for row in received],['기존 번역','',''])

    def test_original_only_receipt_does_not_report_translation_complete(self):
        bridge=ReaderBridge(messages=queue.Queue());bridge.observe(Playback('Example',playing=True))
        reader=InternalReader('.',bridge,queue.Queue());reader.enabled=True
        state=bridge.snapshot();reader.last_key=state['token']+':'+state['track'];reader.awaiting=True
        bridge.set_receipt(state['track'],'displayed',3,0)
        reader.observe(bridge.snapshot())
        self.assertIn('번역 미확인',reader.status)
        self.assertNotIn('연동 완료',reader.status)
        reader.awaiting=True;bridge.set_receipt(state['track'],'displayed',3,2)
        reader.observe(bridge.snapshot())
        self.assertIn('2/3',reader.status)


class ComplementOverlayTests(unittest.TestCase):
    def test_late_source_preserves_translation_originals_timing_and_follow_position(self):
        gc.collect()
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.save_settings({'online':False,'auto_audio':False,'web_auto':False,'reader_mode':'chrome','borderless':False})
            root=tk.Tk();root.withdraw();app=LyricsApp(root,store,start_services=False)
            try:
                playback=Playback('Example','Unit','Album',duration=180)
                app.set_playback(playback)
                app.receive_direct_lyrics(playback.key,{'source':'Spotify','lines':lines('기존 번역','',''),'times':[1,20,40]})
                before_direct=deepcopy(app.direct_result);before_timing=deepcopy(app.timing)
                app.manual_index=1;app.cursor=1;app.follow.set(False)
                app.receive_reader_lyrics(app.reader_bridge.snapshot()['track'],lines('덮어쓸 번역','새 꽃 번역',''),True)
                self.assertEqual([row.korean for row in app.display_lines],['기존 번역','새 꽃 번역',''])
                self.assertEqual([row.original for row in app.display_lines],list(ORIGINALS))
                self.assertEqual(app.direct_result,before_direct);self.assertEqual(app.timing,before_timing)
                self.assertEqual((app.manual_index,app.cursor,app.follow.get()),(1,1,False))
                self.assertEqual(app.reader_bridge.snapshot()['receipt']['translated_lines'],2)
                app.receive_reader_lyrics(app.reader_bridge.snapshot()['track'],(Line('無関係な言葉','','다른 곡'),),True)
                self.assertEqual([row.korean for row in app.display_lines],['기존 번역','새 꽃 번역',''])
                self.assertEqual(app.direct_result,before_direct);self.assertEqual(app.timing,before_timing)
            finally:app.close()
        gc.collect()


if __name__=='__main__':unittest.main()
