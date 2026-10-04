from dataclasses import replace
import unittest
from unittest.mock import patch

from lyrics import Line, Song
from storage import atomic_json
import test_background_overlay as background


class SavedAnnotationsOverlayTests(unittest.TestCase):
    setUp=background.BackgroundOverlayTests.setUp
    tearDown=background.BackgroundOverlayTests.tearDown
    provider=background.BackgroundOverlayTests.provider
    receive=background.BackgroundOverlayTests.receive

    def saved_song(self):
        song=Song('saved-local','Test Song','마유즈미 후유코','Sol',tuple(
            Line(text,'saved reading '+str(index),'saved translation '+str(index))
            for index,text in enumerate(background.TEXTS)))
        self.store.save_library([song])
        self.store.save_timing(song,{'times':[1,2,3],'offset':4,'origins':['manual']*3})
        self.app.songs=[song]
        self.playback=replace(self.playback,artist='黛 冬優子 (CV.幸村恵理)',
                              album='THE IDOLM@STER SHINY COLORS HOPEFUL FE@THERS -Sol-')
        self.app.set_playback(self.playback)
        return song

    def test_library_translation_is_restored_with_spotify_direct_clock(self):
        source=self.saved_song();fingerprint=source.fingerprint
        before=(self.store.directory/'library.json').read_bytes()
        self.assertIsNone(self.app.song)
        self.receive()
        self.assertTrue(self.app.live_lyrics.is_temporary(self.app.song))
        self.assertEqual(self.app.translated_count,3)
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.assertEqual(tuple(row.original for row in self.app.display_lines),background.TEXTS)
        self.assertEqual(self.store.timing(source)['times'],[1,2,3])
        self.assertEqual(source.fingerprint,fingerprint)
        self.assertEqual((self.store.directory/'library.json').read_bytes(),before)

    def test_late_library_refresh_preserves_song_manual_state_and_existing_translation(self):
        self.playback=replace(self.playback,artist='黛 冬優子 (CV.幸村恵理)')
        self.app.set_playback(self.playback);self.receive()
        identity=self.app.song;fingerprint=identity.fingerprint
        self.app.timing={**self.app.timing,'times':[None,19.5,None],'offset':1.25,
                         'origins':[None,'manual',None],'manual_priority':True}
        self.app.save_current_timing(self.app.timing)
        timing=self.app.timing;direct=self.app.direct_result
        path=self.app.live_lyrics.timing_store.path(identity);before=path.read_bytes()
        self.app.manual_index=1;self.app.follow.set(False)
        self.app.display_lines=(replace(self.app.display_lines[0],korean='existing translation'),
                                *self.app.display_lines[1:])
        self.app.songs=[Song('new-local','Test Song','마유즈미 후유코','Sol',tuple(
            Line(text,'saved reading','saved translation') for text in background.TEXTS))]
        self.app.render()
        self.assertEqual(self.app.translated_count,3)
        self.assertEqual(self.app.display_lines[0].korean,'existing translation')
        self.assertIs(self.app.song,identity)
        self.assertEqual(identity.fingerprint,fingerprint)
        self.assertIs(self.app.timing,timing)
        self.assertIs(self.app.direct_result,direct)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.app.manual_index,1)
        self.assertFalse(self.app.follow.get())

    def test_corrupt_optional_annotations_preserve_tick_original_translation_and_manual_clock(self):
        source=self.saved_song()
        path=self.app.lyric_annotations.directory/(source.id+'.json')
        atomic_json(path,{'fingerprint':source.fingerprint,'lines':None})
        before=path.read_bytes();saved_timing=self.store.timing(source)
        self.receive()
        with patch.object(self.root,'after',wraps=self.root.after) as scheduled:
            self.app.tick()
        self.assertTrue(any(call.args[-1]==self.app.tick for call in scheduled.call_args_list))
        self.assertEqual(tuple(row.korean for row in self.app.display_lines),
                         tuple(row.korean for row in source.lines))
        self.assertEqual(self.app.direct_times(),[10,20,30])
        self.app.safe(lambda:self.app.lyric_annotations.save(source,source.lines,{}),interactive=False)
        self.assertTrue(self.app.error_notice[0])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.store.timing(source),saved_timing)


if __name__=='__main__':unittest.main()
