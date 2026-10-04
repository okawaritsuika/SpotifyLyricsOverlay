import queue
import unittest
from unittest.mock import patch

from media import Playback
from reader_bridge import ReaderBridge
from web_lyrics import parse_table_text


class ReaderOriginalOnlyTests(unittest.TestCase):
    def test_verified_original_only_table_reaches_overlay_without_invented_translation(self):
        for text in ('窓を開ける\n花を描く','Open the window\nDraw a flower'):
            with self.subTest(text=text):
                messages=queue.Queue();bridge=ReaderBridge(messages=messages)
                bridge.observe(Playback(title='Example Song',playing=True))
                state=bridge.snapshot()
                value={'track':state['track'],'token':state['token'],'url':'https://namu.wiki/w/Example%20Song',
                       'text':text,'match_mode':'title'}
                with patch('reader_bridge.parse_table_text',side_effect=lambda text,strict,source:
                           parse_table_text(text,reader=lambda row:'reading',strict=strict,source=source)):
                    self.assertEqual(bridge.receive(value),'queued')
                kind,(track,lines,supplement,token)=messages.get_nowait()
                self.assertEqual((kind,track),('reader_lyrics',state['track']))
                self.assertIs(supplement,False)
                self.assertEqual(token,state['token'])
                self.assertEqual([line.original for line in lines],text.splitlines())
                self.assertTrue(all(not line.korean for line in lines))
                self.assertEqual(bridge.snapshot()['receipt']['line_count'],2)


if __name__=='__main__':unittest.main()
