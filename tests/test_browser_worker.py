import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from browser_worker import BrowserScriptError,BrowserTimeout,DomReadyBridge,SourceBrowser,error_result


class DomReadyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.completed=Mock()
        self.bridge=DomReadyBridge(self.completed)
        self.source=SimpleNamespace(Source='https://sidem-ko.fandom.com/ko/wiki/True_Horizon')
        self.event=SimpleNamespace(NavigationId=2)
        self.bridge.starting(self.source,self.event)

    def test_article_dom_initializes_bridge_without_waiting_for_ads(self):
        self.bridge.dom_ready(self.source,self.event)
        self.completed.assert_called_once_with(self.source,self.event)
        self.bridge.finish(self.source,self.event)
        self.completed.assert_called_once()

    def test_other_sources_and_non_article_pages_wait_for_full_navigation(self):
        for url in ('about:blank','https://example.com/ko/wiki/True_Horizon',
                    'https://sidem-ko.fandom.com.evil.test/ko/wiki/True_Horizon',
                    'https://sidem-ko.fandom.com/','https://namu.wiki/w/ESPADA'):
            with self.subTest(url=url):
                source=SimpleNamespace(Source=url)
                self.bridge.starting(source,self.event)
                self.completed.reset_mock()
                self.bridge.dom_ready(source,self.event)
                self.completed.assert_not_called()
                self.bridge.finish(source,self.event)
                self.completed.assert_called_once_with(source,self.event)

    def test_stale_document_events_do_not_initialize_new_navigation(self):
        newer=SimpleNamespace(NavigationId=3)
        self.bridge.starting(self.source,newer)
        self.bridge.dom_ready(self.source,self.event)
        self.bridge.finish(self.source,self.event)
        self.completed.assert_not_called()
        self.bridge.dom_ready(self.source,newer)
        self.completed.assert_called_once_with(self.source,newer)

    def test_each_new_document_gets_its_own_bridge(self):
        self.bridge.dom_ready(self.source,self.event)
        newer=SimpleNamespace(NavigationId=3)
        self.bridge.starting(self.source,newer)
        self.bridge.dom_ready(self.source,newer)
        self.assertEqual(self.completed.call_count,2)


class BrowserWorkerTests(unittest.TestCase):
    def setUp(self):
        self.window=Mock()
        self.window.events.loaded.is_set.return_value=False
        self.stopped=threading.Event()
        self.browser=SourceBrowser(self.window,self.stopped)

    def test_each_timeout_preserves_its_stage_for_bounded_retry_and_status(self):
        for stage,label in [('startup','초기화'),('navigation','문서 로딩'),('evaluation','문서 후보 검색')]:
            with self.subTest(stage=stage):
                event=error_result(BrowserTimeout(stage,'문서 후보 검색'))
                self.assertEqual((event['kind'],event['error_code'],event['stage']),('error','timeout',stage))
                self.assertIn(label,event['text'])

    def test_navigation_timeout_does_not_inject_or_hide_its_stage(self):
        self.window.events=SimpleNamespace(loaded=Mock())
        self.window.events.loaded.is_set.return_value=False
        self.window.events.loaded.wait.return_value=False
        with self.assertRaises(BrowserTimeout) as caught:
            self.browser.navigate('https://namu.wiki/w/Example')
        self.assertEqual(caught.exception.stage,'navigation')
        self.window.evaluate_js.assert_not_called()

    def test_loaded_same_document_reuses_the_page_without_clearing_readiness(self):
        pairs=[
            ('https://namu.wiki/w/Crossing!','https://namu.wiki/w/Crossing!'),
            ('https://namu.wiki/w/Crossing!','https://namu.wiki/w/Crossing%21#s-4'),
            ('https://namu.wiki/w/dear%E2%80%A6?from=dear...','https://namu.wiki/w/dear…'),
        ]
        for current,target in pairs:
            with self.subTest(current=current,target=target):
                self.window.reset_mock()
                self.window.events.loaded.is_set.return_value=True
                self.window.events.loaded.wait.return_value=False
                self.window.get_current_url.return_value=current
                with patch.object(self.browser,'inject') as inject:self.browser.navigate(target)
                self.window.events.loaded.clear.assert_not_called()
                self.window.events.loaded.wait.assert_not_called()
                self.window.load_url.assert_not_called()
                inject.assert_called_once_with()

    def test_compatibility_spelling_is_a_distinct_navigation_target(self):
        for current,target in (
            ('https://namu.wiki/w/Resonance%2B','https://namu.wiki/w/Resonance%E2%81%BA'),
            ('https://namu.wiki/w/dear...','https://namu.wiki/w/dear%E2%80%A6'),
        ):
            with self.subTest(current=current,target=target):
                self.window.reset_mock()
                self.window.events.loaded.is_set.return_value=True
                self.window.events.loaded.wait.return_value=True
                self.window.get_current_url.return_value=current
                with patch.object(self.browser,'inject') as inject:self.browser.navigate(target)
                self.window.load_url.assert_called_once_with(target)
                self.window.events.loaded.clear.assert_called_once_with()
                self.window.events.loaded.wait.assert_called_once_with(20)
                inject.assert_called_once_with()

    def test_same_url_without_readiness_still_waits_for_real_navigation(self):
        self.window.get_current_url.return_value='https://namu.wiki/w/Crossing!'
        self.window.events.loaded.wait.return_value=True
        with patch.object(self.browser,'inject') as inject:
            self.browser.navigate('https://namu.wiki/w/Crossing!')
        self.window.load_url.assert_called_once_with('https://namu.wiki/w/Crossing!')
        self.window.events.loaded.wait.assert_called_once_with(20)
        inject.assert_called_once_with()

    def test_a_different_document_still_navigates_and_waits_before_injection(self):
        self.window.events.loaded.is_set.return_value=True
        self.window.events.loaded.wait.return_value=True
        self.window.get_current_url.return_value='https://namu.wiki/w/Crossing!'
        target='https://namu.wiki/w/Crossing!%20(Other%20Singer%20Ver.)'
        with patch.object(self.browser,'inject') as inject:self.browser.navigate(target)
        self.window.load_url.assert_called_once_with(target)
        self.window.events.loaded.clear.assert_called_once_with()
        self.window.events.loaded.wait.assert_called_once_with(20)
        inject.assert_called_once_with()

    def test_foreign_navigation_is_not_reclassified_as_retryable_timeout(self):
        with self.assertRaises(ValueError):self.browser.navigate('https://example.com/w/Example')
        self.window.load_url.assert_not_called()
        event=error_result(ValueError('untrusted page detail'))
        self.assertNotEqual(event.get('error_code'),'timeout')
        self.assertNotIn('untrusted page detail',event['text'])

    def test_installed_promise_callback_contract_delivers_result(self):
        def evaluate(script,callback):
            callback({'status':'matched','text':'synthetic'})
            return True
        self.window.evaluate_js.side_effect=evaluate
        self.assertEqual(self.browser.evaluate('syntheticCall()'),{'status':'matched','text':'synthetic'})

    def test_missing_dispatch_ack_fails_immediately_instead_of_waiting_for_a_callback(self):
        self.window.evaluate_js.return_value=None
        with patch('browser_worker.time.monotonic') as clock:
            with self.assertRaises(BrowserScriptError) as caught:
                self.browser.evaluate('brokenCall(','가사 표 읽기')
        clock.assert_not_called()
        event=error_result(caught.exception)
        self.assertEqual((event['error_code'],event['stage']),('script','evaluation'))
        self.assertIn('가사 표 읽기',event['text'])

    def test_reported_script_failure_is_not_treated_as_a_missing_lyric_table(self):
        def evaluate(script,callback):callback({'status':'script_error'});return True
        self.window.evaluate_js.side_effect=evaluate
        with self.assertRaises(BrowserScriptError):self.browser.evaluate('missingReader()')

    def test_dispatched_but_unanswered_promise_has_evaluation_timeout_and_operation(self):
        self.window.evaluate_js.return_value=True
        with patch('browser_worker.time.monotonic',side_effect=[0,13]):
            with self.assertRaises(BrowserTimeout) as caught:
                self.browser.evaluate('pendingPromise()','문서 후보 검색')
        self.assertEqual(caught.exception.stage,'evaluation')
        self.assertIn('문서 후보 검색',str(caught.exception))

    def test_cancellation_does_not_become_a_retryable_timeout(self):
        self.window.evaluate_js.return_value=True
        self.stopped.set()
        with self.assertRaises(RuntimeError) as caught:self.browser.evaluate('pendingPromise()')
        self.assertNotIsInstance(caught.exception,BrowserTimeout)


if __name__=='__main__':unittest.main()
