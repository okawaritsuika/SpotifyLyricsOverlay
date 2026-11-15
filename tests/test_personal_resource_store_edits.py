import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_translation import BatchTranslationStore, FORMAT
from lyric_annotations import LyricTextEdits
from lyrics import Line
from storage import Store
from user_annotations import SingerRoster


class PersonalResourceStoreEditsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_remove_text_field_preserves_other_edits_and_legacy_original(self):
        edits = LyricTextEdits(Store(self.directory))
        layout = ('title\0artist\0album', ('First row', 'Second row'))
        edits.select(layout)
        edits.save(0, 'Corrected first row')
        edits.save(0, '', field='korean')
        edits.save(1, '두 번째 줄', field='korean')
        self.assertTrue(edits.remove(0, 'korean'))
        self.assertFalse(edits.remove(0, 'korean'))
        reopened = LyricTextEdits(Store(self.directory))
        reopened.select(layout)
        self.assertEqual(reopened.rows, {0:'Corrected first row', 1:{'korean':'두 번째 줄'}})
        self.assertTrue(reopened.remove(0, 'original'))
        self.assertEqual(reopened.rows, {1:{'korean':'두 번째 줄'}})

    def test_reset_roster_keeps_other_recording_and_restores_automatic(self):
        roster = SingerRoster(self.directory)
        other = 'Other title\0artist\0album'
        selected = 'Target title\0artist\0album'
        roster.select(other)
        roster.save(('Other singer',))
        roster.select(selected)
        roster.save(())
        self.assertTrue(roster.remove())
        self.assertIsNone(roster.names)
        self.assertFalse(roster.remove())
        reopened = SingerRoster(self.directory)
        reopened.select(selected)
        self.assertIsNone(reopened.names)
        reopened.select(other)
        self.assertEqual(reopened.names, ('Other singer',))

    def test_ai_update_and_remove_persist_only_imports_and_allow_reply_again(self):
        archive = BatchTranslationStore(self.directory)
        metadata = ('Title', 'Artist', 'Album')
        originals = (Line('夜空を見る', '', ''), Line('風を聞く', 'かぜをきく', ''))
        identifier = archive.capture(*metadata, originals)
        batch = archive.prepare(archive.documents())[0]
        reply = json.dumps({'format': FORMAT, 'batch_id': batch['batch_id'], 'songs': [{'id': identifier, 'lines': [
            {'index': 1, 'original': originals[0].original, 'korean': '밤하늘을 본다'},
            {'index': 2, 'original': originals[1].original, 'korean': '바람을 듣는다'}]}]})
        archive.apply_reply(reply)
        self.assertEqual(archive.fill(*metadata, originals)[0].korean, '밤하늘을 본다')
        self.assertTrue(archive.update_translation(identifier, 0, '밤하늘을 바라본다'))
        self.assertEqual(archive.fill(*metadata, originals)[0].korean, '밤하늘을 바라본다')
        with patch('batch_translation.atomic_json', side_effect=OSError('write failed')), self.assertRaises(OSError):
            archive.remove_translation(identifier, 0)
        self.assertEqual(archive.fill(*metadata, originals)[0].korean, '밤하늘을 바라본다')
        archive = BatchTranslationStore(self.directory)
        self.assertEqual(archive.fill(*metadata, originals)[0].korean, '밤하늘을 바라본다')
        self.assertTrue(archive.remove_translation(identifier, 0))
        self.assertFalse(archive.remove_translation(identifier, 0))
        self.assertEqual(archive.fill(*metadata, originals), (originals[0], Line('風を聞く', 'かぜをきく', '바람을 듣는다')))
        self.assertTrue(archive.remove_translation(identifier, 1))
        self.assertFalse(archive.update_translation(identifier, 1, '사라진 번역'))
        self.assertEqual(BatchTranslationStore(self.directory).summary(), {'total_songs':0, 'total_lines':0})
        self.assertEqual(archive.apply_reply(reply)['lines'], 2)


if __name__ == '__main__':
    unittest.main()
