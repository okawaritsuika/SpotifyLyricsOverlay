import unittest

from browser_worker import BrowserTimeout,connect_page


URL='https://sidem-ko.fandom.com/ko/wiki/Example'
ROWS=('朝の空を見上げて歩こう','いつか夢の続きへ向かおう','新しい明日を迎えるために')


def page(url=URL,meanings=('', '', ''),rows=ROWS,**extra):
    text='\n'.join(value for pair in zip(rows,meanings) for value in pair if value)
    return dict(status='matched',url=url,text=text,matchMode='lyrics',**extra)


class Browser:
    def __init__(self,results,suggestions=()):
        self.results=iter(results);self.urls=[];self.found=suggestions;self.searches=0

    def clean_title(self,title):return title
    def navigate(self,url):self.urls.append(url)
    def read(self,state,pending):
        self.pending=pending
        result=next(self.results,{'status':'no_table','url':self.urls[-1]})
        if isinstance(result,Exception):raise result
        return result

    def suggestions(self,title):
        self.searches+=1
        if isinstance(self.found,Exception):raise self.found
        return self.found


class TranslationDiscoveryTests(unittest.TestCase):
    def connect(self,browser,selected_url='',**state):
        return connect_page(browser,{'title':'Example','suggested_document_url':URL,
                            **state},lambda *args,**kwargs:None,selected_url)

    def test_original_only_source_continues_to_verified_translated_candidate(self):
        second=URL+'_(Series)'
        original=page(candidates=[second])
        translated=page(second,('아침의 하늘','꿈의 다음으로','새로운 내일'))
        browser=Browser([original,translated])
        value=self.connect(browser)['value']
        self.assertEqual((value['url'],value['text']),(original['url'],original['text']))
        self.assertEqual(value['translated_lines'],3)
        self.assertTrue(value['translation_complete'])
        self.assertFalse(value['original_only'])
        self.assertEqual(value['supplements'][0]['text'],translated['text'])
        self.assertEqual(browser.urls,[URL,second])

    def test_partial_sources_complement_blank_rows_without_replacing_base_meaning(self):
        second,third=URL+'_(Series_2)',URL+'_(Series_3)'
        original=page(meanings=('기존 번역','',''),candidates=[second,third])
        browser=Browser([original,page(second,('다른 번역','두 번째 뜻','')),
                         page(third,('','','세 번째 뜻'))])
        value=self.connect(browser)['value']
        self.assertEqual(value['text'],original['text'])
        self.assertEqual(value['translated_lines'],3)
        self.assertEqual([item['url'] for item in value['supplements']],[second,third])
        self.assertTrue(value['translation_complete'])

    def test_full_sequence_allows_different_line_boundaries(self):
        second=URL+'_(Series_2)'
        browser=Browser([page(candidates=[second]),
            page(second,('한 문장의 뜻','마지막 뜻'),rows=(''.join(ROWS[:2]),ROWS[2]))])
        value=self.connect(browser)['value']
        self.assertEqual((value['line_count'],value['translated_lines']),(3,3))

    def test_matching_snippets_cannot_supply_a_different_complete_song(self):
        second=URL+'_(Series_2)'
        browser=Browser([page(candidates=[second]),
            page(second,('첫 번째 뜻','두 번째 뜻','다른 노래 뜻'),rows=ROWS[:2]+('全く違う結末の歌詞',))])
        value=self.connect(browser)['value']
        self.assertTrue(value['original_only'])
        self.assertNotIn('supplements',value)

    def test_full_text_match_under_a_different_song_title_is_not_a_complement(self):
        second='https://sidem-ko.fandom.com/ko/wiki/Unrelated'
        browser=Browser([page(candidates=[second]),page(second,('첫 뜻','둘째 뜻','셋째 뜻'))])
        value=self.connect(browser)['value']
        self.assertTrue(value['original_only'])
        self.assertNotIn('supplements',value)

    def test_competing_new_translations_remain_blank_in_estimated_coverage(self):
        second,third=URL+'_(Series_2)',URL+'_(Series_3)'
        browser=Browser([page(candidates=[second,third]),
            page(second,('첫 해석','','')),page(third,('다른 해석','',''))])
        value=self.connect(browser)['value']
        self.assertEqual(value['translated_lines'],0)
        self.assertTrue(value['original_only'])
        self.assertEqual(len(value['supplements']),2)

    def test_each_source_uses_its_own_table_convention(self):
        second='https://namu.wiki/w/Example_(Series)'
        translated=page(second)
        translated['text']='\n'.join(value for pair in zip(ROWS,('첫 뜻','둘째 뜻','셋째 뜻'))
                                    for value in (pair[0],'한글 발음',pair[1]))
        browser=Browser([page(candidates=[second]),translated])
        value=self.connect(browser)['value']
        self.assertEqual(value['translated_lines'],3)
        self.assertTrue(value['translation_complete'])
        self.assertEqual(value['supplements'][0]['text'],translated['text'])

    def test_a_namu_pronunciation_row_alone_does_not_count_as_korean_translation(self):
        second='https://namu.wiki/w/Example_(Series)'
        browser=Browser([page(candidates=[second]),page(second,('첫 발음','둘째 발음','셋째 발음'))])
        value=self.connect(browser)['value']
        self.assertEqual(value['translated_lines'],0)
        self.assertNotIn('supplements',value)

    def test_oversized_supplement_is_skipped_without_losing_primary(self):
        second=URL+'_(Series_2)'
        original=page(candidates=[second]);original['text']+='\n'+' '*250000
        translated=page(second,('첫 뜻','둘째 뜻','셋째 뜻'));translated['text']+='\n'+' '*250000
        browser=Browser([original,translated])
        value=self.connect(browser)['value']
        self.assertEqual(value['text'],original['text'])
        self.assertTrue(value['original_only'])
        self.assertNotIn('supplements',value)
        self.assertEqual(value['discovery_status'],'bounded')

    def test_shortened_document_cannot_supply_complete_source(self):
        second=URL+'_(Series_2)'
        browser=Browser([page(candidates=[second]),page(second,('첫 뜻','둘째 뜻'),rows=ROWS[:2])])
        value=self.connect(browser)['value']
        self.assertEqual(value['translated_lines'],0)
        self.assertNotIn('supplements',value)

    def test_unmatched_and_confirmation_candidates_never_donate_translations(self):
        for status in ('unmatched','needs_confirmation'):
            with self.subTest(status=status):
                second=URL+'_(Series_2)'
                candidate=page(second,('첫 뜻','둘째 뜻','셋째 뜻'));candidate['status']=status
                browser=Browser([page(candidates=[second]),candidate])
                value=self.connect(browser)['value']
                self.assertEqual(value['translated_lines'],0)
                self.assertNotIn('supplements',value)

    def test_explicit_selection_and_pinned_document_do_not_start_automatic_discovery(self):
        for options in ({'selected_url':URL},{'document_url':URL,'document_learned':False}):
            with self.subTest(options=options):
                browser=Browser([page(candidates=[URL+'_(Series_2)'])])
                value=self.connect(browser,**options)['value']
                self.assertEqual(value['discovery_status'],'manual')
                self.assertTrue(value['original_only'])
                self.assertEqual(browser.urls,[URL])
                self.assertEqual(browser.searches,0)

    def test_learned_document_with_missing_translation_can_continue_discovery(self):
        second=URL+'_(Series_2)'
        browser=Browser([page(candidates=[second]),page(second,('첫 뜻','둘째 뜻','셋째 뜻'))])
        value=self.connect(browser,document_url=URL,document_learned=True)['value']
        self.assertEqual(value['translated_lines'],3)
        self.assertEqual(browser.urls,[URL,second])

    def test_complete_primary_returns_without_additional_navigation(self):
        browser=Browser([page(meanings=('첫 뜻','둘째 뜻','셋째 뜻'),candidates=[URL+'_(Series_2)'])])
        value=self.connect(browser)['value']
        self.assertTrue(value['translation_complete'])
        self.assertEqual(browser.urls,[URL])
        self.assertEqual(browser.searches,0)

    def test_original_only_fallback_retains_truthful_counts_after_search_exhaustion(self):
        original=page()
        browser=Browser([original])
        value=self.connect(browser)['value']
        self.assertEqual(value['text'],original['text'])
        self.assertEqual((value['line_count'],value['translated_lines']),(3,0))
        self.assertTrue(value['original_only'])
        self.assertFalse(value['translation_complete'])
        self.assertEqual(value['discovery_status'],'complete')
        self.assertEqual(browser.searches,1)

    def test_candidate_navigation_stays_bounded_when_every_source_lacks_translation(self):
        candidates=[URL+'_(Series_'+str(index)+')' for index in range(12)]
        browser=Browser([page(candidates=candidates)]+[page(url) for url in candidates[:7]])
        value=self.connect(browser)['value']
        self.assertEqual(len(browser.urls),8)
        self.assertEqual(value['discovery_status'],'bounded')
        self.assertTrue(value['original_only'])

    def test_later_access_challenge_stops_discovery_but_preserves_first_source(self):
        second=URL+'_(Series_2)'
        browser=Browser([page(candidates=[second,URL+'_(Series_3)']),{'status':'blocked'}])
        value=self.connect(browser)['value']
        self.assertEqual((value['url'],value['discovery_url']),(URL,second))
        self.assertEqual(value['discovery_status'],'blocked')
        self.assertTrue(value['original_only'])
        self.assertEqual(browser.urls,[URL,second])
        self.assertEqual(browser.searches,0)

    def test_later_timeout_preserves_source_and_identifies_incomplete_discovery(self):
        browser=Browser([page(candidates=[URL+'_(Series_2)']),BrowserTimeout('evaluation')])
        value=self.connect(browser)['value']
        self.assertEqual(value['discovery_status'],'error')
        self.assertEqual(value['discovery_error']['error_code'],'timeout')
        self.assertTrue(value['original_only'])

    def test_autocomplete_timeout_preserves_original_only_source(self):
        browser=Browser([page()],BrowserTimeout('evaluation'))
        value=self.connect(browser)['value']
        self.assertEqual(value['discovery_status'],'error')
        self.assertTrue(value['original_only'])

    def test_initial_timeout_keeps_normal_retry_contract(self):
        with self.assertRaises(BrowserTimeout):self.connect(Browser([BrowserTimeout('navigation')]))

    def test_cancellation_after_primary_is_not_hidden_as_a_source_failure(self):
        browser=Browser([page(candidates=[URL+'_(Series_2)']),RuntimeError('cancelled')])
        with self.assertRaisesRegex(RuntimeError,'cancelled'):self.connect(browser)


if __name__=='__main__':unittest.main()
