import json
from pathlib import Path
import queue
import tempfile
import unittest
from extension_inbox import ExtensionInbox,parse_export
from storage import Store

def payload(**updates):
    return json.dumps({'format':'spotify-lyrics-extension','version':1,'title':'Example Song',
                       'url':'https://namu.wiki/w/Example%20Song','table_text':'窓を開ける\n마도오 아케루\n창문을 열다',**updates},ensure_ascii=False).encode()

class InboxTests(unittest.TestCase):
    def test_uses_translation_and_generates_reading_without_fetching(self):
        title,lines,source=parse_export(payload())
        self.assertEqual(title,'Example Song');self.assertEqual(lines[0].korean,'창문을 열다')
        self.assertIn('あける',lines[0].reading)
        self.assertIn('확장',source['translation'])

    def test_invalid_source_and_incomplete_groups_are_rejected(self):
        for data in [payload(url='https://evil.test/'),payload(table_text='窓を開ける\n마도오 아케루'),payload(format='other'),b'[]']:
            with self.assertRaises(ValueError):parse_export(data)

    def test_sidem_export_uses_its_bilingual_table_convention(self):
        text='Synthetic phrase one\n첫 번째 뜻\nSynthetic phrase two\n두 번째 뜻'
        title,lines,source=parse_export(payload(url='https://sidem-ko.fandom.com/ko/wiki/Example_Song',table_text=text))
        self.assertEqual(title,'Example Song')
        self.assertEqual([line.korean for line in lines],['첫 번째 뜻','두 번째 뜻'])
        self.assertEqual(source['url'],'https://sidem-ko.fandom.com/ko/wiki/Example_Song')
        with self.assertRaises(ValueError):parse_export(payload(table_text=text))

    def test_completed_download_is_delivered_once_and_receipt_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);inbox=root/'inbox';inbox.mkdir()
            store=Store(root/'data');messages=queue.Queue()
            (inbox/'pending.spotifylyrics.json.crdownload').write_bytes(payload())
            service=ExtensionInbox(store,messages,inbox);service.scan()
            self.assertTrue(messages.empty())
            path=inbox/'ready.spotifylyrics.json';path.write_bytes(payload())
            service.scan();key,result,error=messages.get_nowait()[1]
            self.assertEqual(error,'');self.assertEqual(result[0],'Example Song')
            service.acknowledge(key);service.scan();self.assertTrue(messages.empty())
            resumed=ExtensionInbox(store,messages,inbox);resumed.scan();self.assertTrue(messages.empty())
            self.assertEqual(path.read_bytes(),payload())

    def test_bad_file_is_reported_once_and_other_downloads_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);messages=queue.Queue();service=ExtensionInbox(Store(root/'data'),messages,root)
            (root/'bad.spotifylyrics.json').write_bytes(b'{')
            (root/'good.spotifylyrics.json').write_bytes(payload())
            service.scan();events=[messages.get_nowait()[1] for _ in range(2)]
            self.assertEqual(sum(bool(e[2]) for e in events),1)
            service.scan();self.assertTrue(messages.empty())

if __name__=='__main__':unittest.main()
