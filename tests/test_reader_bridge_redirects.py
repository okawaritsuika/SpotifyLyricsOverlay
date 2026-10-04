import queue
import unittest
from unittest.mock import patch

from lyrics import Line
from media import Playback
from reader_bridge import ReaderBridge


class ReaderBridgeRedirectTests(unittest.TestCase):
    def setUp(self):
        self.bridge=ReaderBridge(messages=queue.Queue())
        playback=Playback(title='dear...',artist='Example',playing=True)
        self.bridge.observe(playback)
        self.bridge.set_reference(playback.key,['unrelated reference one','unrelated reference two'])
        self.state=self.bridge.snapshot()
        self.value={'track':self.state['track'],'token':self.state['token'],
                    'url':'https://namu.wiki/w/dear%E2%80%A6?from=dear...#s-4','text':'synthetic'}

    def receive(self):
        with patch('reader_bridge.parse_table_text',return_value=(Line('source phrase','reading','번역'),)):
            return self.bridge.receive(self.value)

    def test_saved_alias_survives_ellipsis_redirect_and_query_changes(self):
        for saved in ('https://namu.wiki/w/dear...','https://namu.wiki/w/dear%E2%80%A6',
                      'https://namu.wiki/w/dear%E2%80%A6?from=dear...'):
            with self.subTest(saved=saved):
                self.bridge.document_urls[self.state['track']]=saved
                self.assertEqual(self.receive(),'queued')
                self.assertEqual(self.bridge.snapshot()['receipt']['status'],'queued')

    def test_saved_document_approval_does_not_cross_case_or_distinct_document_names(self):
        for saved in ('https://namu.wiki/w/Dear...','https://namu.wiki/w/dear(Other)'):
            with self.subTest(saved=saved):
                self.bridge.document_urls[self.state['track']]=saved
                with self.assertRaisesRegex(ValueError,'일치하는 구절'):
                    self.receive()
                self.assertTrue(self.bridge.messages.empty())


if __name__=='__main__':unittest.main()
