"""Exercise host-generated source candidates without making any page requests."""
from copy import deepcopy
import queue
import unittest
from unittest.mock import patch
from urllib.parse import quote,unquote,urlsplit

from browser_worker import connect_page
from media import Playback
from reader_bridge import ReaderBridge
from web_lyrics import source_id,parse_table_text


ORIGINALS=('朝の空を見上げて歩こう','いつか夢の続きへ向かおう','新しい明日を迎えるために')
MEANINGS=('첫 번째 번역','두 번째 번역','세 번째 번역')
SIDEM_ALBUM='THE IDOLM@STER SideM Synthetic Album'


def matched(url,meanings=('', '', ''),originals=ORIGINALS,**extra):
    rows=[]
    for original,korean in zip(originals,meanings):
        rows.append(original)
        if korean:
            if source_id(url)=='namu':rows.append('합성 한글 발음')
            rows.append(korean)
    return dict(status='matched',url=url,text='\n'.join(rows),matchMode='lyrics',**extra)


class URLBrowser:
    """A wrong navigation cannot accidentally receive the next expected page."""
    def __init__(self,pages):
        self.pages=pages;self.urls=[];self.search_urls=[];self.current=''

    def clean_title(self,title):return title

    def navigate(self,url):
        if url not in self.pages:raise AssertionError('Unexpected synthetic navigation: '+url)
        self.current=url;self.urls.append(url)

    def read(self,state,pending):
        return deepcopy(self.pages[self.current])

    def suggestions(self,title):
        self.search_urls.append(self.current)
        return []


class CrossSourceCandidateTests(unittest.TestCase):
    def bridge(self,title='Synthetic Example',album=SIDEM_ALBUM,document='',learned=True):
        bridge=ReaderBridge(messages=queue.Queue())
        playback=Playback(title,'Synthetic Unit',album,playing=True,duration=180)
        bridge.observe(playback)
        bridge.set_reference(playback.key,ORIGINALS)
        if document:
            bridge.document_urls[bridge.snapshot()['track']]=({'url':document,'learned':True}
                                                            if learned else document)
        return bridge

    def namu(self,title):return 'https://namu.wiki/w/'+quote(title,safe='')

    def connect(self,bridge,pages,selected_url=''):
        browser=URLBrowser(pages)
        event=connect_page(browser,bridge.snapshot(),lambda *args,**kwargs:None,selected_url)
        return browser,event

    def deliver(self,bridge,event):
        state=bridge.snapshot()
        with patch('reader_bridge.parse_table_text',side_effect=lambda text,strict,source:
                   parse_table_text(text,reader=lambda row:'',strict=strict,source=source)):
            self.assertEqual(bridge.receive({**event['value'],'token':state['token'],
                                            'track':state['track']}),'queued')
        kind,payload=bridge.messages.get_nowait()
        self.assertEqual(kind,'reader_lyrics')
        return payload[1]

    def test_original_only_sidem_generates_other_source_then_bridge_verifies_complement(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        primary=matched(first)
        browser,event=self.connect(bridge,{first:primary,second:matched(second,MEANINGS)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(event['value']['text'],primary['text'])
        self.assertEqual([item['url'] for item in event['value']['supplements']],[second])
        received=self.deliver(bridge,event)
        self.assertEqual(tuple(row.original for row in received),ORIGINALS)
        self.assertEqual(tuple(row.korean for row in received),MEANINGS)
        self.assertEqual(bridge.snapshot()['receipt']['translation_sources'],[second])

    def test_partial_primary_keeps_its_translation_and_fills_only_blanks(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        browser,event=self.connect(bridge,{first:matched(first,('기존 뜻','','')),
                                           second:matched(second,MEANINGS)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(tuple(row.korean for row in self.deliver(bridge,event)),
                         ('기존 뜻',MEANINGS[1],MEANINGS[2]))

    def test_learned_namu_can_use_the_actual_bridge_sidem_album_suggestion(self):
        first=self.namu('Synthetic Example')
        bridge=self.bridge(document=first);second=bridge.snapshot()['suggested_document_url']
        browser,event=self.connect(bridge,{first:matched(first),second:matched(second,MEANINGS)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(tuple(row.korean for row in self.deliver(bridge,event)),MEANINGS)

    def test_namu_without_sidem_album_suggestion_stays_on_its_source(self):
        first=self.namu('Synthetic Example');bridge=self.bridge(album='Different Series',document=first)
        self.assertNotIn('suggested_document_url',bridge.snapshot())
        browser,event=self.connect(bridge,{first:matched(first)})
        self.assertEqual(browser.urls,[first])
        self.assertTrue(event['value']['original_only'])
        self.assertNotIn('supplements',event['value'])

    def test_wrong_title_sidem_suggestion_is_not_a_discovery_lead(self):
        first=self.namu('Synthetic Example');bridge=self.bridge(document=first)
        wrong='https://sidem-ko.fandom.com/ko/wiki/Different_Song'
        bridge.state['suggested_document_url']=wrong
        browser,event=self.connect(bridge,{first:matched(first),wrong:{'status':'no_table','url':wrong}})
        self.assertEqual(browser.urls,[first])
        self.assertNotIn('supplements',event['value'])

    def test_foreign_host_suggestion_is_not_a_discovery_lead(self):
        first=self.namu('Synthetic Example');bridge=self.bridge(document=first)
        bridge.state['suggested_document_url']='https://example.com/ko/wiki/Synthetic_Example'
        browser,event=self.connect(bridge,{first:matched(first)})
        self.assertEqual(browser.urls,[first])
        self.assertTrue(event['value']['original_only'])

    def test_second_source_keeps_a_slot_when_seven_same_source_candidates_exist(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        alternatives=[first+'_(Variant_'+str(index)+')' for index in range(7)]
        pages={first:matched(first,candidates=alternatives),second:matched(second,MEANINGS),
               **{url:{'status':'no_table','url':url} for url in alternatives}}
        browser,event=self.connect(bridge,pages)
        self.assertIn(second,browser.urls)
        self.assertLessEqual(len(browser.urls),8)
        self.assertEqual(len(browser.urls),len(set(browser.urls)))
        self.assertEqual(event['value']['translated_lines'],3)

    def test_manual_document_and_explicit_selection_do_not_cross_sources_even_without_table(self):
        for selected in (False,True):
            for status in ('matched','no_table'):
                with self.subTest(selected=selected,status=status):
                    bridge=self.bridge();first=bridge.snapshot()['suggested_document_url']
                    if not selected:bridge.document_urls[bridge.snapshot()['track']]=first
                    result=matched(first) if status=='matched' else {'status':'no_table','url':first}
                    browser,event=self.connect(bridge,{first:result},first if selected else '')
                    self.assertEqual(browser.urls,[first])
                    if status=='matched':self.assertEqual(event['value']['discovery_status'],'manual')

    def test_initial_block_does_not_generate_or_visit_another_source(self):
        bridge=self.bridge();first=bridge.snapshot()['suggested_document_url']
        browser,event=self.connect(bridge,{first:{'status':'blocked'}})
        self.assertEqual(browser.urls,[first]);self.assertFalse(browser.search_urls)
        self.assertEqual(event,{'kind':'blocked','url':first})

    def test_generated_source_block_retains_primary_and_stops_without_search(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        primary=matched(first)
        browser,event=self.connect(bridge,{first:primary,second:{'status':'blocked'}})
        self.assertEqual(browser.urls,[first,second]);self.assertFalse(browser.search_urls)
        self.assertEqual(event['value']['text'],primary['text'])
        self.assertEqual(event['value']['discovery_status'],'blocked')
        self.assertTrue(event['value']['original_only'])

    def test_generated_title_keeps_source_spelling_and_url_encoding(self):
        title='Synthetic 星⁺ & 夢'
        bridge=self.bridge(title=title);first=bridge.snapshot()['suggested_document_url'];second=self.namu(title)
        browser,event=self.connect(bridge,{first:matched(first),second:matched(second,MEANINGS)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(unquote(urlsplit(browser.urls[-1]).path[len('/w/'):]),title)
        self.assertIn('%E2%81%BA',browser.urls[-1]);self.assertIn('%26',browser.urls[-1])
        self.assertEqual(event['value']['translated_lines'],3)

    def test_generated_candidate_with_different_complete_lyrics_cannot_supply_meaning(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        wrong=ORIGINALS[:2]+('全く違う別の結末の歌',)
        browser,event=self.connect(bridge,{first:matched(first),second:matched(second,MEANINGS,wrong)})
        self.assertEqual(browser.urls,[first,second])
        self.assertNotIn('supplements',event['value'])
        self.assertEqual(event['value']['translated_lines'],0)
        self.assertFalse(any(row.korean for row in self.deliver(bridge,event)))

    def test_generated_candidate_still_requires_page_match_classification(self):
        for status in ('unmatched','needs_confirmation'):
            with self.subTest(status=status):
                bridge=self.bridge();state=bridge.snapshot()
                first=state['suggested_document_url'];second=self.namu(state['title'])
                other=matched(second,MEANINGS);other['status']=status
                browser,event=self.connect(bridge,{first:matched(first),second:other})
                self.assertEqual(browser.urls,[first,second])
                self.assertNotIn('supplements',event['value'])

    def test_no_table_can_discover_other_source_without_injecting_a_candidate(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        browser,event=self.connect(bridge,{first:{'status':'no_table','url':first},second:matched(second,MEANINGS)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(event['value']['url'],second)
        self.assertEqual(tuple(row.korean for row in self.deliver(bridge,event)),MEANINGS)

    def test_two_original_only_sources_finish_without_bouncing_between_origins(self):
        bridge=self.bridge();state=bridge.snapshot()
        first=state['suggested_document_url'];second=self.namu(state['title'])
        browser,event=self.connect(bridge,{first:matched(first),second:matched(second)})
        self.assertEqual(browser.urls,[first,second])
        self.assertEqual(browser.search_urls,[second])
        self.assertTrue(event['value']['original_only'])
        self.assertEqual(event['value']['discovery_status'],'complete')

    def test_complete_primary_does_not_generate_unneeded_cross_source_navigation(self):
        bridge=self.bridge();first=bridge.snapshot()['suggested_document_url']
        browser,event=self.connect(bridge,{first:matched(first,MEANINGS)})
        self.assertEqual(browser.urls,[first]);self.assertFalse(browser.search_urls)
        self.assertTrue(event['value']['translation_complete'])


if __name__=='__main__':unittest.main()
