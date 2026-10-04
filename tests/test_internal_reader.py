import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from browser_worker import connect_page
from internal_reader import BrowserJob, InternalReader

URL='https://namu.wiki/w/Example'


class FakeJob:
    def __init__(self,base,state,messages,**options):
        self.key=state['token']+':'+state['track'];self.cancelled=threading.Event();self.options=options;self.state=state
    def cancel(self):self.cancelled.set()
    def close(self):self.cancel()


class Bridge:
    def __init__(self):
        self.state={'track':'one','token':'test','title':'Example','playing':True,'receipt':{},'reference_lines':[]}
        self.received=[]
    def snapshot(self):return dict(self.state)
    def receive(self,value):self.received.append(value);return 'queued'


class InternalReaderTests(unittest.TestCase):
    def test_real_worker_pipe_returns_result_and_closes_without_request_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'browser_worker.py').write_text('import json,sys\nrequest=json.loads(sys.stdin.readline())\nprint(json.dumps({"kind":"lyrics","value":{"text":"synthetic"}}),flush=True)\ncommand=json.loads(sys.stdin.readline())\nassert command["action"]=="close"\n',encoding='utf-8')
            messages=queue.Queue()
            job=BrowserJob(base,self.bridge.snapshot(),messages,profile=base/'profile')
            try:
                kind,(source,event)=messages.get(timeout=5)
                self.assertEqual((kind,source,event['kind']),('internal_reader',job,'lyrics'))
                job.close()
                self.assertFalse(job.thread.is_alive());self.assertEqual(job.process.returncode,0)
                self.assertEqual([path.name for path in base.iterdir()],['browser_worker.py'])
            finally:job.close()

    def setUp(self):
        self.bridge=Bridge();self.reader=InternalReader('.',self.bridge,queue.Queue(),job_factory=FakeJob)
        self.reader.enabled=True
    def tearDown(self):self.reader.close()
    def observe(self):self.reader.observe(self.bridge.snapshot());return self.reader.job

    def test_one_worker_per_song_and_only_acknowledged_display_closes_it(self):
        job=self.observe();self.observe();self.assertIs(self.reader.job,job)
        self.reader.receive(job,{'kind':'lyrics','value':{'url':URL,'text':'synthetic','match_mode':'title'}})
        self.assertEqual(self.bridge.received[0]['token'],'test');self.assertFalse(job.cancelled.is_set())
        self.bridge.state['receipt']={'status':'displayed'};self.observe()
        self.assertTrue(job.cancelled.is_set());self.assertIsNone(self.reader.job)
        self.observe();self.assertIsNone(self.reader.job)
        self.assertIn('연동 완료',self.reader.status)

    def test_old_song_result_never_reaches_bridge(self):
        old=self.observe();self.bridge.state['track']='two';new=self.observe()
        self.assertTrue(old.cancelled.is_set());self.assertIsNot(old,new)
        self.reader.receive(old,{'kind':'lyrics','value':{'url':URL,'text':'old'}})
        self.assertEqual(self.bridge.received,[])

    def test_blocked_page_is_not_retried_when_references_arrive(self):
        job=self.observe();self.reader.receive(job,{'kind':'blocked','url':URL})
        self.reader.receive(job,{'kind':'finished'});self.bridge.state['reference_lines']=['first','second']
        self.observe();self.assertIsNone(self.reader.job)
        self.assertIn('확인 필요',self.reader.status)

    def test_ambiguous_page_rechecks_once_when_references_arrive(self):
        job=self.observe();self.reader.receive(job,{'kind':'choices','urls':[URL]})
        self.reader.receive(job,{'kind':'finished'})
        self.bridge.state['reference_lines']=['first','second'];new=self.observe()
        self.assertIsNotNone(new);self.assertIsNot(new,job)
        self.observe();self.assertIs(self.reader.job,new)

    def test_references_arriving_during_discovery_recheck_the_stale_worker_once(self):
        job=self.observe()
        self.bridge.state['reference_lines']=['first reference','second reference']
        self.reader.receive(job,{'kind':'choices','urls':[URL]})
        self.reader.receive(job,{'kind':'finished'})
        new=self.observe()
        self.assertIsNotNone(new);self.assertIsNot(new,job)
        self.reader.receive(new,{'kind':'choices','urls':[URL]})
        self.reader.receive(new,{'kind':'finished'})
        self.observe();self.assertIsNone(self.reader.job)

    def test_no_table_rechecks_once_when_background_originals_arrive(self):
        job=self.observe()
        self.reader.receive(job,{'kind':'error','error_code':'no_table','text':'No matching table'})
        self.reader.receive(job,{'kind':'finished'})
        self.observe();self.assertIsNone(self.reader.job)
        self.bridge.state['reference_lines']=['first reference','second reference']
        retry=self.observe()
        self.assertIsNotNone(retry);self.assertIsNot(retry,job)
        self.assertEqual(retry.state['reference_lines'],self.bridge.state['reference_lines'])
        self.reader.receive(retry,{'kind':'error','error_code':'no_table'})
        self.reader.receive(retry,{'kind':'finished'})
        self.observe();self.assertIsNone(self.reader.job)

    def test_no_table_rechecks_if_originals_arrived_while_discovery_was_running(self):
        job=self.observe()
        self.bridge.state['reference_lines']=['first reference','second reference']
        self.reader.receive(job,{'kind':'error','error_code':'no_table'})
        self.reader.receive(job,{'kind':'finished'})
        self.assertIsNotNone(self.observe())

    def test_script_failure_does_not_retry_when_background_originals_arrive(self):
        job=self.observe()
        self.reader.receive(job,{'kind':'error','error_code':'script'})
        self.reader.receive(job,{'kind':'finished'})
        self.bridge.state['reference_lines']=['first reference','second reference']
        self.observe();self.assertIsNone(self.reader.job)

    def test_access_challenge_is_not_retried_when_references_arrive_during_discovery(self):
        job=self.observe()
        self.bridge.state['reference_lines']=['first reference','second reference']
        self.reader.receive(job,{'kind':'blocked','url':URL})
        self.reader.receive(job,{'kind':'finished'})
        self.observe();self.assertIsNone(self.reader.job)

    def test_missing_dependency_and_external_candidate_are_rejected(self):
        with patch('internal_reader.available',return_value=False):
            with self.assertRaises(ValueError):self.reader.set_enabled(True)
        self.assertFalse(self.reader.enabled)
        self.reader.enabled=True
        with self.assertRaises(ValueError):self.reader.connect(selected_url='https://example.com/')

    def test_disable_stops_worker_and_pause_still_acknowledges(self):
        job=self.observe();self.reader.awaiting=True
        self.bridge.state.update(playing=False,receipt={'status':'displayed'});self.observe()
        self.assertTrue(job.cancelled.is_set())
        self.reader.set_enabled(False);self.bridge.state['track']='new';self.observe()
        self.assertIsNone(self.reader.job)

    def test_timeout_retries_wait_five_then_fifteen_seconds_and_for_worker_exit(self):
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            first=self.observe()
            self.reader.receive(first,{'kind':'error','error_code':'timeout','stage':'navigation','text':'navigation timeout'})
            now.return_value=4.9;self.assertIs(self.observe(),first)
            now.return_value=6;self.assertIs(self.observe(),first)
            self.reader.receive(first,{'kind':'finished'})
            second=self.observe();self.assertIsNotNone(second);self.assertIsNot(second,first)
            self.reader.receive(second,{'kind':'error','error_code':'timeout','stage':'evaluation'})
            self.reader.receive(second,{'kind':'finished'})
            now.return_value=20.9;self.assertIsNone(self.observe())
            now.return_value=21;third=self.observe();self.assertIsNotNone(third);self.assertIsNot(third,second)
            self.reader.receive(third,{'kind':'error','error_code':'timeout','stage':'startup'})
            self.reader.receive(third,{'kind':'finished'})
            now.return_value=100;self.assertIsNone(self.observe())

    def test_pending_timeout_waits_while_paused_then_retries_with_current_state(self):
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            first=self.observe()
            self.reader.receive(first,{'kind':'error','error_code':'timeout'})
            self.reader.receive(first,{'kind':'finished'})
            self.bridge.state['playing']=False;now.return_value=10
            self.assertIsNone(self.observe())
            self.bridge.state['playing']=True
            second=self.observe();self.assertIsNotNone(second);self.assertEqual(second.key,first.key)

    def test_new_track_or_session_cancels_old_timeout_and_resets_retry_budget(self):
        for field in ('track','token'):
            with self.subTest(field=field),patch('internal_reader.time.monotonic',return_value=0) as now:
                self.reader.last_key='';first=self.observe()
                self.reader.receive(first,{'kind':'error','error_code':'timeout'})
                self.reader.receive(first,{'kind':'finished'})
                self.bridge.state[field]+='-new';now.return_value=2;current=self.observe()
                self.assertNotEqual(first.key,current.key)
                self.reader.receive(first,{'kind':'finished'})
                self.assertIs(self.reader.job,current)
                self.reader.receive(current,{'kind':'error','error_code':'timeout'})
                self.reader.receive(current,{'kind':'finished'})
                now.return_value=5;self.assertIsNone(self.observe())
                now.return_value=7;self.assertIsNotNone(self.observe())

    def test_manual_connect_cancels_pending_timeout_and_retry_keeps_selected_document(self):
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            first=self.observe()
            self.reader.receive(first,{'kind':'error','error_code':'timeout'})
            self.reader.receive(first,{'kind':'finished'})
            now.return_value=2;self.reader.connect(visible=True,selected_url=URL)
            manual=self.reader.job
            self.reader.receive(manual,{'kind':'error','error_code':'timeout'})
            self.reader.receive(manual,{'kind':'finished'})
            now.return_value=5;self.assertIsNone(self.observe())
            now.return_value=7;retry=self.observe();self.assertIsNotNone(retry)
            self.assertTrue(retry.options['visible']);self.assertEqual(retry.options['selected_url'],URL)

    def start_sidem_search(self):
        self.bridge.state['document_url']=URL
        suggestion='https://sidem-ko.fandom.com/ko/wiki/Example'
        self.reader.connect({**self.bridge.snapshot(),'document_url':'','suggested_document_url':suggestion})
        return self.reader.job,suggestion

    def test_sidem_search_timeout_retry_keeps_source_without_approving_document(self):
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            first,suggestion=self.start_sidem_search()
            self.reader.receive(first,{'kind':'error','error_code':'timeout','stage':'navigation'})
            self.reader.receive(first,{'kind':'finished'})
            now.return_value=6;retry=self.observe()
            self.assertEqual(retry.state['suggested_document_url'],suggestion)
            self.assertEqual(retry.state['document_url'],'')
            self.assertEqual(retry.options['selected_url'],'')
            self.assertEqual(self.bridge.state['document_url'],URL)

    def test_sidem_search_late_reference_retry_keeps_source_and_refreshes_reference(self):
        first,suggestion=self.start_sidem_search()
        self.reader.receive(first,{'kind':'choices','urls':[suggestion]})
        self.reader.receive(first,{'kind':'finished'})
        self.bridge.state['reference_lines']=['first new phrase','second new phrase']
        retry=self.observe()
        self.assertEqual(retry.state['suggested_document_url'],suggestion)
        self.assertEqual(retry.state['document_url'],'')
        self.assertEqual(retry.state['reference_lines'],self.bridge.state['reference_lines'])
        self.assertEqual(retry.options['selected_url'],'')

    def test_search_source_context_clears_on_new_song_or_session_even_while_paused(self):
        for field in ('track','token'):
            with self.subTest(field=field):
                first,_=self.start_sidem_search()
                self.bridge.state[field]+='-new';self.bridge.state['playing']=False
                self.observe()
                self.assertTrue(first.cancelled.is_set());self.assertIsNone(self.reader.search_context)
                self.bridge.state['playing']=True;next_job=self.observe()
                self.assertEqual(next_job.state['document_url'],URL)
                self.assertNotIn('suggested_document_url',next_job.state)

    def test_a_new_manual_connection_replaces_the_old_source_context(self):
        first,suggestion=self.start_sidem_search()
        self.reader.connect(selected_url=URL)
        self.assertTrue(first.cancelled.is_set())
        self.assertEqual(self.reader.job.options['selected_url'],URL)
        self.assertNotEqual(self.reader.search_context[1]['suggested_document_url'],suggestion)
        self.reader.set_enabled(False)
        self.assertIsNone(self.reader.search_context)

    def test_mode_off_cancels_pending_timeout_retry(self):
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            job=self.observe();self.reader.receive(job,{'kind':'error','error_code':'timeout'})
            self.reader.set_enabled(False)
            self.reader.receive(job,{'kind':'finished'})
            now.return_value=100;self.assertIsNone(self.observe())

    def test_only_typed_timeouts_retry_and_blocked_cancels_a_pending_retry(self):
        for event in ({'kind':'error','text':'TimeoutError'},
                      {'kind':'error','error_code':'no_table'},
                      {'kind':'error','error_code':'script'},
                      {'kind':'error','error_code':'navigation_failed'},
                      {'kind':'blocked','url':URL}):
            with self.subTest(event=event),patch('internal_reader.time.monotonic',return_value=0) as now:
                self.reader.last_key='';job=self.observe()
                self.reader.receive(job,event);self.reader.receive(job,{'kind':'finished'})
                now.return_value=100;self.assertIsNone(self.observe())
        with patch('internal_reader.time.monotonic',return_value=0) as now:
            self.reader.last_key='';job=self.observe()
            self.reader.receive(job,{'kind':'error','error_code':'timeout'})
            self.reader.receive(job,{'kind':'blocked','url':URL})
            self.reader.receive(job,{'kind':'finished'})
            now.return_value=100;self.assertIsNone(self.observe())


class Browser:
    def __init__(self,results,suggestions=()):self.results=iter(results);self.urls=[];self.found=suggestions;self.searches=0
    def clean_title(self,title):return title
    def navigate(self,url):self.urls.append(url)
    def read(self,state,pending):self.pending=pending;return next(self.results)
    def suggestions(self,title):self.searches+=1;return self.found


class DiscoveryTests(unittest.TestCase):
    def connect(self,browser,**state):return connect_page(browser,{'title':'Example',**state},lambda *args,**kw:None)

    def test_missing_lyric_table_has_a_typed_result_for_late_reference_matching(self):
        result=self.connect(Browser([{'status':'no_table','url':URL}]))
        self.assertEqual((result['kind'],result.get('error_code')),('error','no_table'))

    def test_autocomplete_does_not_revisit_current_document_under_an_escaped_alias(self):
        source='https://namu.wiki/w/Crossing!'
        browser=Browser([{'status':'no_table','url':source}],
                        [source,source+'?from=Crossing#s-4','https://namu.wiki/w/Crossing%21'])
        self.assertEqual(self.connect(browser,title='Crossing!')['kind'],'error')
        self.assertEqual(browser.urls,['https://namu.wiki/w/Crossing%21'])

    def test_redirect_destination_is_remembered_even_when_the_page_has_no_table(self):
        source='https://namu.wiki/w/Actual'
        browser=Browser([{'status':'no_table','url':source,'candidates':[source+'?from=Example']}],[source])
        self.assertEqual(self.connect(browser)['kind'],'error')
        self.assertEqual(browser.urls,[URL])

    def test_pending_candidates_are_deduplicated_by_document_identity(self):
        source='https://namu.wiki/w/dear%E2%80%A6'
        browser=Browser([{'status':'no_table','candidates':[source,'https://namu.wiki/w/dear…']},
                         {'status':'no_table','url':source}])
        self.assertEqual(self.connect(browser)['kind'],'error')
        self.assertEqual(browser.urls,[URL,source])

    def test_compatibility_candidate_is_not_discarded_after_wrong_spelling(self):
        wrong='https://namu.wiki/w/Resonance%2B'
        correct='https://namu.wiki/w/Resonance%E2%81%BA'
        browser=Browser([{'status':'no_table','url':wrong,'candidates':[correct]},
                         {'status':'matched','url':correct,'text':'synthetic','matchMode':'lyrics'}])
        result=self.connect(browser,title='Resonance⁺ -25 colors-',document_url=wrong)
        self.assertEqual(result['kind'],'lyrics')
        self.assertEqual(result['value']['url'],correct)
        self.assertEqual(browser.urls,[wrong,correct])

    def test_autocomplete_can_correct_a_compatibility_spelling(self):
        wrong='https://namu.wiki/w/Resonance%2B'
        correct='https://namu.wiki/w/Resonance%E2%81%BA'
        browser=Browser([{'status':'no_table','url':wrong},
                         {'status':'matched','url':correct,'text':'synthetic','matchMode':'lyrics'}],
                        [wrong,correct])
        self.assertEqual(self.connect(browser,document_url=wrong)['kind'],'lyrics')
        self.assertEqual(browser.urls,[wrong,correct])
        self.assertEqual(browser.searches,1)

    def test_distinct_document_case_is_not_merged(self):
        lower='https://namu.wiki/w/example'
        browser=Browser([{'status':'no_table','candidates':[lower]},{'status':'no_table','url':lower}])
        self.assertEqual(self.connect(browser)['kind'],'error')
        self.assertEqual(browser.urls,[URL,lower])

    def test_access_prompt_stops_before_search_or_candidate_navigation(self):
        browser=Browser([{'status':'blocked'}],['https://namu.wiki/w/Other'])
        self.assertEqual(self.connect(browser)['kind'],'blocked')
        self.assertEqual(browser.searches,0);self.assertEqual(browser.urls,[URL])

    def test_original_candidates_and_unique_title_reuse_existing_matching_rules(self):
        other=URL+'(Series)'
        browser=Browser([{'status':'no_table','candidates':[other]},
            {'status':'needs_confirmation','url':other},
            {'status':'matched','url':other,'text':'synthetic','matchMode':'unique_title'}])
        result=self.connect(browser)
        self.assertEqual(result['kind'],'lyrics');self.assertEqual(result['value']['match_mode'],'unique_title')
        self.assertEqual(browser.pending['uniqueTitleURL'],other)

    def test_multiple_choices_are_not_guessed(self):
        other=URL+'(Other)'
        browser=Browser([{'status':'needs_confirmation','url':URL,'candidates':[other]},
                         {'status':'needs_confirmation','url':other}])
        self.assertEqual(self.connect(browser),{'kind':'choices','urls':[URL,other]})

    def test_candidate_navigation_is_bounded_and_wrong_host_rejected(self):
        urls=[URL+str(i) for i in range(12)]
        browser=Browser([{'status':'no_table','candidates':urls}]*8)
        self.connect(browser);self.assertEqual(len(browser.urls),8)
        with self.assertRaises(ValueError):self.connect(Browser([]),document_url='https://example.com/')

    def test_discovery_limit_does_not_turn_one_visited_candidate_into_unique_match(self):
        urls=[URL+str(i) for i in range(10)]
        browser=Browser([{'status':'needs_confirmation','url':URL}]+[{'status':'no_table'}]*7,urls)
        self.assertEqual(self.connect(browser),{'kind':'choices','urls':[URL]})
        self.assertEqual(len(browser.urls),8)

    def test_page_truncation_does_not_make_one_visible_lyric_candidate_unique(self):
        browser=Browser([{'status':'needs_confirmation','url':URL,'unexamined':True}])
        self.assertEqual(self.connect(browser),{'kind':'choices','urls':[URL]})
        self.assertEqual(browser.urls,[URL])
        self.assertEqual(browser.searches,1)


if __name__=='__main__':unittest.main()
