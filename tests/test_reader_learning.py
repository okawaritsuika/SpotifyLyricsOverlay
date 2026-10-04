import hashlib
import queue
import unittest
from unittest.mock import patch

from lyrics import Line
from media import Playback
from reader_bridge import ReaderBridge


class ReaderLearningTests(unittest.TestCase):
    def setUp(self):
        self.messages=queue.Queue()
        self.bridge=ReaderBridge(messages=self.messages)
        self.playback=Playback(title='Example Song',artist='Example Artist',album='Example Album',playing=True)
        self.bridge.observe(self.playback)
        self.lines=(Line('窓を開けて空を見る','reading','창문을 열고 하늘을 봐'),
                    Line('花を描いて歩き出す','reading','꽃을 그리고 걸어가'))

    def payload(self,**changes):
        state=self.bridge.snapshot()
        return {**{'track':state['track'],'token':state['token'],
                   'url':'https://namu.wiki/w/Example%20Song','text':'transient source text'},**changes}

    def receive(self,**changes):
        with patch('reader_bridge.parse_table_text',return_value=self.lines):
            return self.bridge.receive(self.payload(**changes))

    def take_lyrics(self):
        kind,value=self.messages.get_nowait()
        self.assertEqual(kind,'reader_lyrics')
        self.assertEqual(value[1],self.lines)
        return value[0]

    def learned(self,url):return {'url':url,'learned':True}

    def test_non_ascii_session_token_is_rejected_without_changing_receipt(self):
        receipt=self.bridge.snapshot()['receipt']
        with self.assertRaises(ValueError):self.bridge.receive(self.payload(token='잘못된 연결'))
        self.assertEqual(self.bridge.snapshot()['receipt'],receipt)
        self.assertTrue(self.messages.empty())

    def test_exact_title_learns_only_after_successful_display_and_never_sends_lyrics_for_storage(self):
        self.assertEqual(self.receive(),'queued')
        track=self.take_lyrics()
        self.assertTrue(self.messages.empty())
        self.assertEqual(self.bridge.document_urls,{})
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),
                         ('reader_document',(track,self.learned(self.payload()['url']))))
        self.assertTrue(self.messages.empty())
        self.assertEqual(self.bridge.document_urls,{})
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_reference_match_can_learn_a_different_document_title(self):
        self.bridge.set_reference(self.playback.key,[line.original for line in self.lines])
        url='https://namu.wiki/w/Canonical%20Document'
        self.assertEqual(self.receive(url=url),'queued')
        track=self.take_lyrics();self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,self.learned(url))))

    def test_existing_approval_paths_learn_after_display(self):
        for mode in ('title','metadata','unique_title','selected','explicit'):
            with self.subTest(mode=mode):
                url='https://namu.wiki/w/Example%20Song(Series)'
                changes={'url':url,'match_mode':mode}
                if mode=='selected':changes['user_selected']=True;changes['url']='https://namu.wiki/w/Selected'
                if mode=='explicit':
                    changes['url']='https://namu.wiki/w/Previously%20Approved'
                    self.bridge.document_urls[self.bridge.snapshot()['track']]=changes['url']
                self.assertEqual(self.receive(**changes),'queued')
                track=self.take_lyrics();self.assertTrue(self.messages.empty())
                self.bridge.set_receipt(track,'displayed',len(self.lines))
                document=changes['url'] if mode in ('selected','explicit') else self.learned(changes['url'])
                self.assertEqual(self.messages.get_nowait(),('reader_document',(track,document)))

    def test_failed_display_does_not_learn_even_for_user_selection(self):
        self.receive(url='https://namu.wiki/w/Selected',user_selected=True)
        track=self.take_lyrics();self.bridge.set_receipt(track,'display_failed')
        self.assertTrue(self.messages.empty())
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_queued_pages_consume_acknowledgements_in_delivery_order(self):
        first='https://namu.wiki/w/First';second='https://namu.wiki/w/Second'
        self.receive(url=first,user_selected=True);self.receive(url=second,user_selected=True)
        track=self.take_lyrics();self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,first)))
        self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[first])
        self.bridge.set_receipt(track,'display_failed')
        self.assertTrue(self.messages.empty())
        self.assertNotIn('translation_sources',self.bridge.snapshot()['receipt'])
        self.receive(url=first,user_selected=True);self.receive(url=second,user_selected=True)
        self.take_lyrics();self.take_lyrics()
        self.bridge.set_receipt(track,'display_failed')
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,second)))
        self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[second])

    def test_each_display_acknowledgement_uses_its_own_translation_sources(self):
        first='https://namu.wiki/w/First';second='https://namu.wiki/w/Second'
        self.receive(url=first,user_selected=True);self.receive(url=second,user_selected=True)
        track=self.take_lyrics();self.take_lyrics()
        for url in (first,second):
            self.bridge.set_receipt(track,'displayed',len(self.lines))
            self.assertEqual(self.messages.get_nowait(),('reader_document',(track,url)))
            self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[url])

    def test_original_only_delivery_does_not_inherit_previous_source(self):
        first='https://namu.wiki/w/First';second='https://namu.wiki/w/Second'
        self.receive(url=first,user_selected=True);track=self.take_lyrics()
        originals=tuple(Line(line.original,line.reading,'') for line in self.lines)
        with patch('reader_bridge.parse_table_text',return_value=originals):
            self.bridge.receive(self.payload(url=second,user_selected=True))
        self.messages.get_nowait()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[first])
        self.bridge.set_receipt(track,'displayed',len(originals),0)
        self.assertNotIn('translation_sources',self.bridge.snapshot()['receipt'])

    def test_track_change_clears_queued_translation_sources(self):
        self.receive(url='https://namu.wiki/w/Old',user_selected=True);old_track=self.take_lyrics()
        self.bridge.observe(Playback(title='Other Song',playing=True))
        new='https://namu.wiki/w/New'
        self.receive(url=new,user_selected=True);track=self.take_lyrics()
        self.bridge.set_receipt(old_track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,new)))
        self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[new])

    def test_track_change_and_old_payloads_cannot_learn(self):
        old=self.payload();self.receive();track=self.take_lyrics()
        self.bridge.observe(Playback(title='Other Song',playing=True))
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())
        self.assertEqual(self.bridge.receive(old),'stale')
        self.bridge.observe(self.playback)
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_track_change_during_parsing_cannot_queue_or_learn_the_old_document(self):
        def parse(*args,**kwargs):
            self.bridge.observe(Playback(title='Other Song',playing=True))
            return self.lines
        with patch('reader_bridge.parse_table_text',side_effect=parse):
            self.assertEqual(self.bridge.receive(self.payload()),'stale')
        self.assertTrue(self.messages.empty())

    def test_expired_playback_snapshot_cannot_queue_or_learn(self):
        with patch('reader_bridge.time.monotonic',return_value=self.bridge.updated+6):
            self.assertEqual(self.receive(),'stale')
        self.bridge.set_receipt(self.bridge.snapshot()['track'],'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_rejected_reference_mismatch_never_becomes_a_learning_candidate(self):
        self.bridge.set_reference(self.playback.key,['different reference phrase','another unmatched phrase'])
        with self.assertRaisesRegex(ValueError,'일치하는 구절'):
            self.receive()
        self.bridge.set_receipt(self.bridge.snapshot()['track'],'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_invalid_delivery_cannot_learn_or_consume_an_earlier_display_ack(self):
        self.receive();track=self.take_lyrics()
        with patch('reader_bridge.parse_table_text',side_effect=ValueError('invalid table')):
            with self.assertRaises(ValueError):self.bridge.receive(self.payload())
        self.assertTrue(self.messages.empty())
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,self.learned(self.payload()['url']))))
        self.assertEqual(self.bridge.snapshot()['receipt']['translation_sources'],[self.payload()['url']])
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_learning_keys_keep_artist_album_and_version_separate(self):
        tracks=[]
        for playback in (self.playback,
                         Playback(title=self.playback.title,artist='Other Artist',album=self.playback.album),
                         Playback(title=self.playback.title,artist=self.playback.artist,album='Other Album'),
                         Playback(title='Example Song (OathOne Recording)',artist=self.playback.artist,album=self.playback.album)):
            self.bridge.observe(playback)
            self.receive(user_selected=True)
            track=self.take_lyrics();self.bridge.set_receipt(track,'displayed',len(self.lines))
            self.assertEqual(self.messages.get_nowait(),('reader_document',(track,self.payload()['url'])))
            self.assertEqual(track,hashlib.sha256(playback.key.encode()).hexdigest())
            tracks.append(track)
        self.assertEqual(len(set(tracks)),len(tracks))

    def test_learned_url_is_reused_without_becoming_manual_approval(self):
        self.receive();track=self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        _,(_,document)=self.messages.get_nowait()
        self.bridge.document_urls[track]=document
        state=self.bridge.snapshot()
        self.assertEqual(state['document_url'],self.payload()['url'])
        self.assertTrue(state['document_learned'])
        self.assertEqual(self.receive(),'queued');self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,document)))

    def test_learned_url_cannot_override_new_conflicting_spotify_references(self):
        track=self.bridge.snapshot()['track'];url=self.payload()['url']
        self.bridge.document_urls[track]=self.learned(url)
        self.bridge.set_reference(self.playback.key,['different reference phrase','another unmatched phrase'])
        with self.assertRaisesRegex(ValueError,'일치하는 구절'):
            self.receive()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())

    def test_learned_url_with_matching_references_remains_learned(self):
        track=self.bridge.snapshot()['track'];url=self.payload()['url']
        self.bridge.document_urls[track]=self.learned(url)
        self.bridge.set_reference(self.playback.key,[line.original for line in self.lines])
        self.assertEqual(self.receive(),'queued');self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,self.learned(url))))

    def test_legacy_manual_url_keeps_explicit_approval_when_references_conflict(self):
        track=self.bridge.snapshot()['track'];url=self.payload()['url']
        self.bridge.document_urls[track]=url
        self.bridge.set_reference(self.playback.key,['different reference phrase','another unmatched phrase'])
        self.assertFalse(self.bridge.snapshot()['document_learned'])
        self.assertEqual(self.receive(),'queued');self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,url)))

    def test_automatic_success_cannot_replace_a_different_manual_url(self):
        track=self.bridge.snapshot()['track'];manual='https://namu.wiki/w/Manually%20Chosen'
        self.bridge.document_urls[track]=manual
        self.assertEqual(self.receive(),'queued');self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertTrue(self.messages.empty())
        self.assertEqual(self.bridge.document_urls[track],manual)
        self.assertEqual(self.receive(user_selected=True),'queued');self.take_lyrics()
        self.bridge.set_receipt(track,'displayed',len(self.lines))
        self.assertEqual(self.messages.get_nowait(),('reader_document',(track,self.payload()['url'])))


if __name__=='__main__':unittest.main()
