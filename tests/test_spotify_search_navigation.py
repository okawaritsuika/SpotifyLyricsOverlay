"""Search paths preserve complete titles and retain strict response identity."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import quote

from spotify_worker import SpotifyBrowser


class SpotifySearchNavigationTests(unittest.TestCase):
    def setUp(self):
        self.core=Mock();self.callbacks=[]
        self.window=Mock();self.window.get_current_url.return_value='https://open.spotify.com/'
        self.window.native=SimpleNamespace(browser=SimpleNamespace(webview=SimpleNamespace(CoreWebView2=self.core)),
                                           Invoke=lambda action:self.callbacks.append(action))
        self.browser=SpotifyBrowser(self.window,queue.Queue(),threading.Event())
        self.browser.authenticated=True
        system=patch.dict('sys.modules',{'System':SimpleNamespace(Action=lambda action:action)})
        system.start();self.addCleanup(system.stop)

    def test_encoded_slash_and_reserved_characters_reach_core_on_gui_callback(self):
        for title in ('1/3','a/b\\c%?#','日本語 1/3'):
            url='https://open.spotify.com/search/'+quote(title,safe='')
            self.browser.navigate_search(url)
            self.core.Navigate.assert_not_called()
            self.callbacks.pop()()
            self.core.Navigate.assert_called_once_with(url)
            self.core.reset_mock()
        self.window.load_url.assert_not_called()

    def test_catalog_requires_complete_slash_title_before_finishing(self):
        with patch('spotify_worker.emit') as emit:
            self.browser.command({'action':'catalog_search','request_id':'one','title':'1/3'})
            self.callbacks.pop()()
            self.core.Navigate.assert_called_once_with('https://open.spotify.com/search/1%2F3')
            self.browser.receive(('search','1',{'data':{'searchV2':{}}}))
            self.assertIsNotNone(self.browser.catalog_active)
            emit.assert_not_called()
            self.browser.receive(('search','1/3',{'data':{'searchV2':{}}}))
        self.assertIsNone(self.browser.catalog_active)
        self.assertEqual(emit.call_args.args,('catalog_result',))
        self.assertEqual(emit.call_args.kwargs['request_id'],'one')
        self.assertEqual(emit.call_args.kwargs['error'],'')

    def test_lyric_search_uses_same_safe_path_without_replacing_request_or_generation(self):
        pending={'key':'song','title':'1/3','artist':'Artist','album':'Album','duration':200}
        self.browser.pending=pending;self.browser.generation=7
        with patch('spotify_worker.emit'):self.browser.begin_search()
        self.callbacks.pop()()
        self.core.Navigate.assert_called_once_with('https://open.spotify.com/search/1%2F3%20Artist')
        self.assertIs(self.browser.pending,pending)
        self.assertEqual(self.browser.generation,7)
        self.assertEqual(self.browser.active_query,'1/3 Artist')
        self.assertGreater(self.browser.search_deadline,0)

    def test_verified_current_recording_requests_lyrics_without_search_navigation(self):
        track={'id':'1'*22,'title':'1/3','artists':['Artist'],'album':'Album','duration':200}
        pending={'key':'song','title':'1/3','artist':'Artist','album':'Album','duration':200,
                 'verified_track':track}
        self.browser.pending=pending
        with patch('spotify_worker.emit'),patch.object(self.browser,'fetch_current_lyrics') as fetch:
            self.browser.begin_search()
        fetch.assert_called_once_with(track)
        self.assertEqual(self.callbacks,[])

    def test_cached_track_for_different_album_still_requires_search(self):
        track={'id':'1'*22,'title':'1/3','artists':['Artist'],'album':'Other','duration':200}
        self.browser.pending={'key':'song','title':'1/3','artist':'Artist','album':'Album','duration':200,
                              'verified_track':track}
        with patch('spotify_worker.emit'),patch.object(self.browser,'fetch_current_lyrics') as fetch:
            self.browser.begin_search()
        fetch.assert_not_called()
        self.callbacks.pop()()
        self.core.Navigate.assert_called_once_with('https://open.spotify.com/search/1%2F3%20Artist')

    def test_same_current_search_still_uses_reload(self):
        self.window.get_current_url.return_value='https://open.spotify.com/search/1%2F3'
        with patch.object(self.browser,'reload') as reload:
            self.browser.command({'action':'catalog_search','request_id':'one','title':'1/3'})
        reload.assert_called_once_with()
        self.assertEqual(self.callbacks,[])

    def test_navigation_exception_retains_existing_catalog_browser_error(self):
        self.window.native.Invoke=Mock(side_effect=RuntimeError('synthetic navigation failure'))
        with patch('spotify_worker.emit') as emit:
            self.browser.command({'action':'catalog_search','request_id':'one','title':'1/3'})
        self.assertEqual(emit.call_args.args,('catalog_result',))
        self.assertEqual(emit.call_args.kwargs['code'],'browser')
        self.assertIsNone(self.browser.catalog_active)


if __name__=='__main__':unittest.main()
