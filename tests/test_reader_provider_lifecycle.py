"""Provider switches revoke old parser results and UI queue acknowledgements."""
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import LyricsApp
from lyrics import Line
from media import Playback
from reader_bridge import ReaderBridge


class ReaderProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.messages=queue.Queue();self.bridge=ReaderBridge(messages=self.messages)
        self.bridge.observe(Playback(title='Test Song',artist='Singer',playing=True))
        self.lines=(Line('窓を開けて空を見る','','창문을 열고 하늘을 봐'),)

    def payload(self):
        state=self.bridge.snapshot()
        return {'token':state['token'],'track':state['track'],'text':'synthetic',
                'url':'https://namu.wiki/w/Test%20Song','match_mode':'title'}

    def receive(self):
        with patch('reader_bridge.parse_table_text',return_value=self.lines):
            return self.bridge.receive(self.payload())

    def test_actual_switch_rotates_token_and_clears_only_pending_receipts(self):
        self.receive();before=self.bridge.snapshot()
        self.bridge.set_reference('Test Song\0Singer\0',['窓を開けて空を見る'])
        self.bridge.document_urls[before['track']]='https://namu.wiki/w/Saved'
        self.bridge.set_backend('internal');after=self.bridge.snapshot()
        self.assertNotEqual(before['token'],after['token'])
        self.assertEqual(after['track'],before['track'])
        self.assertEqual(after['receipt'],{'status':'waiting','line_count':0})
        self.assertEqual(after['reference_lines'],('窓を開けて空を見る',))
        self.assertEqual(after['document_url'],'https://namu.wiki/w/Saved')
        self.assertEqual(self.bridge.pending_documents,[])
        self.assertEqual(self.bridge.pending_translation_sources,[])

    def test_same_mode_keeps_token_and_pending_acknowledgement(self):
        self.receive();before=self.bridge.snapshot()
        self.bridge.set_backend('chrome')
        self.assertEqual(self.bridge.snapshot(),before)
        self.assertEqual(len(self.bridge.pending_documents),1)

    def test_switch_and_aba_during_parser_reject_before_commit(self):
        for modes in (('internal',),('internal','chrome')):
            with self.subTest(modes=modes):
                self.bridge.set_backend('chrome')
                value=self.payload()
                def parse(*args,**kwargs):
                    for mode in modes:self.bridge.set_backend(mode)
                    return self.lines
                with patch('reader_bridge.parse_table_text',side_effect=parse):
                    self.assertEqual(self.bridge.receive(value),'stale')
                self.assertTrue(self.messages.empty())
                self.assertEqual(self.bridge.pending_documents,[])
                self.assertEqual(self.bridge.snapshot()['receipt']['status'],'waiting')

    def test_payload_prepared_before_aba_is_rejected_before_parsing(self):
        value=self.payload();self.bridge.set_backend('internal');self.bridge.set_backend('chrome')
        with patch('reader_bridge.parse_table_text') as parse:
            with self.assertRaises(ValueError):self.bridge.receive(value)
        parse.assert_not_called();self.assertTrue(self.messages.empty())

    def test_internal_direct_delivery_uses_current_token(self):
        self.bridge.set_backend('internal')
        self.assertEqual(self.receive(),'queued')
        kind,payload=self.messages.get_nowait()
        self.assertEqual(kind,'reader_lyrics')
        self.assertEqual(payload,(self.bridge.snapshot()['track'],self.lines,False,self.bridge.snapshot()['token']))

    def test_old_ui_payload_returns_before_applying_or_acknowledging_new_source(self):
        self.receive();_,old=self.messages.get_nowait()
        self.bridge.set_backend('internal');self.receive()
        before=self.bridge.snapshot()['receipt'].copy()
        # Accessing any app state beyond the bridge would fail this minimal stub.
        LyricsApp.receive_reader_lyrics(SimpleNamespace(reader_bridge=self.bridge),*old)
        self.assertEqual(self.bridge.snapshot()['receipt'],before)
        self.assertEqual(len(self.bridge.pending_documents),1)
        self.assertEqual(self.messages.qsize(),1)

    def test_old_parser_error_does_not_replace_new_provider_receipt(self):
        value=self.payload()
        def parse(*args,**kwargs):
            self.bridge.set_backend('internal')
            self.receive()
            raise ValueError('synthetic parser failure')
        with patch('reader_bridge.parse_table_text',side_effect=parse):
            with self.assertRaises(ValueError):self.bridge.receive(value)
        self.assertEqual(self.bridge.snapshot()['receipt']['status'],'queued')
        self.assertEqual(len(self.bridge.pending_documents),1)

    def test_old_receipt_cannot_consume_current_pending_document(self):
        old=self.payload();self.bridge.set_backend('internal');self.receive()
        self.messages.get_nowait()
        self.bridge.set_receipt(old['track'],'displayed',1,token=old['token'])
        self.assertEqual(len(self.bridge.pending_documents),1)
        self.assertTrue(self.messages.empty())
        self.bridge.set_receipt(old['track'],'displayed',1,token=self.bridge.snapshot()['token'])
        self.assertEqual(self.messages.get_nowait()[0],'reader_document')


if __name__=='__main__':unittest.main()
