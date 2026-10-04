from pathlib import Path
import tempfile
import unittest
from lyrics import Song,Line
from live_timing import LiveTimingStore
import json


class LiveTimingFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.store=LiveTimingStore(self.directory)
        self.song=Song('memory-shapes','Title','Artist','Album',(Line('First','',''),Line('Second','','')))
        self.path=self.store.path(self.song);self.path.parent.mkdir(parents=True)

    def tearDown(self):self.temp.cleanup()

    def test_bad_file_shapes_raise_value_error_preserve_bytes_and_allow_repair(self):
        valid={'signature':self.store.signature(self.song),'times':[10,20]}
        invalid=[None,[],[1],'invalid',True,{'signature':valid['signature']}]
        invalid.extend({**valid,**change} for change in (
            {'times':None},{'times':[10]},{'times':[20,10]},{'times':[False,20]},
            {'times':[10,10**400]},{'offset':10**400},{'offset':None},
            {'offset':True},{'estimate_intro':10**400},{'estimate_outro':-1},
            {'automatic_times':[20,10]}))
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value),encoding='utf-8');before=self.path.read_bytes()
                with self.assertRaises(ValueError):self.store.load(self.song)
                self.assertEqual(self.path.read_bytes(),before)
                self.path.write_text(json.dumps(valid),encoding='utf-8')
                self.assertEqual(self.store.load(self.song)['times'],[10,20])

    def test_invalid_save_does_not_replace_existing_user_times(self):
        self.store.save(self.song,{'times':[10,20],'offset':.5})
        before=self.path.read_bytes()
        for value in (None,[],{}, {'times':None}, {'times':[10,20],'offset':10**400}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.store.save(self.song,value)
                self.assertEqual(self.path.read_bytes(),before)

    def test_other_lyric_signature_stays_incompatible_without_modifying_file(self):
        self.path.write_text(json.dumps({'signature':'another document','times':None}),encoding='utf-8')
        before=self.path.read_bytes()
        self.assertIsNone(self.store.load(self.song))
        self.assertEqual(self.path.read_bytes(),before)

class LiveTimingTests(unittest.TestCase):
    def test_later_document_cannot_discard_or_shift_a_manual_row_onset(self):
        from live_lyrics import LiveLyrics
        from media import Playback
        old=(Line('abcd','',''),Line('ef','',''),Line('Sunrise','',''))
        variants=((Line('abcdef','','첫 두 구절'),old[2]),
                  (Line('ab','','첫 부분'),Line('cdef','','나머지'),old[2]),
                  (old[0],old[2]))
        for incoming in variants:
            with self.subTest(incoming=incoming):
                live=LiveLyrics();playback=Playback(title='Example',artist='Singer',album='Album')
                song=live.receive(playback,None,old)
                timing={'times':[10,20,30],'origins':['manual']*3,'offset':.5}
                live.timing=timing
                result=live.receive(playback,None,incoming)
                self.assertEqual([line.original for line in result.lines],[line.original for line in song.lines])
                self.assertIs(live.timing,timing)
                self.assertEqual(live.timing['times'],[10,20,30])

    def test_safe_regrouping_keeps_manual_onsets_and_leaves_unmeasured_rows_empty(self):
        from live_lyrics import LiveLyrics
        from media import Playback
        live=LiveLyrics();playback=Playback(title='Example',artist='Singer',album='Album')
        live.receive(playback,None,(Line('abcdef','',''),Line('Sunrise','','')))
        live.timing={'times':[10,30],'origins':['manual']*2,'offset':.5}
        live.receive(playback,None,(Line('ab','',''),Line('cdef','',''),Line('Sunrise','','')))
        self.assertEqual(live.timing['times'],[10,None,30])
        self.assertEqual(live.timing['origins'],['manual',None,'manual'])
        self.assertEqual(live.timing['offset'],.5)

    def test_same_lyrics_keep_separate_timestamps_for_recordings(self):
        from live_lyrics import LiveLyrics
        from media import Playback
        with tempfile.TemporaryDirectory() as directory:
            store=LiveTimingStore(Path(directory));live=LiveLyrics(store)
            lines=(Line('窓を開ける','','창문을 열다'),)
            original=Playback(title='蒼い鳥',artist='Same Artist',album='Same Album')
            recording=Playback(title='蒼い鳥 (OathOne Recording)',artist=original.artist,album=original.album)
            first=live.receive(original,None,lines)
            store.save(first,{'times':[12.5],'offset':.3})
            second=live.receive(recording,None,lines)
            self.assertNotEqual(store.path(first),store.path(second))
            self.assertEqual(store.signature(first),store.signature(second))
            self.assertEqual(live.timing['times'],[None])
            store.save(second,{'times':[14.2],'offset':-.2})
            live.receive(original,None,lines)
            self.assertEqual(live.timing['times'],[12.5]);self.assertEqual(live.timing['offset'],.3)
            live.receive(recording,None,lines)
            self.assertEqual(live.timing['times'],[14.2]);self.assertEqual(live.timing['offset'],-.2)

    def test_only_timestamps_survive_restart_and_changed_lyrics_invalidate_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store=LiveTimingStore(Path(directory));song=Song('memory-example','Title','Artist','',(Line('窓を開ける','まど','번역'),))
            store.save(song,{'times':[12.5],'offset':.3,'source':'must not persist arbitrary text'})
            body=store.path(song).read_text(encoding='utf-8')
            for text in ['窓を開ける','まど','번역','must not persist','Title','Artist']:self.assertNotIn(text,body)
            restored=LiveTimingStore(Path(directory)).load(song);self.assertEqual(restored['times'],[12.5])
            translated=Song(song.id,song.title,'','',(Line('窓を開ける','changed','changed'),))
            self.assertEqual(store.load(translated)['times'],[12.5])
            changed=Song(song.id,song.title,'','',(Line('空を見る','',''),))
            self.assertIsNone(store.load(changed))
            with self.assertRaises(ValueError):store.save(song,{'times':[12.5],'offset':float('nan')})
