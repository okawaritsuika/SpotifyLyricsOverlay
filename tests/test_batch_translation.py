import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from batch_translation import BatchTranslationStore, FORMAT, MAX_TEXT
from lyrics import Line
from storage import atomic_json


class BatchTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name)
        self.store=BatchTranslationStore(self.directory)

    def song(self, title='Song', lines=None, artist='Artist', album='Album'):
        lines=lines or (Line('空を見て', 'そらをみて', ''), Line('風が吹く', '', '기존 번역'), Line('空を見て', '', ''))
        identifier=self.store.capture(title, artist, album, lines)
        return next(song for song in self.store.documents() if song['id'] == identifier)

    def batch(self, songs=None, size=5):
        return self.store.prepare(songs or [self.song()], size)[0]

    def reply(self, batch, rows=None):
        songs=[]
        for song in batch['songs']:
            lines=[{'index': index, 'original': row.original, 'korean': '번역 '+str(index)}
                   for index, row in enumerate(song['lines'], 1) if row.original.strip() and not row.korean.strip()]
            songs.append({'id': song['id'], 'lines': lines})
        if rows is not None:songs[0]['lines']=rows
        return {'format': FORMAT, 'batch_id': batch['batch_id'], 'songs': songs}

    def apply(self, reply):
        return self.store.apply_reply(json.dumps(reply, ensure_ascii=False))

    def test_roundtrip_preserves_original_reading_source_and_other_files(self):
        originals=(Line('空を見て', 'そらをみて', ''), Line('風が吹く', '', '기존 번역'), Line('空を見て', '', ''))
        song=self.song(lines=originals)
        untouched=[]
        for name in ('library.json', 'settings.json', 'timings/song.json', 'lyrics_cache/cache.json'):
            path=self.directory/name;path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'original fixture');untouched.append(path)
        batch=self.batch([song]);result=self.apply(self.reply(batch))
        self.assertEqual(result, {'batch_id': batch['batch_id'], 'songs': 1, 'lines': 2, 'remaining': 0,
                                 'received_songs': 1, 'received_lines': 2, 'already_lines': 0,
                                 'preserved_lines': 0, 'total_songs': 1, 'total_lines': 2})
        filled=self.store.fill('Song', 'Artist', 'Album', originals)
        self.assertEqual([row.original for row in filled], [row.original for row in originals])
        self.assertEqual([row.reading for row in filled], [row.reading for row in originals])
        self.assertEqual([row.korean for row in filled], ['번역 1', '기존 번역', '번역 3'])
        self.assertTrue(all(path.read_bytes() == b'original fixture' for path in untouched))
        self.assertEqual(self.store.revision, 1)

    def test_missing_rows_and_partial_songs_report_batch_remaining(self):
        batch=self.batch([self.song('One'), self.song('Two')])
        reply=self.reply(batch);reply['songs']=reply['songs'][:1];reply['songs'][0]['lines']=reply['songs'][0]['lines'][:1]
        result=self.apply(reply)
        self.assertEqual((result['songs'], result['lines'], result['remaining']), (1, 1, 3))
        self.assertEqual((result['received_songs'], result['received_lines'], result['already_lines'],
                          result['preserved_lines'], result['total_songs'], result['total_lines']), (1, 1, 0, 0, 1, 1))
        result=self.apply(self.reply(batch))
        self.assertEqual((result['songs'], result['lines'], result['remaining']), (2, 3, 0))
        self.assertEqual((result['received_songs'], result['received_lines'], result['already_lines'],
                          result['preserved_lines'], result['total_songs'], result['total_lines']), (2, 4, 1, 0, 2, 4))

    def test_batching_and_complete_documents(self):
        songs=[self.song(str(index)) for index in range(7)]
        complete=self.song('Complete', (Line('完成', '', '완료'),))
        batches=self.store.prepare(songs+[complete], 3)
        self.assertEqual([len(batch['songs']) for batch in batches], [3, 3, 1])
        self.assertEqual(len({song['id'] for batch in batches for song in batch['songs']}), 7)
        for value in (0, 51, True, 2.5, '3'):
            with self.subTest(value=value), self.assertRaises(ValueError):self.store.prepare(songs, value)

    def test_prompt_has_context_missing_indices_and_is_not_reply(self):
        batch=self.batch()
        self.assertIn('missing_indices', batch['prompt'])
        self.assertIn('기존 번역', batch['prompt'])
        self.assertIn('風が吹く', batch['prompt'])
        self.assertFalse(self.store.is_reply(batch['prompt']))
        self.assertFalse(self.store.is_reply('ordinary clipboard text'))
        self.assertFalse(self.store.is_reply(json.dumps({'format': FORMAT, 'batch_id': '../../escape'})))
        body=json.dumps(self.reply(batch), ensure_ascii=False)
        self.assertTrue(self.store.is_reply('```json\n'+body+'\n```'))
        self.assertEqual(self.store.apply_reply('```json\n'+body+'\n```')['lines'], 2)

    def test_blank_and_symbol_rows_keep_their_positions(self):
        lines=(Line('', '', ''), Line('  ', '', ''), Line('♪', '', ''), Line('言葉', '', ''))
        song=self.song(lines=lines);batch=self.batch([song])
        self.assertEqual(len(batch['songs'][0]['lines']), 4)
        self.assertEqual([row['index'] for row in self.reply(batch)['songs'][0]['lines']], [3, 4])
        self.apply(self.reply(batch))
        self.assertEqual([row.original for row in self.store.fill('Song', 'Artist', 'Album', lines)], ['', '  ', '♪', '言葉'])

    def test_latin_only_rows_are_excluded_and_source_translations_are_preserved(self):
        lines=(Line('“Flyers!!!”', '', ''), Line('Café ＭＵＳＩＣ ♪', '', ''),
               Line('Let’s fly!', '', '가져온 번역'), Line('空へ', '', ''))
        song=self.song(lines=lines)
        batch=self.batch([song])
        manifest=json.loads((self.store.requests_directory/(batch['batch_id']+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(manifest['songs'][song['id']]['indices'], [4])
        self.assertEqual(self.store.fill('Song', 'Artist', 'Album', lines), lines)
        self.assertEqual(self.store.prepare([self.song('English', lines[:2])]), [])

    def test_mixed_reply_preserves_exact_latin_phrases_and_can_retry_skipped_rows(self):
        lines=(Line('高みを目指して Flyers!!!', '', ''), Line('未来へ One More Time', '', ''),
               Line('WANTED! WANTED! にげちゃって!', '', ''), Line('風が吹く', '', ''))
        batch=self.batch([self.song(lines=lines)])
        reply=self.reply(batch)
        rows=reply['songs'][0]['lines']
        for row, korean in zip(rows, ('더 높은 곳을 향해, 날아오르는 이들!!!', '미래로 One more Time',
                                     'WANTED! 도망쳐!', '바람이 불어')):
            row['korean']=korean
        result=self.apply(reply)
        self.assertEqual((result['lines'], result['changed_latin_lines'], result['remaining']), (1, 3, 3))
        for row, korean in zip(rows, ('더 높은 곳을 향해, Flyers!!!', '미래로 One More Time',
                                     'WANTED! WANTED! 도망쳐!')):
            row['korean']=korean
        result=self.apply(reply)
        self.assertEqual((result['lines'], result['already_lines'], result['remaining']), (3, 1, 0))
        self.assertEqual([line.korean for line in self.store.fill('Song', 'Artist', 'Album', lines)],
                         [row['korean'] for row in rows])

    def test_mixed_reply_preserves_symbols_inside_latin_expressions(self):
        lines=(Line('True & False 失敗？', '', ''), Line('響け M@STERPIECE', '', ''))
        batch=self.batch([self.song(lines=lines)])
        reply=self.reply(batch)
        rows=reply['songs'][0]['lines']
        rows[0]['korean']='True / False 실패?'
        rows[1]['korean']='울려라 M/STERPIECE'
        result=self.apply(reply)
        self.assertEqual((result['lines'], result.get('changed_latin_lines', 0), result['remaining']), (0, 2, 2))

        rows[0]['korean']='True & False 실패?'
        rows[1]['korean']='울려라 M@STERPIECE'
        result=self.apply(reply)
        self.assertEqual((result['lines'], result['remaining']), (2, 0))

    def test_legacy_ai_english_is_restored_even_in_mirrored_history(self):
        original='Flyers!!!'
        old='날아오르는 이들!!!'
        lines=(Line(original, '', old),)
        song=self.song(lines=lines)
        atomic_json(self.store.imports_path, {'version':1, 'documents':{song['id']:{'1':old}}})
        self.store=BatchTranslationStore(self.directory)
        self.assertEqual(self.store.fill('Song', 'Artist', 'Album', lines)[0].korean, original)
        self.assertEqual(self.store.documents()[0]['lines'][0].korean, original)
        source=(Line(original, '', '별도로 가져온 번역'),)
        self.assertEqual(self.store.fill('Song', 'Artist', 'Album', source), source)

    def test_same_document_merges_missing_annotations_and_latest_source_wins(self):
        first=self.song(lines=(Line('空', '', '기존'), Line('風', '', '')))
        second=self.song(lines=(Line('空', 'そら', ''), Line('風', '', '바람')))
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.documents()), 1)
        self.assertEqual(second['lines'], (Line('空', 'そら', '기존'), Line('風', '', '바람')))
        latest=self.song(lines=(Line('空', '', '새 직접 번역'), Line('風', '', '')))
        self.assertEqual(latest['lines'][0].korean, '새 직접 번역')
        self.assertEqual(self.store.prepare([first, second, latest]), [])

    def test_repeated_capture_avoids_disk_writes(self):
        song=self.song()
        with patch('batch_translation.atomic_json', side_effect=AssertionError('unexpected write')):
            self.store.capture(song['title'], song['artist'], song['album'], song['lines'])

    def test_old_batch_survives_restart_and_regrouping(self):
        batch=self.batch([self.song('One'), self.song('Two')])
        self.store=BatchTranslationStore(self.directory)
        newer=self.store.prepare(self.store.documents(), 1)
        self.assertEqual(len(newer), 2)
        self.assertEqual(self.apply(self.reply(batch))['lines'], 4)
        reopened=BatchTranslationStore(self.directory)
        self.assertTrue(all(row.korean for song in reopened.documents() for row in song['lines']))
        self.assertEqual(reopened.prepare(reopened.documents()), [])

    def test_repeat_import_and_new_answer_never_overwrite(self):
        batch=self.batch();reply=self.reply(batch);self.apply(reply)
        before=self.store.imports_path.read_bytes()
        reply['songs'][0]['lines'][0]['korean']='다른 AI 번역'
        with patch('batch_translation.atomic_json', side_effect=AssertionError('unexpected write')):
            result=self.apply(reply)
        self.assertEqual(result['lines'], 0)
        self.assertEqual((result['received_songs'], result['received_lines'], result['already_lines'],
                          result['preserved_lines'], result['total_songs'], result['total_lines']), (1, 2, 1, 1, 1, 2))
        self.assertEqual(self.store.imports_path.read_bytes(), before)
        self.assertEqual(self.store.revision, 1)

    def test_duplicate_reply_after_restart_reports_existing_translations_without_writing(self):
        batch=self.batch();reply=self.reply(batch);self.apply(reply)
        self.store=BatchTranslationStore(self.directory)
        reply['songs'][0]['lines'][0]['korean']='  번역 1\n'
        with patch('batch_translation.atomic_json', side_effect=AssertionError('unexpected write')):
            result=self.apply(reply)
        self.assertEqual(result, {'batch_id': batch['batch_id'], 'songs': 0, 'lines': 0, 'remaining': 0,
                                 'received_songs': 1, 'received_lines': 2, 'already_lines': 2,
                                 'preserved_lines': 0, 'total_songs': 1, 'total_lines': 2})
        self.assertEqual(self.store.summary(), {'total_songs': 1, 'total_lines': 2})
        self.assertEqual(self.store.revision, 0)

    def test_summary_counts_only_saved_ai_translations_across_batches(self):
        self.song('Existing', (Line('完成', '', '기존 번역'),))
        self.assertEqual(self.store.summary(), {'total_songs': 0, 'total_lines': 0})
        batch=self.batch([self.song('First')]);self.apply(self.reply(batch))
        batch=self.batch([self.song('Second')]);result=self.apply(self.reply(batch))
        self.assertEqual((result['total_songs'], result['total_lines']), (2, 4))
        reopened=BatchTranslationStore(self.directory)
        self.assertEqual(reopened.summary(), {'total_songs': 2, 'total_lines': 4})

    def test_matching_source_translation_is_existing_but_not_an_ai_import(self):
        batch=self.batch();reply=self.reply(batch)
        self.store.capture('Song', 'Artist', 'Album',
                           (Line('空を見て', '', ' 번역 1 '), Line('風が吹く', '', '기존 번역'), Line('空を見て', '', '번역 3')))
        with patch('batch_translation.atomic_json', side_effect=AssertionError('unexpected write')):
            result=self.apply(reply)
        self.assertEqual((result['songs'], result['lines'], result['already_lines'], result['preserved_lines']), (0, 0, 2, 0))
        self.assertEqual(self.store.summary(), {'total_songs': 0, 'total_lines': 0})

    def test_source_arriving_after_request_has_priority(self):
        batch=self.batch()
        lines=(Line('空を見て', '새 독음', '사람 번역'), Line('風が吹く', '', '기존 번역'), Line('空を見て', '', ''))
        self.store.capture('Song', 'Artist', 'Album', lines)
        result=self.apply(self.reply(batch))
        self.assertEqual((result['received_lines'], result['lines'], result['already_lines'], result['preserved_lines']), (2, 1, 0, 1))
        self.assertEqual((result['total_songs'], result['total_lines']), (1, 1))
        filled=self.store.fill('Song', 'Artist', 'Album', batch['songs'][0]['lines'])
        self.assertEqual(filled[0].korean, '사람 번역')
        supplied=(Line('空を見て', '', '현재 원문 번역'), *lines[1:])
        self.assertEqual(self.store.fill('Song', 'Artist', 'Album', supplied)[0].korean, '현재 원문 번역')

    def test_changed_identity_or_originals_cannot_receive_old_reply(self):
        batch=self.batch();self.apply(self.reply(batch))
        changed=(Line('違う原文', '', ''), Line('風が吹く', '', ''), Line('空を見て', '', ''))
        self.assertEqual(self.store.fill('Song', 'Artist', 'Album', changed), changed)
        source=batch['songs'][0]['lines']
        for metadata in [('Song live', 'Artist', 'Album'), ('Song', 'Other Artist', 'Album'), ('Song', 'Artist', 'Other Album')]:
            with self.subTest(metadata=metadata):self.assertEqual(self.store.fill(*metadata, source), source)

    def assert_rejected_without_write(self, reply):
        before=self.store.imports_path.read_bytes() if self.store.imports_path.exists() else None
        with self.assertRaises(ValueError):self.apply(reply)
        after=self.store.imports_path.read_bytes() if self.store.imports_path.exists() else None
        self.assertEqual(before, after)

    def test_whole_response_validated_before_any_write(self):
        batch=self.batch([self.song('One'), self.song('Two')])
        reply=self.reply(batch);reply['songs'][1]['lines'][1]['original']='원문 변경'
        self.assert_rejected_without_write(reply)
        self.assertEqual(self.store.imports, {})

    def test_duplicate_unknown_and_invalid_rows_rejected(self):
        batch=self.batch();original=self.reply(batch)
        invalid=[]
        value=copy.deepcopy(original);value['songs'].append(value['songs'][0]);invalid.append(value)
        value=copy.deepcopy(original);value['songs'][0]['id']='0'*64;invalid.append(value)
        value=copy.deepcopy(original);value['batch_id']='0'*32;invalid.append(value)
        value=copy.deepcopy(original);value['songs'][0]['lines'].append(value['songs'][0]['lines'][0]);invalid.append(value)
        for index in (True, 0, 2, 4, '1'):
            value=copy.deepcopy(original);value['songs'][0]['lines'][0]['index']=index;invalid.append(value)
        for korean in ('', '  ', None, 'x'*(MAX_TEXT+1), 'a\0b'):
            value=copy.deepcopy(original);value['songs'][0]['lines'][0]['korean']=korean;invalid.append(value)
        for value in invalid:
            with self.subTest(value=str(value)[:120]):self.assert_rejected_without_write(value)

    def test_corrupt_unanswered_manifest_song_rejects_before_write(self):
        batch=self.batch([self.song('One'), self.song('Two')])
        path=self.store.requests_directory/(batch['batch_id']+'.json')
        manifest=json.loads(path.read_text(encoding='utf-8'))
        manifest['songs'][batch['songs'][1]['id']]['indices']=[9999]
        path.write_text(json.dumps(manifest), encoding='utf-8')
        reply=self.reply(batch);reply['songs']=reply['songs'][:1]
        self.assert_rejected_without_write(reply)

    def test_failed_atomic_save_does_not_publish_in_memory_translations(self):
        batch=self.batch()
        with patch('batch_translation.atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.apply(self.reply(batch))
        self.assertEqual(self.store.imports, {})
        self.assertEqual(self.store.revision, 0)
        self.assertEqual(self.store.summary(), {'total_songs': 0, 'total_lines': 0})

    def test_fill_reuses_memory_without_disk_reads(self):
        batch=self.batch();source=batch['songs'][0]['lines'];self.apply(self.reply(batch))
        self.store.fill('Song', 'Artist', 'Album', source)
        with patch('batch_translation._read', side_effect=AssertionError('disk read in rendering')):
            for _ in range(10):self.store.fill('Song', 'Artist', 'Album', source)

    def test_preparing_request_files_does_not_block_capture_or_render_fill(self):
        song=self.song()
        writing=threading.Event();release=threading.Event();rendered=threading.Event()
        errors=[];batches=[]
        def delayed_write(path, value):
            if path.parent == self.store.requests_directory:
                writing.set()
                if not release.wait(5):raise RuntimeError('test request writer timed out')
            return atomic_json(path, value)
        def prepare():
            try:batches.extend(self.store.prepare([song]))
            except Exception as error:errors.append(error)
        def render():
            try:
                rows=(Line('別の曲', '', '기존 번역'),)
                self.store.capture('Other', 'Artist', 'Album', rows)
                self.assertEqual(self.store.fill('Other', 'Artist', 'Album', rows), rows)
                rendered.set()
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=prepare, daemon=True)
        renderer=threading.Thread(target=render, daemon=True)
        with patch('batch_translation.atomic_json', side_effect=delayed_write):
            try:
                worker.start()
                self.assertTrue(writing.wait(2))
                renderer.start()
                self.assertTrue(rendered.wait(2), 'preparing request files held the rendering lock')
            finally:
                release.set()
                worker.join(5)
                if renderer.ident is not None:renderer.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(renderer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(batches), 1)

    def test_malformed_imports_are_preserved(self):
        self.store.imports_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.imports_path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):BatchTranslationStore(self.directory)
        self.assertEqual(self.store.imports_path.read_text(encoding='utf-8'), '{broken')

    def test_oversize_reply_rejected(self):
        batch=self.batch()
        with patch('batch_translation.MAX_REPLY_BYTES', 100):
            self.assert_rejected_without_write(self.reply(batch))

    def test_document_ids_and_limits_checked(self):
        for rows in ((), [Line('x', '', '')]*1501, (Line('x'*(MAX_TEXT+1), '', ''),)):
            with self.assertRaises(ValueError):self.store.capture('Song', 'Artist', 'Album', rows)
        song=self.song();song['id']='../arbitrary'
        with self.assertRaises(ValueError):self.store.prepare([song])


if __name__ == '__main__':unittest.main()
