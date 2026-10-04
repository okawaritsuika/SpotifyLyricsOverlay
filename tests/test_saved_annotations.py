from dataclasses import replace
from types import SimpleNamespace
import unittest

from lyrics import Line, Song, match_recording
from saved_annotations import match_saved_annotations


class SavedAnnotationTests(unittest.TestCase):
    def test_library_reuses_verified_main_lyric_when_source_has_paired_aside(self):
        from test_lyric_annotations import AnnotationTests
        originals,supplied=AnnotationTests().parenthetical_document()
        source=(replace(supplied[0],original=supplied[0].original+'(風の合図)',
                        reading='',korean=supplied[0].korean+'(코러스)'),*supplied[1:])
        song=replace(self.song,lines=source)
        extras=match_saved_annotations(self.playback,originals,[song])
        self.assertEqual([line.korean for line in extras],[line.korean for line in supplied])
        self.assertFalse(extras[0].reading)
        self.assertEqual(song.lines,source)

    def setUp(self):
        self.song = Song('local', 'Test Song', '마유즈미 후유코', 'Sol', (
            Line('朝の光を見上げて', 'あさのひかりをみあげて', '아침의 빛을 올려다봐'),
            Line('明日の花を描く', 'あしたのはなをえがく', '내일의 꽃을 그려'),
            Line('最後の言葉を歌う', 'さいごのことばをうたう', '마지막 말을 노래해')))
        self.playback = SimpleNamespace(title='Test Song', artist='黛 冬優子 (CV.幸村恵理)',
                                        album='THE IDOLM@STER SHINY COLORS HOPEFUL FE@THERS -Sol-')
        self.lines = tuple(Line(text, '', '') for text in
                           ('朝の光を', '見上げて', '明日の花を', '描く', '最後の言葉を', '歌う'))

    def test_korean_artist_short_album_reuses_annotations_but_not_recording(self):
        extras = match_saved_annotations(self.playback,self.lines,[self.song])
        self.assertEqual([row.korean for row in extras],
                         [line.korean for line in self.song.lines for _ in range(2)])
        self.assertTrue(all(not row.reading for row in extras))
        self.assertIsNone(match_recording([self.song],self.playback.title,self.playback.artist,self.playback.album))

    def test_exact_rows_keep_saved_reading_and_source_song_is_unchanged(self):
        fingerprint = self.song.fingerprint
        extras = match_saved_annotations(self.playback,self.song.lines,[self.song])
        self.assertEqual(extras,self.song.lines)
        self.assertEqual(self.song.fingerprint,fingerprint)

    def test_other_performer_cover_or_short_edit_cannot_borrow_annotations(self):
        for title,artist in [('Test Song','和泉愛依'),('Test Song - Game Size',self.playback.artist),
                             ('Test Song (Live)',self.playback.artist),('Test Song',''),
                             ('Test Song','黛冬優子 tribute')]:
            with self.subTest(title=title,artist=artist):
                playback=SimpleNamespace(title=title,artist=artist,album=self.playback.album)
                self.assertFalse(any(match_saved_annotations(playback,self.lines,[self.song])))

    def test_partial_or_different_original_never_restores_by_title_alone(self):
        self.assertFalse(any(match_saved_annotations(self.playback,self.lines[:-1],[self.song])))
        other = (Line('別の物語','',''),*self.lines[1:])
        self.assertFalse(any(match_saved_annotations(self.playback,other,[self.song])))

    def test_verified_repeated_phrases_keep_sequence_specific_translations(self):
        lines=(Line('繰り返す空','','첫 번째 하늘'),Line('新しい朝','','새 아침'),
               Line('繰り返す空','','두 번째 하늘'))
        song=replace(self.song,lines=lines)
        target=tuple(Line(row.original,'','') for row in lines)
        self.assertEqual([row.korean for row in match_saved_annotations(self.playback,target,[song])],
                         [row.korean for row in lines])

    def test_conflicting_local_documents_do_not_choose_arbitrary_translation(self):
        other=replace(self.song,id='other',lines=tuple(replace(row,korean='다른 뜻') for row in self.song.lines))
        rows=match_saved_annotations(self.playback,self.lines,[self.song,other])
        self.assertFalse(any(rows))

    def test_saved_annotation_overrides_are_respected(self):
        def display(song):return tuple(replace(row,korean='수정된 뜻') for row in song.lines)
        rows=match_saved_annotations(self.playback,self.lines,[self.song],display)
        self.assertTrue(all(row.korean=='수정된 뜻' for row in rows))

    def test_unknown_artist_requires_same_whole_name_and_cv_suffix_is_explicit(self):
        song=replace(self.song,artist='A singer')
        playback=SimpleNamespace(title='Test Song',artist='A singer',album='Other label')
        self.assertTrue(all(match_saved_annotations(playback,self.lines,[song])))
        playback.artist='A singer (Live)'
        self.assertFalse(any(match_saved_annotations(playback,self.lines,[song])))

    def test_legacy_library_korean_name_uses_reviewed_spelling_alias(self):
        song=replace(self.song,artist='사이조 쥬리')
        playback=SimpleNamespace(title='Test Song',artist='西城 樹里 (CV.永井真里子)',album='Full album')
        self.assertTrue(all(match_saved_annotations(playback,self.lines,[song])))


if __name__=='__main__':unittest.main()
