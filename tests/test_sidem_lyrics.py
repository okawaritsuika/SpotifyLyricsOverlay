"""Synthetic lines verify provider boundaries without bundling third-party lyrics."""
import queue
import unittest

from browser_worker import connect_page,document_key
from media import Playback
from reader_bridge import ReaderBridge,same_document_url
from web_lyrics import document_name,parse_html,parse_table_text,sidem_url,validate_url

PAIR='窓を開ける\n창문을 열다\nOpen the window\n花を描く\n꽃을 그리다\n夜が来る\n窓を開ける\n창문을 열다'


class SideMWikiTests(unittest.TestCase):
    def test_only_named_korean_wiki_documents_are_allowed(self):
        url=sidem_url('Example Song')
        self.assertEqual(url,'https://sidem-ko.fandom.com/ko/wiki/Example_Song')
        self.assertEqual(validate_url(url+'#가사'),url)
        self.assertEqual(document_name(url),'Example Song')
        for bad in ('https://other.fandom.com/ko/wiki/Example','https://sidem-ko.fandom.com.evil.test/ko/wiki/Example',
                    'http://sidem-ko.fandom.com/ko/wiki/Example','https://user@sidem-ko.fandom.com/ko/wiki/Example',
                    'https://sidem-ko.fandom.com:8000/ko/wiki/Example','https://sidem-ko.fandom.com/wiki/Example',
                    'https://sidem-ko.fandom.com/ko/wiki/'):
            with self.subTest(url=bad),self.assertRaises(ValueError):validate_url(bad)

    def test_host_is_part_of_document_identity_and_mediawiki_spaces_are_aliases(self):
        self.assertTrue(same_document_url(sidem_url('Example Song'),sidem_url('Example Song').replace('_','%20')))
        self.assertFalse(same_document_url(sidem_url('Example'),'https://namu.wiki/w/Example'))
        self.assertNotEqual(document_key(sidem_url('Example')),document_key('https://namu.wiki/w/Example'))

    def test_bilingual_lines_keep_order_repeated_refrains_and_missing_meanings(self):
        lines=parse_table_text(PAIR,reader=lambda _: 'ひらがな',strict=False,source='sidem')
        self.assertEqual([x.original for x in lines],['窓を開ける','Open the window','花を描く','夜が来る','窓を開ける'])
        self.assertEqual([x.korean for x in lines],['창문을 열다','','꽃을 그리다','','창문을 열다'])
        self.assertTrue(all(x.reading=='ひらがな' for x in lines))
        self.assertTrue(all(not x.korean for x in parse_table_text('窓を開ける\n마도오 아케루',reader=str,strict=False)))

    def test_three_row_tables_keep_pronunciation_separate_for_both_sources(self):
        for source in ('namu','sidem'):
            lines=parse_table_text('窓を開ける\n마도오 아케루\n창문을 열다',reader=lambda _: 'まど',source=source)
            self.assertEqual((lines[0].reading,lines[0].korean),('まど','창문을 열다'))

    def test_bilingual_html_selects_lyrics_not_credits_or_next_section(self):
        html='<h1>Example Song</h1><table><tr><td>Japanese credits</td></tr></table><h2><span id="가사">가사</span></h2>'
        html+='<table><tr><th>가사</th></tr><tr><td><p>窓を開ける<br>창문을 열다</p><p>花を描く<br>꽃을 그리다</p></td></tr></table>'
        html+='<h2>기타</h2><table><tr><td>Different original<br>다른 번역</td></tr></table>'
        lines,title=parse_html(html,'Example Song',reader=str,source='sidem')
        self.assertEqual(title,'Example Song')
        self.assertEqual([x.korean for x in lines],['창문을 열다','꽃을 그리다'])

    def bridge(self,title='Example Song',album=''):
        bridge=ReaderBridge(messages=queue.Queue())
        playback=Playback(title=title,artist='315 ALLSTARS',album=album,playing=True)
        bridge.observe(playback)
        return bridge,playback

    def test_album_brand_suggests_source_without_changing_recording_identity(self):
        title='Beyond The Dream - GROWING SIGN@L Ver.'
        bridge,playback=self.bridge(title,'THE IDOLM@STER SideM GROWING SIGN@L 01 Growing Smiles！')
        state=bridge.snapshot()
        self.assertEqual(state['title'],title)
        self.assertEqual(state['document_url'],'')
        self.assertEqual(state['suggested_document_url'],sidem_url(title))
        for album in ('SideM cover collection','THE IDOLM@STER MILLION LIVE!',''):
            other,_=self.bridge(title,album)
            self.assertNotIn('suggested_document_url',other.snapshot())

    def test_bilingual_reader_delivery_uses_original_reference_and_rejects_mismatch(self):
        bridge,playback=self.bridge()
        bridge.set_reference(playback.key,['窓を開けて空を見る','花を描いて道を歩く'])
        state=bridge.snapshot()
        value={'token':state['token'],'track':state['track'],'url':sidem_url('Example Song'),
               'text':'違う長い原文ですね\n다른 내용\n別の長い原文ですね\n별개 내용','match_mode':'title'}
        with self.assertRaisesRegex(ValueError,'일치하는 구절'):bridge.receive(value)
        self.assertTrue(bridge.messages.empty())
        value['text']='窓を開けて空を見る\n창문을 열어 하늘을 본다\n花を描いて道を歩く\n꽃을 그리고 길을 걷는다'
        self.assertEqual(bridge.receive(value),'queued')
        kind,(track,lines,supplement,token)=bridge.messages.get_nowait()
        self.assertEqual((kind,track),('reader_lyrics',state['track']))
        self.assertIs(supplement,False)
        self.assertEqual(token,state['token'])
        self.assertEqual([x.korean for x in lines],['창문을 열어 하늘을 본다','꽃을 그리고 길을 걷는다'])

    def test_selected_source_priority_keeps_suggestions_below_saved_documents(self):
        class Browser:
            def clean_title(self,title):return title
            def navigate(self,url):self.url=url
            def read(self,state,pending):return {'status':'blocked'}
        state={'title':'Example','suggested_document_url':sidem_url('Example')}
        browser=Browser()
        self.assertEqual(connect_page(browser,state,lambda *a,**k:None)['url'],sidem_url('Example'))
        state['document_url']='https://namu.wiki/w/Example'
        self.assertEqual(connect_page(browser,state,lambda *a,**k:None)['url'],state['document_url'])
        self.assertEqual(connect_page(browser,state,lambda *a,**k:None,selected_url=sidem_url('Another'))['url'],sidem_url('Another'))


if __name__=='__main__':unittest.main()
