import json
import queue
import threading
import unittest
from email.message import Message
from unittest.mock import Mock,patch
from urllib.error import HTTPError

from spotify_worker import SpotifyBrowser


TRACK={'id':'a'*22,'title':'Fixture song','artists':['Fixture artist'],'album':'Fixture album','duration':210.0}


def lyrics_payload(sync_type='LINE_SYNCED'):
    return {'lyrics':{'syncType':sync_type,'lines':[
        {'words':'fixture first','startTimeMs':'1000'},
        {'words':'fixture second','startTimeMs':'2000'}]},'private':'response detail'}


class SpotifyBatchLyricsTests(unittest.TestCase):
    def setUp(self):
        self.window=Mock();self.browser=SpotifyBrowser(self.window,queue.Queue(),threading.Event())
        self.browser.authenticated=True;self.browser.authorization='Bearer private token';self.browser.client_token='private client'
        self.command={'action':'batch_lyrics','request_id':'batch-1','track':dict(TRACK)}
        self.threads=[]
        class DeferredThread:
            def __init__(inner,target,**kwargs):self.threads.append(target)
            def start(inner):pass
        for name,replacement in (('spotify_worker.threading.Thread',DeferredThread),('spotify_worker.emit',None)):
            patcher=patch(name,replacement) if replacement else patch(name)
            value=patcher.start();self.addCleanup(patcher.stop)
            if name.endswith('emit'):self.emit=value

    def start(self,**changes):self.browser.command({**self.command,**changes})

    def fetch(self,payload=None,error=None,index=-1):
        with patch('spotify_worker.fetch_lyrics',return_value=payload or lyrics_payload(),side_effect=error) as fetch:
            self.threads[index]()
        while not self.browser.network.empty():self.browser.receive(self.browser.network.get_nowait())
        return fetch

    def result(self):
        self.assertEqual(self.emit.call_args.args,('batch_lyrics_result',))
        return self.emit.call_args.kwargs

    def test_exact_track_id_is_read_without_navigation_playback_or_current_lyric_changes(self):
        pending={'key':'currently playing'};self.browser.pending=pending
        before=(self.browser.generation,self.browser.last_status,self.browser.active_query)
        self.start();fetch=self.fetch()
        fetch.assert_called_once_with(TRACK['id'],'Bearer private token','private client')
        self.assertEqual(self.window.method_calls,[])
        self.assertIs(self.browser.pending,pending)
        self.assertEqual((self.browser.generation,self.browser.last_status,self.browser.active_query),before)
        result=self.result()
        self.assertEqual(result['provider_id'],TRACK['id']);self.assertTrue(result['synced'])
        self.assertEqual(result['lines'],[{'text':'fixture first','time':1.0},{'text':'fixture second','time':2.0}])
        self.assertEqual(result['source'],'Spotify');self.assertEqual(result['request_id'],'batch-1')
        for private in ('private token','private client','response detail','Authorization'):
            self.assertNotIn(private,json.dumps(result))

    def test_invalid_id_or_incomplete_metadata_never_issues_request(self):
        for values in ({'id':'bad/track'},{'id':'https://private.example/'},{'title':''},{'artists':[]},
                       {'duration':True},{'duration':float('inf')},{'album':None}):
            with self.subTest(values=values):
                self.start(track={**TRACK,**values})
                self.assertEqual(self.result()['code'],'query')
                self.assertEqual(self.threads,[])
        count=self.emit.call_count
        self.start(request_id='private request token')
        self.assertEqual(self.emit.call_count,count)

    def test_busy_current_lyrics_and_catalog_work_are_not_interrupted(self):
        for field,value in (('fetching',True),('active_query','current title'),('search_deadline',100),
                            ('catalog_pending',{'request_id':'search'}),('catalog_active',{'request_id':'search'}),
                            ('playlist',{'request_id':'playlist'})):
            with self.subTest(field=field):
                old=getattr(self.browser,field);setattr(self.browser,field,value)
                self.start();self.assertEqual(self.result()['code'],'busy')
                self.assertEqual(getattr(self.browser,field),value);setattr(self.browser,field,old)
        self.assertEqual(self.threads,[])

    def test_one_batch_request_at_a_time_and_next_request_waits_after_response(self):
        self.start();active=self.browser.batch
        self.start(request_id='batch-2');self.assertEqual(self.result()['code'],'busy')
        self.assertIs(self.browser.batch,active);self.assertEqual(len(self.threads),1)
        self.fetch();self.start(request_id='batch-2')
        self.assertEqual(self.result()['code'],'busy');self.assertEqual(len(self.threads),1)
        self.browser.batch_next_at=0;self.start(request_id='batch-2');self.assertEqual(len(self.threads),2)

    def test_cancelled_request_drops_late_success_and_remains_serial_until_socket_returns(self):
        self.start();self.browser.command({'action':'batch_lyrics_cancel','request_id':'batch-1'})
        self.assertEqual(self.result()['code'],'cancelled')
        self.start(request_id='batch-2');self.assertEqual(self.result()['code'],'busy')
        count=self.emit.call_count;self.fetch();self.assertEqual(self.emit.call_count,count)
        self.assertIsNone(self.browser.batch_inflight)

    def test_reused_request_id_and_wrong_track_id_cannot_accept_an_old_response(self):
        self.start();generation=self.browser.batch['generation']
        self.browser.command({'action':'catalog_cancel','request_id':'batch-1'});self.fetch()
        self.browser.batch_next_at=0;self.start();current=self.browser.batch['generation']
        count=self.emit.call_count
        self.browser.receive(('batch_lyrics',generation,'batch-1',TRACK['id'],lyrics_payload()))
        self.browser.receive(('batch_lyrics',current,'batch-1','z'*22,lyrics_payload()))
        self.assertEqual(self.emit.call_count,count);self.assertIsNotNone(self.browser.batch)
        self.fetch();self.assertTrue(self.result()['synced'])

    def test_new_current_lyric_lookup_takes_priority_and_batch_never_emits_current_status(self):
        self.start()
        with patch.object(self.browser,'begin_search') as begin:
            self.browser.command({'action':'lookup','key':'new-song','title':'New current song'})
        self.assertEqual(self.result()['code'],'interrupted');begin.assert_called_once()
        count=self.emit.call_count;self.fetch();self.assertEqual(self.emit.call_count,count)
        self.assertEqual(self.browser.pending['key'],'new-song')

    def test_http_errors_are_typed_and_response_text_is_not_returned(self):
        for status,code in ((404,'not_found'),(401,'login_required'),(403,'login_required'),(503,'response')):
            self.browser.batch_next_at=0;self.start()
            error=HTTPError('https://spclient.wg.spotify.com/private',status,'private server detail',Message(),None)
            self.fetch(error=error)
            self.assertEqual(self.result()['code'],code);self.assertEqual(self.result()['lines'],[])
            self.assertNotIn('private',json.dumps(self.result()))

    def test_rate_limit_honors_retry_after_across_track_ids_even_after_cancellation(self):
        self.start();self.browser.command({'action':'batch_lyrics_cancel','request_id':'batch-1'})
        headers=Message();headers['Retry-After']='73'
        self.fetch(error=HTTPError('https://spclient.wg.spotify.com/',429,'private',headers,None))
        self.start(request_id='batch-2',track={**TRACK,'id':'b'*22})
        result=self.result()
        self.assertEqual(result['code'],'rate_limited');self.assertGreaterEqual(result['retry_after'],72)
        self.assertEqual(len(self.threads),1)

    def test_active_rate_limit_error_reports_server_delay(self):
        self.start();headers=Message();headers['Retry-After']='91'
        self.fetch(error=HTTPError('https://spclient.wg.spotify.com/',429,'private',headers,None))
        self.assertEqual(self.result()['code'],'rate_limited');self.assertEqual(self.result()['retry_after'],91)

    def test_unsynced_and_corrupt_timing_cannot_be_reported_as_verified_sync(self):
        for value in (lyrics_payload('UNSYNCED'),lyrics_payload()):
            if value['lyrics']['syncType']=='LINE_SYNCED':value['lyrics']['lines'][1]['startTimeMs']='999999'
            self.browser.batch_next_at=0;self.start();self.fetch(value)
            self.assertFalse(self.result()['synced']);self.assertEqual(self.result()['code'],'')
            self.assertTrue(all(row['time'] is None for row in self.result()['lines']))

    def test_malformed_lyric_payload_is_not_success_or_empty_completion(self):
        self.start();self.fetch({'lyrics':{'syncType':'LINE_SYNCED','lines':[]}})
        self.assertEqual(self.result()['code'],'response');self.assertEqual(self.result()['lines'],[])

    def test_recent_verified_browser_credentials_stay_inside_worker(self):
        self.browser.receive(('batch_credentials','Bearer newest secret','newest client'))
        self.start();fetch=self.fetch()
        fetch.assert_called_once_with(TRACK['id'],'Bearer newest secret','newest client')
        self.assertNotIn('newest',str(self.emit.call_args_list))

    def test_missing_authentication_never_navigates_or_starts_fetch(self):
        self.browser.authenticated=False;self.start()
        self.assertEqual(self.result()['code'],'login_required')
        self.assertEqual(self.window.method_calls,[]);self.assertEqual(self.threads,[])

    def test_close_cancels_batch_and_late_result_does_not_resurrect_it(self):
        self.start();self.assertFalse(self.browser.command({'action':'close'}))
        self.assertEqual(self.result()['code'],'cancelled')
        count=self.emit.call_count;self.fetch();self.assertEqual(self.emit.call_count,count)


if __name__=='__main__':unittest.main()
