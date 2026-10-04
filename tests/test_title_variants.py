import json
from pathlib import Path
import queue
import shutil
import subprocess
import unittest
from urllib.parse import quote

from media import Playback
from lyrics import normalized
from reader_bridge import ReaderBridge,recording_base,same_lyric_title


BASE=Path(__file__).resolve().parent.parent
CASES=json.loads((BASE/'tests/title_variants.json').read_text(encoding='utf-8'))
TEXT='新しい窓を開ける\n아타라시이 마도오 아케루\n새로운 창문을 열다\n静かな空を見上げる\n시즈카나 소라오 미아게루\n조용한 하늘을 올려다보다'


class TitleVariantsTests(unittest.TestCase):
    def test_width_and_composed_forms_match_in_both_directions_without_losing_kana_meaning(self):
        for left,right,expected in CASES['widths']:
            for name,title in ((left,right),(right,left)):
                with self.subTest(name=name,title=title):
                    self.assertEqual(same_lyric_title(name,title),expected)
                    self.assertEqual(normalized(name)==normalized(title),expected)

    def test_width_forms_reach_the_receiver_but_distinct_kana_and_singer_versions_do_not(self):
        for left,right,expected in CASES['widths']:
            for name,title in ((left,right),(right,left)):
                with self.subTest(name=name,title=title):
                    bridge=ReaderBridge(messages=queue.Queue());bridge.observe(Playback(title=title,playing=True))
                    state=bridge.snapshot()
                    value={'token':state['token'],'track':state['track'],'url':'https://namu.wiki/w/'+quote(name,safe=''),
                           'text':TEXT,'match_mode':'unique_title'}
                    if expected:self.assertEqual(bridge.receive(value),'queued')
                    else:
                        with self.assertRaises(ValueError):bridge.receive(value)

    def test_common_suffixes_and_meaningful_titles(self):
        for title,base in CASES['versions']:
            with self.subTest(title=title):self.assertEqual(recording_base(title),base)
        for title in CASES['preserved']:
            with self.subTest(preserved=title):
                # NFKC is used for comparison, never for Spotify's track identity.
                import unicodedata
                self.assertEqual(recording_base(title),unicodedata.normalize('NFKC',title))
        for name,title,expected in CASES['comparisons']:
            with self.subTest(name=name,title=title):self.assertEqual(same_lyric_title(name,title),expected)

    @unittest.skipUnless(shutil.which('node'),'Node.js is needed to compare both reader backends')
    def test_javascript_and_python_share_the_same_title_decisions(self):
        script="""const m=require('./extension/document_match.js');
const c=JSON.parse(require('node:fs').readFileSync('tests/title_variants.json','utf8'));
console.log(JSON.stringify({bases:[...c.versions.map(x=>x[0]),...c.preserved].map(m.recordingBase),
matches:c.comparisons.map(([name,title])=>m.sameLyricTitle('https://namu.wiki/w/'+encodeURIComponent(name),title)),
widths:c.widths.flatMap(([left,right])=>[[left,right],[right,left]]).map(([name,title])=>({
title:m.sameLyricTitle('https://namu.wiki/w/'+encodeURIComponent(name),title),
text:m.lyricKey(name)===m.lyricKey(title)}))}));"""
        result=subprocess.run(['node','-e',script],cwd=BASE,capture_output=True,text=True,encoding='utf-8',check=True)
        actual=json.loads(result.stdout)
        self.assertEqual(actual['bases'],[recording_base(title) for title in [*[row[0] for row in CASES['versions']],*CASES['preserved']]])
        self.assertEqual(actual['matches'],[same_lyric_title(name,title) for name,title,_ in CASES['comparisons']])
        self.assertEqual(actual['widths'],[{'title':same_lyric_title(name,title),'text':normalized(name)==normalized(title)}
                                         for left,right,_ in CASES['widths'] for name,title in ((left,right),(right,left))])

    def test_all_known_versions_reach_the_receiver_with_evidence(self):
        for title,base in CASES['versions']:
            for mode in ('metadata','unique_title'):
                with self.subTest(title=title,mode=mode):
                    bridge=ReaderBridge(messages=queue.Queue());bridge.observe(Playback(title=title,playing=True))
                    state=bridge.snapshot()
                    value={'token':state['token'],'track':state['track'],'url':'https://namu.wiki/w/'+quote(base,safe=''),'text':TEXT,'match_mode':mode}
                    self.assertEqual(bridge.receive(value),'queued')

    def test_unknown_parentheses_need_matching_lyrics_and_conflicting_lyrics_are_rejected(self):
        for title in ('Crossing! (Another Story)','Crossing! (如月千早 Ver.)'):
            bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title=title,playing=True);bridge.observe(playback)
            state=bridge.snapshot()
            value={'token':state['token'],'track':state['track'],'url':'https://namu.wiki/w/Crossing!','text':TEXT,'match_mode':'unique_title'}
            if 'Another Story' in title:
                with self.assertRaises(ValueError):bridge.receive(value)
            bridge.set_reference(playback.key,['different reference phrase','another unrelated phrase'])
            with self.assertRaises(ValueError):bridge.receive(value)
            bridge.references=()
            bridge.set_reference(playback.key,['different reference phrase','静かな空を見上げる'])
            # Two distinct source phrases are required; a single match is insufficient.
            with self.assertRaises(ValueError):bridge.receive(value)
            bridge.references=('新しい窓を開ける','静かな空を見上げる')
            self.assertEqual(bridge.receive(value),'queued')

    def test_arrangement_with_a_parenthetical_subtitle_requires_lyrics_or_selection_for_the_short_title(self):
        for title,base in (
            ('アイ NEED YOU（FOR WONDERFUL STORY） - THE IDOLM@STER 20th Anniversary Piano Arrange Version','アイ NEED YOU'),
            ('Synthetic Song [Another Story] - Anniversary Piano Arrange Version','Synthetic Song'),
        ):
            with self.subTest(title=title):
                bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title=title,playing=True);bridge.observe(playback)
                state=bridge.snapshot()
                value={'token':state['token'],'track':state['track'],'url':'https://namu.wiki/w/'+quote(base,safe=''),
                       'text':TEXT,'match_mode':'metadata'}
                self.assertFalse(same_lyric_title(base,title))
                for mode in ('metadata','unique_title'):
                    with self.assertRaises(ValueError):bridge.receive({**value,'match_mode':mode})
                bridge.set_reference(playback.key,['新しい窓を開ける','静かな空を見上げる'])
                self.assertEqual(bridge.receive(value),'queued')
                bridge.references=('different reference phrase','another unrelated phrase')
                with self.assertRaises(ValueError):bridge.receive(value)
                self.assertEqual(bridge.receive({**value,'user_selected':True}),'queued')

    def test_subtitle_alias_receiver_revalidates_title_artist_and_candidate_relation(self):
        title='アイ NEED YOU（FOR WONDERFUL STORY） - THE IDOLM@STER 20th Anniversary Piano Arrange Version'
        bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title=title,artist='Example Artist (CV. Someone)',playing=True)
        bridge.observe(playback);state=bridge.snapshot()
        evidence={'title':recording_base(title),'artist':'Example Artist'}
        value={'token':state['token'],'track':state['track'],'url':'https://namu.wiki/w/'+quote('アイ NEED YOU',safe=''),
               'text':TEXT,'match_mode':'metadata_alias','title_alias':evidence}
        for invalid in (None,True,{},{'title':evidence['title']},
                        {**evidence,'title':'アイ NEED YOU'},{**evidence,'artist':'Other Artist'}):
            with self.subTest(evidence=invalid):
                with self.assertRaises(ValueError):bridge.receive({**value,'title_alias':invalid})
        for name in ('Unrelated Song','アイ NEED YOU (Other Singer Ver.)'):
            with self.assertRaises(ValueError):bridge.receive({**value,'url':'https://namu.wiki/w/'+quote(name,safe='')})
        self.assertEqual(bridge.receive(value),'queued')
        bridge.set_reference(playback.key,['different source phrase one','different source phrase two'])
        with self.assertRaises(ValueError):bridge.receive(value)

    def test_clear_reference_only_affects_the_current_track_references(self):
        bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title='Current Song',artist='Artist',album='Album')
        bridge.observe(playback);bridge.set_reference(playback.key,['existing source phrase'])
        track=bridge.snapshot()['track'];bridge.document_urls[track]='https://namu.wiki/w/Current%20Song'
        before=bridge.snapshot()
        bridge.clear_reference(Playback(title='Other Song').key)
        self.assertEqual(bridge.snapshot(),before)
        bridge.clear_reference(playback.key)
        self.assertEqual(bridge.snapshot(),{**before,'reference_lines':()})


if __name__=='__main__':unittest.main()
