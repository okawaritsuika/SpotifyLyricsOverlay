from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from lyric_annotations import _local_reading
from lyrics import Line
from user_annotations import UserAnnotations
import test_background_overlay as background


class ReadingOverlayTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive
    position=background.BackgroundOverlayTests.position

    def test_raw_spotify_without_user_annotations_gets_display_only_reading(self):
        self.receive()
        song=self.app.song;fingerprint=song.fingerprint;timing=self.app.timing;direct=self.app.direct_result
        cache=next(self.app.lyric_cache.directory.glob('*.json'))
        before=cache.read_bytes()
        self.assertTrue(all(self.app.line_readings))
        self.assertTrue(all(not line.reading for line in self.app.display_lines))
        self.assertEqual(self.app.translated_count,0)
        for seconds,index in ((11,0),(21,1),(31,2)):
            self.position(seconds)
            self.assertEqual(self.app.reading_label.cget('text'),self.app.line_readings[index])
        self.assertIs(self.app.song,song);self.assertEqual(song.fingerprint,fingerprint)
        self.assertIs(self.app.timing,timing);self.assertIs(self.app.direct_result,direct)
        self.assertEqual(cache.read_bytes(),before)

    def test_split_user_sentence_gets_own_readings_and_late_source_reading_wins(self):
        path=self.store.directory/'user_lyrics'/'song.json';path.parent.mkdir()
        path.write_text(json.dumps({'schema_version':1,'title':'Test Song','artists':['Singer'],
            'source':{'kind':'user_provided'},'lines':[
                {'original':'青い空を見ながら歩こう','reading':'source whole sentence','korean':'뜻'},
                {'original':'花を描く','reading':'supplied exact reading','korean':'뜻'}]},ensure_ascii=False),encoding='utf-8')
        self.app.user_annotations=UserAnnotations(self.store.directory)
        texts=('青い空を','見ながら歩こう','花を描く');self.receive(texts=texts)
        self.assertTrue(all(self.app.line_readings))
        self.assertEqual([line.reading for line in self.app.display_lines],['','','supplied exact reading'])
        self.assertNotIn('source whole sentence',self.app.line_readings)
        incoming=tuple(Line(text,'late source '+str(index),'') for index,text in enumerate(texts))
        self.app.merge_current_annotations(incoming);self.app.render()
        self.assertEqual(self.app.reading_label.cget('text'),'late source 0')
        self.assertEqual(self.app.line_readings,('late source 0','late source 1','supplied exact reading'))

    def test_hidden_setting_and_blank_english_rows_do_not_reuse_previous_reading(self):
        self.receive(texts=('空を見る','English only',''))
        self.assertTrue(self.app.reading_label.cget('text'))
        self.app.show_reading.set(False);self.app.render()
        self.assertEqual(self.app.reading_label.cget('text'),'')
        self.app.show_reading.set(True);self.app.render()
        self.assertTrue(self.app.reading_label.cget('text'))
        for position in (21,31):
            self.position(position)
            self.assertEqual(self.app.reading_label.cget('text'),'')

    def test_song_change_error_and_tainted_source_clear_generated_readings(self):
        self.receive();self.assertTrue(all(self.app.line_readings))
        self.app.screen_cache_tainted=True;self.app.render()
        self.assertEqual(self.app.line_readings,());self.assertEqual(self.app.reading_label.cget('text'),'')
        self.app.screen_cache_tainted=False;self.app.render()
        self.assertTrue(all(self.app.line_readings))
        self.app.set_playback(replace(self.playback,error='temporary playback failure'));self.app.render()
        self.assertEqual(self.app.line_readings,());self.assertEqual(self.app.reading_label.cget('text'),'')
        self.playback=replace(self.playback,title='Another song')
        self.app.set_playback(self.playback);self.app.render()
        self.assertEqual(self.app.line_readings,());self.assertEqual(self.app.reading_label.cget('text'),'')
        self.receive(texts=('English first','English second','English third'))
        self.assertEqual(self.app.line_readings,('','',''))
        self.assertEqual(self.app.reading_label.cget('text'),'')

    def test_same_index_rerenders_when_failed_converter_recovers(self):
        _local_reading.cache_clear()
        try:
            with patch('web_lyrics.reading',side_effect=RuntimeError('temporary')):
                self.receive()
                self.assertEqual(self.app.reading_label.cget('text'),'')
            self.app.render()
            self.assertEqual(self.app.displayed_index(),0)
            self.assertTrue(self.app.reading_label.cget('text'))
        finally:_local_reading.cache_clear()


if __name__=='__main__':unittest.main()
