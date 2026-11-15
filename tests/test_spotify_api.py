import copy
import json
import queue
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock,patch

from spotify_api import lyric_lines,search_query,search_request_query,search_tracks,select_track
from spotify_worker import SpotifyBrowser,auth_header_allowed,response_kind


class SpotifyApiTests(unittest.TestCase):
    def setUp(self):
        self.request={'key':'track','title':'Crossing! (如月千早 Ver.)','artist':'如月千早','album':'Album','duration':240}
        self.track={'id':'a'*22,'title':self.request['title'],'artists':['如月千早'],'album':'Album','duration':240}

    def test_recording_title_suffix_is_not_removed_for_synchronization(self):
        wrong={**self.track,'title':'Crossing!'}
        self.assertIsNone(select_track(self.request,[wrong]))
        self.assertEqual(select_track(self.request,[wrong,self.track]),self.track)

    def test_rejects_cover_and_wrong_length_even_with_matching_title(self):
        self.assertIsNone(select_track(self.request,[{**self.track,'artists':['Other']}]))
        self.assertIsNone(select_track(self.request,[{**self.track,'duration':243}]))
        self.assertIsNone(select_track({**self.request,'duration':0},[self.track]))

    def test_album_resolves_duplicate_but_equal_album_remains_ambiguous(self):
        other={**self.track,'id':'b'*22,'album':'Other album'}
        self.assertEqual(select_track(self.request,[other,self.track]),self.track)
        self.assertIsNone(select_track({**self.request,'album':''},[other,self.track]))
        self.assertIsNone(select_track(self.request,[{**other,'album':'Album'},self.track]))
        self.assertEqual(select_track(self.request,[self.track,self.track]),self.track)

    def test_unicode_and_spacing_normalization_preserves_version(self):
        self.assertEqual(select_track({**self.request,'title':'Ｃrossing!  (如月千早 Ver.)'},[self.track]),self.track)

    def test_known_album_never_falls_back_to_other_recording_with_same_title_and_length(self):
        other={**self.track,'id':'b'*22,'album':'Live Album'}
        self.assertIsNone(select_track(self.request,[other]))
        self.assertIsNone(select_track(self.request,[{**other,'album':''}]))
        self.assertEqual(select_track({**self.request,'album':''},[other]),other)
        self.assertIsNone(select_track({**self.request,'album':''},[self.track,other]))

    def test_album_comparison_keeps_version_words_and_uses_existing_unicode_normalization(self):
        self.assertEqual(select_track({**self.request,'album':'ＡＬＢＵＭ  '},[self.track]),self.track)
        for album in ('Album (Live)', 'Album -Remastered-', 'Album!'):
            with self.subTest(album=album):
                self.assertIsNone(select_track({**self.request,'album':album},[self.track]))

    def test_web_player_graphql_track_metadata(self):
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}}]},
              'albumOfTrack':{'name':'Album'},'duration':{'totalMilliseconds':240000}}
        payload={'data':{'searchV2':{'tracksV2':{'items':[{'item':{'data':data}}]}}}}
        self.assertEqual(search_tracks(payload),[self.track])
        bad=copy.deepcopy(payload);bad['data']['searchV2']['tracksV2']['items'][0]['item']['data']['uri']='spotify:episode:'+'a'*22
        self.assertEqual(search_tracks(bad),[])
        self.assertEqual(search_tracks({'errors':[]}),[])

    def test_request_query_is_bound_to_spotify_operation_and_exact_search(self):
        query=search_query(self.request)
        body={'operationName':'searchDesktop','variables':{'searchTerm':query}}
        endpoint='https://api-partner.spotify.com/pathfinder/v2/query'
        self.assertEqual(search_request_query(endpoint,json.dumps(body)),query)
        self.assertEqual(search_request_query(endpoint+'?'+urlencode({**body,'variables':json.dumps(body['variables'])})),query)
        self.assertIsNone(search_request_query(endpoint,json.dumps({**body,'operationName':'profileAttributes'})))
        self.assertIsNone(search_request_query(endpoint.replace('spotify.com','spotify.com.evil.test'),json.dumps(body)))
        self.assertIsNone(search_request_query(endpoint,'not json'))

    def test_known_partial_artist_lists_cannot_enter_search_candidates(self):
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}}]},
              'albumOfTrack':{'name':'Album'},'duration':{'totalMilliseconds':240000}}
        payload={'data':{'searchV2':{'tracksV2':{'items':[{'item':{'data':data}}]}}}}
        for count in (2,0,-1,True,'1'):
            with self.subTest(count=count):
                data['artists']['totalCount']=count
                self.assertEqual(search_tracks(payload),[])
        for count in (1,None):
            data['artists']['totalCount']=count
            self.assertEqual(search_tracks(payload),[self.track])
        del data['artists']['totalCount']
        self.assertEqual(search_tracks(payload),[self.track])

    def test_partial_top_result_cannot_overwrite_complete_same_id_search_metadata(self):
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}},{'profile':{'name':'天海春香'}}],'totalCount':2},
              'albumOfTrack':{'name':'Album'},'duration':{'totalMilliseconds':240000}}
        partial=copy.deepcopy(data);partial['artists']['items'].pop()
        payload={'data':{'searchV2':{'tracksV2':{'items':[{'item':{'data':data}}]},
                    'topResultsV2':{'itemsV2':[{'item':{'data':partial}}]}}}}
        tracks=search_tracks(payload)
        self.assertEqual(len(tracks),1)
        merged={track['id']:track for track in tracks}
        self.assertEqual(merged[self.track['id']]['artists'],['如月千早','天海春香'])

    def test_signed_in_top_results_can_contain_mixed_entity_types(self):
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}}]},
              'albumOfTrack':{'name':'Album'},'duration':{'totalMilliseconds':240000}}
        payload={'data':{'searchV2':{'tracksV2':{'totalCount':1},'topResultsV2':{'itemsV2':[
            {'item':{'data':{'__typename':'Artist','uri':'spotify:artist:'+'b'*22,'name':'如月千早'}}},
            {'item':{'data':data}}]}}}}
        self.assertEqual(search_tracks(payload),[self.track])
        self.assertEqual(select_track(self.request,search_tracks(payload)),self.track)
        wrong=copy.deepcopy(payload)
        wrong['data']['searchV2']['topResultsV2']['itemsV2'][1]['item']['data']['name']='Crossing!'
        self.assertIsNone(select_track(self.request,search_tracks(wrong)))

    def test_top_results_do_not_hide_track_results_or_create_duplicate_ambiguity(self):
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}}]},
              'albumOfTrack':{'name':'Album'},'duration':{'totalMilliseconds':240000}}
        payload={'data':{'searchV2':{'tracksV2':{'items':[{'item':{'data':data}}]},
                    'topResultsV2':{'itemsV2':[{'item':{'data':data}}]}}}}
        self.assertEqual(select_track(self.request,search_tracks(payload)),self.track)

    def test_line_times_are_real_seconds_without_inventing_unsynced_times(self):
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':'first','startTimeMs':'1230'},
                                                               {'words':'♪','startTimeMs':'4000'},
                                                               {'words':'last','startTimeMs':'5500'}]}}
        self.assertEqual(lyric_lines(payload,8),[{'text':'first','time':1.23},{'text':'last','time':5.5}])
        payload['lyrics']['syncType']='UNSYNCED'
        self.assertEqual(lyric_lines(payload),[{'text':'first','time':None},{'text':'last','time':None}])

    def test_signed_in_search_operation_uses_query_instead_of_search_term(self):
        query=search_query(self.request)
        endpoint='https://api-partner.spotify.com/pathfinder/v2/query'
        body={'operationName':'searchTopResultsList','variables':{'query':query,'limit':20}}
        self.assertEqual(search_request_query(endpoint,json.dumps(body)),query)
        self.assertEqual(search_request_query(endpoint+'?'+urlencode({**body,'variables':json.dumps(body['variables'])})),query)
        self.assertIsNone(search_request_query(endpoint,json.dumps({**body,'operationName':'recentSearches'})))

    def test_corrupt_timestamp_rejects_whole_timeline(self):
        for stamp in ('NaN','Infinity','-100','20000',True,None):
            with self.subTest(stamp=stamp):
                payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':'first','startTimeMs':stamp}]}}
                with self.assertRaises(ValueError):lyric_lines(payload,10)
        with self.assertRaises(ValueError):
            lyric_lines({'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':'a','startTimeMs':5000},{'words':'b','startTimeMs':3000}]}})

    def test_missing_line_synced_timestamp_preserves_other_onsets_and_original(self):
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':'first','startTimeMs':'1000'},
                    {'words':'untimed'}, {'words':'also untimed','startTimeMs':''},
                    {'words':'last','startTimeMs':'3500'}]}}
        self.assertEqual(lyric_lines(payload,8),[{'text':'first','time':1}, {'text':'untimed','time':None},
                    {'text':'also untimed','time':None}, {'text':'last','time':3.5}])

    def test_same_onset_lines_merge_to_a_strictly_increasing_timeline(self):
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':'a','startTimeMs':1000},
                  {'words':'b','startTimeMs':1000},{'words':'c','startTimeMs':3000}]}}
        self.assertEqual(lyric_lines(payload,10),[{'text':'a b','time':1},{'text':'c','time':3}])
        payload['lyrics']['lines'][-1]['startTimeMs']=12001
        with self.assertRaises(ValueError):lyric_lines(payload,10)

    def test_secret_collection_is_limited_to_https_spotify_hosts(self):
        self.assertTrue(auth_header_allowed('https://api-partner.spotify.com/pathfinder/v2/query'))
        for url in ('http://api-partner.spotify.com/query','https://api-partner.spotify.com.evil.test/query',
                    'https://accounts.google.com/','https://namu.wiki/w/Test'):
            self.assertFalse(auth_header_allowed(url))
            self.assertEqual(response_kind(url),'')
        self.assertEqual(response_kind('https://open.spotify.com/api/token?query=private'),'token')
        self.assertEqual(response_kind('https://accounts.spotify.com/login'),'')

    def test_cancel_invalidates_fetch_and_drops_late_response(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending=self.request;browser.fetching=True;browser.active_query='old';browser.search_deadline=123
        browser.command({'action':'lookup','key':'','title':''})
        self.assertIsNone(browser.pending)
        self.assertEqual((browser.generation,browser.active_query,browser.fetching,browser.search_deadline),(1,'',False,0))
        with patch('spotify_worker.emit') as output:
            browser.receive(('lyrics',0,'old',self.track,{}))
        output.assert_not_called()

    def test_auth_refresh_is_bounded_and_never_pops_up(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending=self.request;browser.authenticated=True;browser.authorization='Bearer synthetic'
        with patch.object(browser,'reload') as reload,patch('spotify_worker.emit') as output:
            browser.receive(('fetch_error',0,401))
            reload.assert_called_once()
            browser.receive(('fetch_error',0,401))
            self.assertEqual(reload.call_count,1)
            self.assertEqual(output.call_args.kwargs['state'],'login_required')
        browser.window.show.assert_not_called()
        self.assertEqual(browser.authorization,'')

    def test_connected_show_and_refresh_keep_browser_hidden(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending=self.request;browser.authenticated=True
        with patch.object(browser,'begin_search') as begin:
            browser.command({'action':'show'})
            browser.command({'action':'refresh'})
            self.assertEqual(begin.call_count,2)
        browser.window.show.assert_not_called()

    def test_track_changes_after_login_failure_finish_each_lookup_without_a_window(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        with patch('spotify_worker.emit') as output:
            browser.receive(('token',None,{'isAnonymous':True}))
            for key in ('first','second'):
                output.reset_mock()
                browser.command({**self.request,'action':'lookup','key':key})
                self.assertEqual(output.call_count,1)
                self.assertEqual(output.call_args.kwargs['state'],'login_required')
                self.assertEqual(output.call_args.kwargs['key'],key)
                self.assertFalse(output.call_args.kwargs['authenticated'])
        browser.window.show.assert_not_called()

    def test_first_lookup_still_waits_for_initial_background_login_check(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        with patch('spotify_worker.emit') as output,patch.object(browser,'begin_search') as begin:
            browser.command({**self.request,'action':'lookup'})
            output.assert_not_called();begin.assert_not_called()
            browser.receive(('token',None,{'isAnonymous':False,'accessToken':'synthetic'}))
            begin.assert_called_once()

    def test_status_reports_new_track_and_authentication_even_with_same_message(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending=self.request
        with patch('spotify_worker.emit') as output:
            browser.status('searching','Searching')
            browser.status('searching','Searching')
            self.assertEqual(output.call_count,1)
            self.assertEqual(output.call_args.kwargs['key'],'track')
            browser.pending={**self.request,'key':'next'}
            browser.status('searching','Searching')
            self.assertEqual(output.call_count,2)
            self.assertEqual(output.call_args.kwargs['key'],'next')
            browser.authenticated=True
            browser.status('searching','Searching')
            self.assertEqual(output.call_count,3)
            self.assertTrue(output.call_args.kwargs['authenticated'])

    def test_anonymous_headers_cannot_replace_verified_token(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        with patch('spotify_worker.emit'):
            browser.receive(('token',None,{'isAnonymous':False,'accessToken':'synthetic-account'}))
            browser.receive(('headers','Bearer synthetic-anonymous','synthetic-client'))
        self.assertEqual(browser.authorization,'Bearer synthetic-account')
        self.assertTrue(browser.authenticated)

    def test_bad_sync_falls_back_to_valid_originals_without_inventing_times(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.authenticated=True
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[
            {'words':'青い空を見る','startTimeMs':'NaN'},
            {'words':'最後の言葉','startTimeMs':'4000'}]}}
        with patch('spotify_worker.emit') as output:
            browser.receive(('lyrics',0,'track',self.track,payload))
        calls=[call for call in output.call_args_list if call.args==('lyrics',)]
        self.assertEqual(calls[0].kwargs['lines'],[
            {'text':'青い空を見る','time':None},{'text':'最後の言葉','time':None}])
        self.assertEqual(output.call_args.kwargs['state'],'unsynced')
        self.assertEqual(payload['lyrics']['syncType'],'LINE_SYNCED')

    def test_bad_originals_still_reject_whole_provider_response(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[{'words':{'invalid':True},'startTimeMs':'0'}]}}
        with patch('spotify_worker.emit') as output:
            browser.receive(('lyrics',0,'track',self.track,payload))
        self.assertTrue(all(call.args!=('lyrics',) for call in output.call_args_list))
        self.assertEqual(output.call_args.kwargs['state'],'error')

    def test_overflowing_timestamp_keeps_originals_without_crashing_worker(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        payload={'lyrics':{'syncType':'LINE_SYNCED','lines':[
            {'words':'Synthetic original','startTimeMs':10**1000}]}}
        with patch('spotify_worker.emit') as output:
            browser.receive(('lyrics',0,'track',self.track,payload))
        delivered=[call for call in output.call_args_list if call.args==('lyrics',)]
        self.assertEqual(delivered[0].kwargs['lines'],[{'text':'Synthetic original','time':None}])
        self.assertEqual(output.call_args.kwargs['state'],'unsynced')

    def test_logout_invalidates_inflight_lyrics_and_search_before_reauthentication(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending=self.request;browser.authenticated=True;browser.fetching=True
        browser.authorization='Bearer synthetic';browser.active_query='query';browser.search_deadline=100
        generation=browser.generation
        payload={'lyrics':{'syncType':'UNSYNCED','lines':[{'words':'Synthetic original'}]}}
        with patch('spotify_worker.emit') as output:
            browser.receive(('token',None,{'isAnonymous':True}))
            self.assertEqual((browser.fetching,browser.active_query,browser.search_deadline),(False,'',0))
            self.assertEqual(output.call_args.kwargs['state'],'login_required')
            output.reset_mock()
            browser.receive(('lyrics',generation,'track',self.track,payload))
            browser.receive(('fetch_error',generation,401))
            output.assert_not_called()
        self.assertIs(browser.pending,self.request)
        self.assertFalse(browser.authenticated)
        self.assertEqual(browser.authorization,'')

    def metadata_search(self, metadata_only=True, album='Album'):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.pending={**self.request,'metadata_only':metadata_only}
        browser.authenticated=True;browser.active_query='query';browser.search_deadline=100
        data={'__typename':'Track','uri':'spotify:track:'+'a'*22,'name':self.request['title'],
              'artists':{'items':[{'profile':{'name':'如月千早'}},{'profile':{'name':'四条貴音'}}]},
              'albumOfTrack':{'name':album},'duration':{'totalMilliseconds':240000}}
        payload={'data':{'searchV2':{'tracksV2':{'items':[{'item':{'data':data}}]}}}}
        return browser,payload

    def test_metadata_only_search_emits_full_credits_without_lyric_api_or_visible_window(self):
        browser,payload=self.metadata_search()
        with patch('spotify_worker.emit') as output,patch('spotify_worker.fetch_lyrics') as fetch:
            browser.receive(('search','query',payload))
        fetch.assert_not_called();browser.window.show.assert_not_called()
        metadata=[call.kwargs for call in output.call_args_list if call.args==('metadata',)]
        self.assertEqual(len(metadata),1)
        self.assertEqual(metadata[0]['track']['artists'],['如月千早','四条貴音'])
        self.assertEqual(metadata[0]['request_duration'],240)
        self.assertFalse(browser.fetching)
        self.assertEqual(browser.active_query,'')
        self.assertTrue(all(call.args!=('status',) for call in output.call_args_list))

    def test_regular_search_also_emits_credits_before_lyric_fetch(self):
        browser,payload=self.metadata_search(metadata_only=False)
        with patch('spotify_worker.emit') as output,patch('spotify_worker.threading.Thread') as thread:
            browser.receive(('search','query',payload))
        thread.return_value.start.assert_called_once()
        self.assertEqual(output.call_args.args,('metadata',))
        self.assertEqual(output.call_args.kwargs['track']['artists'],['如月千早','四条貴音'])

    def test_wrong_album_or_stale_search_never_supplies_metadata(self):
        browser,payload=self.metadata_search(album='Different album')
        with patch('spotify_worker.emit') as output,patch('spotify_worker.fetch_lyrics') as fetch:
            browser.receive(('search','stale query',payload))
            output.assert_not_called()
            browser.receive(('search','query',payload))
        self.assertTrue(all(call.args!=('metadata',) for call in output.call_args_list))
        fetch.assert_not_called()

    def test_regular_lyrics_search_cannot_fetch_timing_from_a_different_album(self):
        browser,payload=self.metadata_search(metadata_only=False,album='Different album')
        with patch('spotify_worker.emit') as output,patch('spotify_worker.threading.Thread') as thread:
            browser.receive(('search','query',payload))
        thread.assert_not_called()
        self.assertFalse(browser.fetching)
        self.assertEqual(output.call_args.kwargs['state'],'not_found')
        self.assertTrue(all(call.args not in (('metadata',),('lyrics',)) for call in output.call_args_list))

    def test_malformed_artist_names_are_not_accepted_from_search(self):
        browser,payload=self.metadata_search()
        payload['data']['searchV2']['tracksV2']['items'][0]['item']['data']['artists']['items'][1]['profile']['name']={}
        self.assertEqual(search_tracks(payload),[])

    def test_overflowing_search_duration_is_rejected_without_stopping_worker(self):
        browser,payload=self.metadata_search()
        payload['data']['searchV2']['tracksV2']['items'][0]['item']['data']['duration']['totalMilliseconds']=10**1000
        self.assertEqual(search_tracks(payload),[])


if __name__=='__main__':unittest.main()
