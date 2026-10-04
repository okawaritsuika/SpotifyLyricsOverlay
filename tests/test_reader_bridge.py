import http.client
import json
import unittest
from unittest.mock import patch

from media import Playback
from reader_bridge import ReaderBridge


class ReaderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge=ReaderBridge(port=0);self.bridge.start()
    def tearDown(self):self.bridge.close()
    def get(self,headers=None,path='/state',method='GET'):
        connection=http.client.HTTPConnection('127.0.0.1',self.bridge.port,timeout=2)
        connection.request(method,path,headers=headers or {})
        response=connection.getresponse();value=response.read()
        result=response.status,json.loads(value) if value.startswith(b'{') else value
        connection.close();return result
    def test_only_current_metadata_is_exposed_to_extension(self):
        self.bridge.observe(Playback(title='Test Song',artist='Artist',playing=True))
        status,value=self.get({'X-Reader-Extension':'a'*32,'Origin':'chrome-extension://'+'a'*32})
        self.assertEqual(status,200)
        self.assertEqual(set(value),{'title','artist','track','playing','token','reference_lines','full_reference_lines','document_url','document_learned','receipt','tracking','overlay','reader_backend'})
        self.assertEqual(value['reader_backend'],'chrome')
        self.assertEqual(value['overlay'],{})
        self.assertEqual(value['title'],'Test Song');self.assertTrue(value['playing'])
        self.assertEqual(self.get({'X-Reader-Extension':'a'*32},'/unknown')[0],404)
    def test_web_origin_and_rebinding_host_are_rejected(self):
        self.assertEqual(self.get()[0],403)
        self.assertEqual(self.get({'X-Reader-Extension':'a'*32,'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.get({'X-Reader-Extension':'a'*32,'Host':'example.com'})[0],403)
        self.assertEqual(self.get({'Origin':'https://example.com'},method='OPTIONS')[0],403)
        self.assertEqual(self.get({'Origin':'chrome-extension://'+'a'*32},method='OPTIONS')[0],200)

    def test_internal_mode_rejects_late_extension_delivery_before_body_is_read(self):
        self.bridge.backend='internal'
        with patch.object(self.bridge,'receive') as receive:
            status,value=self.get({'X-Reader-Extension':'a'*32},'/lyrics','POST')
        self.assertEqual((status,value),(200,{'status':'disabled'}));receive.assert_not_called()
    def test_old_snapshot_and_errors_do_not_trigger_playback(self):
        with patch('reader_bridge.time.monotonic',return_value=100):
            self.bridge.observe(Playback(title='Test',playing=True))
        with patch('reader_bridge.time.monotonic',return_value=110):self.assertFalse(self.bridge.snapshot()['playing'])
        self.bridge.observe(Playback(title='Test',playing=True,error='offline'))
        self.assertFalse(self.bridge.snapshot()['playing'])


class FullReferenceTests(unittest.TestCase):
    def setUp(self):
        self.bridge=ReaderBridge()
        self.playback=Playback(title='Full provider song',artist='Test artist',playing=True)
        self.bridge.observe(self.playback)
        self.rows=tuple(f'Synthetic original phrase number {index}' for index in range(40))+('短い',)*2

    def test_complete_document_keeps_order_and_repetition_separate_from_matching_hints(self):
        self.bridge.set_reference(self.playback.key,self.rows,complete=True)
        state=self.bridge.snapshot()
        self.assertEqual(state['full_reference_lines'],self.rows)
        self.assertEqual(state['reference_lines'],self.rows[10:40])
        self.bridge.set_reference(self.playback.key,['A later screen fragment'])
        self.assertEqual(self.bridge.snapshot()['full_reference_lines'],self.rows)
        self.assertEqual(self.bridge.snapshot()['reference_lines'][-1],'A later screen fragment')

    def test_screen_fragments_never_establish_complete_document(self):
        self.bridge.set_reference(self.playback.key,self.rows)
        self.assertFalse(self.bridge.snapshot()['full_reference_lines'])

    def test_clear_and_track_changes_remove_both_reference_kinds(self):
        self.bridge.set_reference(self.playback.key,self.rows,complete=True)
        self.bridge.clear_reference('another song')
        self.assertEqual(self.bridge.snapshot()['full_reference_lines'],self.rows)
        self.bridge.clear_reference(self.playback.key)
        self.assertFalse(self.bridge.snapshot()['reference_lines'])
        self.assertFalse(self.bridge.snapshot()['full_reference_lines'])
        self.bridge.set_reference(self.playback.key,self.rows,complete=True)
        self.bridge.observe(Playback(title='Next song',playing=True))
        self.bridge.set_reference(self.playback.key,self.rows,complete=True)
        self.assertFalse(self.bridge.snapshot()['reference_lines'])
        self.assertFalse(self.bridge.snapshot()['full_reference_lines'])

    def test_oversized_complete_documents_are_not_partially_accepted(self):
        for rows in [('synthetic lyric',)*2001,('x'*10001,),('x'*1000,)*101]:
            with self.subTest(count=len(rows)):
                self.bridge.set_reference(self.playback.key,self.rows,complete=True)
                self.bridge.set_reference(self.playback.key,rows,complete=True)
                self.assertFalse(self.bridge.snapshot()['full_reference_lines'])
