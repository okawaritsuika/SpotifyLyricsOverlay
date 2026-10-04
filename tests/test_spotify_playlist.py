import json
import queue
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlparse

from spotify_playlist import observed_request,playlist_request_diagnostic,playlist_template,playlist_page
from spotify_worker import SpotifyBrowser,fetch_playlist_page,retry_after_seconds

PLAYLIST='1XJr6hI0TlEHbMa1nSmVNp'
TRACK='0123456789ABCDEFGHIJKL'
ENDPOINT='https://api-partner.spotify.com/pathfinder/v2/query'


def template(offset=0,method='POST'):
    value={'operationName':'fetchPlaylist','variables':{'uri':'spotify:playlist:'+PLAYLIST,'offset':offset,'limit':2},
           'extensions':{'persistedQuery':{'version':1,'sha256Hash':'a'*64}}}
    return observed_request(ENDPOINT,json.dumps(value),method)


def payload(ids=(TRACK,),offset=0,total=None):
    items=[]
    for identifier in ids:
        data={'__typename':'Track','uri':'spotify:track:'+identifier,'name':'Song',
              'artists':{'items':[{'profile':{'name':'Artist'}}]},'albumOfTrack':{'name':'Album'},
              'duration':{'totalMilliseconds':200000}}
        items.append({'itemV2':{'data':data}})
    return {'data':{'playlistV2':{'uri':'spotify:playlist:'+PLAYLIST,'content':{
        'items':items,'totalCount':len(items)+offset if total is None else total,'pagingInfo':{'offset':offset}}}}}


class PlaylistParsingTests(unittest.TestCase):
    def test_only_observed_playlist_read_requests_on_exact_host_are_replayed(self):
        request=template()
        self.assertEqual(playlist_template(request,PLAYLIST),request)
        for name in ('addToPlaylist','removeFromPlaylist','searchTracks','mutationPlaylist'):
            self.assertIsNone(playlist_template({**request,'operationName':name},PLAYLIST))
        for url in ('http://api-partner.spotify.com/pathfinder/v2/query',
                    ENDPOINT.replace('api-partner.spotify.com','api-partner.spotify.com.evil.example'),
                    ENDPOINT.replace('api-partner.spotify.com','user@api-partner.spotify.com'),
                    ENDPOINT.replace('v2/query','delete')):
            self.assertIsNone(observed_request(url,'{}','POST'))
        for key,value in (('path','//evil.example'),('method','DELETE')):
            self.assertIsNone(playlist_template({**request,key:value},PLAYLIST))
        self.assertIsNone(playlist_template(request,'z'*22))

    def test_template_requires_observed_pagination_and_persisted_query(self):
        request=template()
        for key,value in (('offset',None),('offset',True),('offset',-1),('limit',0),('limit',501)):
            self.assertIsNone(playlist_template({**request,'variables':{**request['variables'],key:value}},PLAYLIST))
        self.assertIsNone(playlist_template({**request,'extensions':{}},PLAYLIST))
        self.assertIsNone(playlist_template({**request,'extensions':{'persistedQuery':{'version':1,'sha256Hash':'token'}}},PLAYLIST))

    def test_request_diagnostic_has_keys_not_private_values(self):
        request=template();request['variables']['privateToken']='secret'
        value=playlist_request_diagnostic(request,PLAYLIST)
        self.assertEqual(value['variable_keys'],['uri','offset','limit'])
        self.assertTrue(value['playlist_matches'])
        for text in ('secret','privateToken',PLAYLIST,'https://','a'*64):self.assertNotIn(text,json.dumps(value))

    def test_raw_rows_advance_offset_even_for_unavailable_local_items(self):
        value=payload(total=3)
        items=value['data']['playlistV2']['content']['items']
        items.extend([{'itemV2':{'data':{'__typename':'LocalTrack','uri':'spotify:local:private'}}},
                      {'itemV2':{'data':{'__typename':'NotFound'}}}])
        page=playlist_page(value,PLAYLIST,0)
        self.assertEqual((len(page['tracks']),page['offset'],page['total']),(1,3,3))
        self.assertTrue(page['complete']);self.assertIsNone(page['tracks'][0]['is_playable'])
        self.assertNotIn('spotify:local',json.dumps(page['tracks']))

    def test_playlist_track_uri_on_wrapper_preserves_track_metadata_and_duplicate_rows(self):
        value=payload((TRACK,TRACK),offset=50,total=52)
        for item in value['data']['playlistV2']['content']['items']:
            wrapper=item['itemV2']
            wrapper['_uri']=wrapper['data'].pop('uri')
        page=playlist_page(value,PLAYLIST,50)
        self.assertEqual([row['status'] for row in page['entries']],['track','track'])
        self.assertEqual([row['index'] for row in page['entries']],[50,51])
        self.assertEqual([row['track_id'] for row in page['entries']],[TRACK,TRACK])
        self.assertEqual(len(page['tracks']),1)
        self.assertEqual(page['tracks'][0]['artists'],['Artist'])
        self.assertEqual(page['tracks'][0]['album'],'Album')
        self.assertEqual(page['tracks'][0]['duration'],200)

    def test_observed_playlist_track_duration_and_artist_items_without_total_count(self):
        value=payload()
        data=value['data']['playlistV2']['content']['items'][0]['itemV2']['data']
        del data['duration']
        data['trackDuration']={'totalMilliseconds':314293}
        data['artists']['items'].append({'profile':{'name':'Second Artist'}})
        page=playlist_page(value,PLAYLIST,0)
        self.assertEqual(page['entries'][0]['status'],'track')
        self.assertEqual(page['entries'][0]['track_id'],TRACK)
        self.assertEqual(page['tracks'][0]['duration'],314.293)
        self.assertEqual(page['tracks'][0]['artists'],['Artist','Second Artist'])
        self.assertNotIn('duration',data)

    def test_track_duration_cannot_replace_present_invalid_duration(self):
        for duration in (None,{}, {'totalMilliseconds':0}):
            value=payload()
            data=value['data']['playlistV2']['content']['items'][0]['itemV2']['data']
            data['duration']=duration
            data['trackDuration']={'totalMilliseconds':314293}
            page=playlist_page(value,PLAYLIST,0)
            self.assertEqual(page['entries'][0]['status'],'incomplete')
            self.assertEqual(page['tracks'],[])

    def test_graphql_errors_missing_total_wrong_playlist_and_offset_cannot_complete(self):
        values=[{'errors':[{'message':'private'}]},payload(offset=1),payload()]
        values[2]['data']['playlistV2']['uri']='spotify:playlist:'+'z'*22
        empty=payload(ids=(),total=3);values.append(empty)
        missing=payload();del missing['data']['playlistV2']['content']['totalCount'];values.append(missing)
        for value in values:
            with self.assertRaises(ValueError):playlist_page(value,PLAYLIST,0)

    def test_one_page_can_exceed_old_search_reader_one_hundred_track_limit(self):
        value=payload(tuple(str(index).zfill(22) for index in range(150)))
        self.assertEqual(len(playlist_page(value,PLAYLIST,0)['tracks']),150)

    def test_ordered_entries_preserve_duplicates_and_every_unavailable_row(self):
        value=payload((TRACK,TRACK),offset=4,total=10)
        items=value['data']['playlistV2']['content']['items']
        items.extend([None, {'itemV2':{'data':{'__typename':'LocalTrack','uri':'spotify:local:private',
                                            'name':'Private local filename'}}},
                      {'itemV2':{'data':{'__typename':'NotFound'}}},
                      {'itemV2':{'data':{'__typename':'Episode','uri':'spotify:episode:private'}}}])
        page=playlist_page(value,PLAYLIST,4)
        self.assertEqual(page['start'],4)
        self.assertEqual([row['index'] for row in page['entries']],list(range(4,10)))
        self.assertEqual([row['status'] for row in page['entries']],
                         ['track','track','unavailable','local','unavailable','unsupported'])
        self.assertEqual([row['track_id'] for row in page['entries'][:2]],[TRACK,TRACK])
        self.assertEqual(len(page['tracks']),1)
        self.assertEqual(page['count'],6)
        self.assertNotIn('private',json.dumps(page['entries']))
        self.assertNotIn('Private local filename',json.dumps(page['entries']))

    def test_incomplete_track_keeps_public_id_for_a_later_metadata_lookup(self):
        value=payload()
        value['data']['playlistV2']['content']['items'][0]['itemV2']['data']['artists']['totalCount']=3
        page=playlist_page(value,PLAYLIST,0)
        self.assertEqual(page['entries'],[{'index':0,'status':'incomplete','track_id':TRACK,'track':None}])
        self.assertEqual(page['tracks'],[])

    def test_truncated_or_inconsistent_artist_counts_never_emit_partial_metadata(self):
        for count in (2,0,-1,True,'1'):
            with self.subTest(count=count):
                value=payload()
                value['data']['playlistV2']['content']['items'][0]['itemV2']['data']['artists']['totalCount']=count
                page=playlist_page(value,PLAYLIST,0)
                self.assertEqual(page['tracks'],[])
                self.assertEqual((page['offset'],page['total']),(1,1))
                self.assertTrue(page['complete'])

    def test_partial_duplicate_cannot_replace_complete_artist_list(self):
        value=payload((TRACK,TRACK))
        items=value['data']['playlistV2']['content']['items']
        complete=items[0]['itemV2']['data']['artists']
        complete['items'].append({'profile':{'name':'Second Artist'}});complete['totalCount']=2
        items[1]['itemV2']['data']['artists']['totalCount']=2
        page=playlist_page(value,PLAYLIST,0)
        self.assertEqual(page['tracks'][0]['artists'],['Artist','Second Artist'])
        self.assertEqual(len(page['tracks']),1)

    def test_complete_and_unspecified_artist_counts_keep_valid_tracks(self):
        for count in (1,None):
            value=payload()
            value['data']['playlistV2']['content']['items'][0]['itemV2']['data']['artists']['totalCount']=count
            self.assertEqual(playlist_page(value,PLAYLIST,0)['tracks'][0]['artists'],['Artist'])


class PlaylistWorkerTests(unittest.TestCase):
    def setUp(self):
        self.window=Mock();self.window.get_current_url.return_value='https://open.spotify.com/'
        self.browser=SpotifyBrowser(self.window,queue.Queue(),threading.Event())
        self.browser.authenticated=True;self.browser.authorization='Bearer private token'
        self.command={'action':'catalog_playlist','request_id':'playlist-test','playlist_id':PLAYLIST,'offset':0}
        self.navigation=patch.object(self.browser,'navigate_search');self.navigate=self.navigation.start();self.addCleanup(self.navigation.stop)
        self.emitter=patch('spotify_worker.emit');self.emit=self.emitter.start();self.addCleanup(self.emitter.stop)

    def start(self,**changes):self.browser.command({**self.command,**changes})

    def observed(self,value=None,offset=0):
        self.browser.receive(('playlist_observed',('playlist-test',template(offset),{}),value or payload(total=2)))

    def test_navigation_uses_existing_hidden_window_and_never_changes_playback(self):
        self.start()
        self.navigate.assert_called_once_with('https://open.spotify.com/playlist/'+PLAYLIST)
        self.window.show.assert_not_called();self.window.evaluate_js.assert_not_called()
        self.assertIsNone(self.browser.pending)

    def test_pages_emit_raw_offset_and_finish_only_at_confirmed_total(self):
        self.start();self.observed()
        self.assertEqual(self.emit.call_args.args,('catalog_playlist_progress',))
        self.assertEqual(self.emit.call_args.kwargs['offset'],1)
        self.assertFalse(self.emit.call_args.kwargs['complete'])
        self.browser.receive(('playlist_page','playlist-test',1,payload(('z'*22,),offset=1,total=2)))
        self.assertEqual(self.emit.call_args.args,('catalog_playlist_result',))
        self.assertTrue(self.emit.call_args.kwargs['complete'])
        self.assertEqual(self.emit.call_args.kwargs['offset'],2);self.assertIsNone(self.browser.playlist)

    def test_resume_observes_template_then_fetches_requested_offset_without_reapplying_zero(self):
        self.start(offset=4);self.observed(payload(total=5))
        self.assertEqual(self.browser.playlist['offset'],4)
        self.assertFalse(any(call.args==('catalog_playlist_progress',) for call in self.emit.call_args_list))
        self.browser.receive(('playlist_page','playlist-test',4,payload(('z'*22,),offset=4,total=5)))
        self.assertTrue(self.emit.call_args.kwargs['complete']);self.assertEqual(self.emit.call_args.kwargs['offset'],5)

    def test_cancel_preserves_last_offset_and_ignores_late_pages(self):
        self.start();self.observed()
        self.browser.command({'action':'catalog_cancel','request_id':'playlist-test'})
        self.assertEqual(self.emit.call_args.kwargs['code'],'cancelled')
        self.assertEqual(self.emit.call_args.kwargs['offset'],1)
        count=self.emit.call_count
        self.browser.receive(('playlist_page','playlist-test',1,payload(offset=1,total=2)))
        self.assertEqual(self.emit.call_count,count)

    def test_lyric_work_waits_before_start_and_new_lookup_interrupts(self):
        self.browser.fetching=True;self.start();self.navigate.assert_not_called()
        self.browser.fetching=False;self.browser.tick_playlist();self.navigate.assert_called_once()
        self.browser.command({'action':'lookup'})
        self.assertEqual(self.emit.call_args.kwargs['code'],'interrupted');self.assertIsNone(self.browser.playlist)

    def test_search_and_second_playlist_receive_busy(self):
        self.start();self.browser.command({'action':'catalog_search','request_id':'search','title':'Song'})
        self.assertEqual(self.emit.call_args.kwargs['code'],'busy')
        self.start(request_id='second');self.assertEqual(self.emit.call_args.kwargs['code'],'busy')
        self.assertEqual(self.browser.playlist['request_id'],'playlist-test')

    def test_rate_limit_stops_and_blocks_restart_until_retry_after(self):
        self.start();self.observed()
        self.browser.receive(('playlist_error','playlist-test',429,90))
        self.assertEqual(self.emit.call_args.kwargs['retry_after'],90)
        self.assertFalse(self.emit.call_args.kwargs['complete']);self.assertEqual(self.emit.call_args.kwargs['offset'],1)
        self.start();self.assertEqual(self.emit.call_args.kwargs['code'],'rate_limited')
        self.assertIsNone(self.browser.playlist)

    def test_changed_total_and_repeated_pages_are_explicitly_incomplete(self):
        for second,code in ((payload(('z'*22,),offset=1,total=3),'changed'),(payload(offset=1,total=2),'no_progress')):
            self.start();self.observed()
            self.browser.receive(('playlist_page','playlist-test',1,second))
            self.assertEqual(self.emit.call_args.kwargs['code'],code)
            self.assertFalse(self.emit.call_args.kwargs['complete'])

    def test_pages_are_serial_and_spaced_by_at_least_one_second(self):
        self.start();self.observed()
        request=self.browser.playlist
        with patch('spotify_worker.threading.Thread') as thread:
            self.browser.tick_playlist(request['next_at']-.1);thread.assert_not_called()
            self.browser.tick_playlist(request['next_at']);self.assertEqual(thread.call_count,1)
            self.browser.tick_playlist(request['next_at']+1);self.assertEqual(thread.call_count,1)

    def test_native_page_uses_credentials_from_successful_browser_request_without_emitting_them(self):
        self.start()
        credentials={'Authorization':'Bearer freshly observed token','client-token':'fresh client'}
        self.browser.receive(('playlist_observed',('playlist-test',template(),credentials),payload(total=2)))
        class ImmediateThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        with patch('spotify_worker.threading.Thread',ImmediateThread),patch('spotify_worker.fetch_playlist_page') as fetch:
            fetch.return_value=payload(('z'*22,),offset=1,total=2)
            self.browser.tick_playlist(self.browser.playlist['next_at'])
            self.assertEqual(fetch.call_args.args[3:5],('Bearer freshly observed token','fresh client'))
            self.browser.receive(self.browser.network.get_nowait())
        serialized=str(self.emit.call_args_list)
        for value in ('Bearer freshly observed token','fresh client','private token'):self.assertNotIn(value,serialized)

    def test_retry_after_is_bounded_to_real_delay_and_does_not_expose_response_text(self):
        self.assertEqual(retry_after_seconds('90'),90)
        self.assertEqual(retry_after_seconds('1.2'),2)
        self.assertEqual(retry_after_seconds('invalid response text'),60)
        self.assertEqual(retry_after_seconds(float('nan')),60)

    def test_native_replay_uses_observed_hash_no_redirects_and_only_allowlisted_headers(self):
        for method in ('GET','POST'):
            response=Mock();response.read.return_value=json.dumps(payload()).encode()
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            with patch('spotify_worker.build_opener') as opener:
                opener.return_value.open.return_value=response
                fetch_playlist_page(template(method=method),PLAYLIST,6,'Bearer private token','private client',
                                    {'Cookie':'private cookie','Spotify-App-Version':'version'})
                call=opener.return_value.open.call_args
                request=call.args[0]
                self.assertEqual(urlparse(request.full_url).netloc,'api-partner.spotify.com')
                self.assertNotIn('Cookie',request.headers);self.assertEqual(request.get_method(),method)
                body=json.loads(request.data) if request.data else {key:json.loads(value[0]) if key!='operationName' else value[0]
                    for key,value in parse_qs(urlparse(request.full_url).query).items()}
                self.assertEqual(body['variables']['offset'],6)
                self.assertEqual(body['extensions']['persistedQuery']['sha256Hash'],'a'*64)
                self.assertEqual(template()['variables']['offset'],0)
                self.assertEqual(type(opener.call_args.args[0]).__name__,'NoRedirect')
        self.assertNotIn('private token',str(self.emit.call_args_list))


if __name__=='__main__':unittest.main()
