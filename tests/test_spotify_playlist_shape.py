import json
import queue
import threading
import unittest
from unittest.mock import Mock,patch

from spotify_worker import SpotifyBrowser,playlist_track_shapes


def payload(count=1):
    track={'uri':'spotify:track:'+'a'*22,'__typename':'Track','name':'private test title',
           'artists':{'items':[{'profile':{'name':'private test artist'}}],'totalCount':1},
           'trackDuration':{'totalMilliseconds':212345},'albumOfTrack':{'name':'private test album'},
           'private-key':'private token'}
    return {'data':{'playlistV2':{'content':{'items':[{'itemV2':{'data':track,'_uri':'private wrapper'}} for _ in range(count)],
                                          'totalCount':count+1,'pagingInfo':{'offset':0}}}},
            'Authorization':'Bearer private token'}


class PlaylistTrackShapeTests(unittest.TestCase):
    def test_report_has_only_fixed_schema_keys_booleans_and_bounded_public_numbers(self):
        samples=playlist_track_shapes(payload(5))
        self.assertEqual(len(samples),3)
        first=samples[0]
        self.assertEqual(first['duration_container'],'null')
        self.assertEqual(first['track_duration_container'],'object')
        self.assertEqual(first['track_duration_total_milliseconds'],212345)
        self.assertEqual(first['artist_items_count'],1);self.assertEqual(first['artist_total_count'],1)
        self.assertTrue(first['data_uri_present']);self.assertTrue(first['wrapper_uri_present'])
        self.assertTrue(first['first_artist_profile_name_present']);self.assertTrue(first['album_name_present'])
        for private in ('private','Bearer','Authorization','a'*22,'spotify:track:'):
            self.assertNotIn(private,json.dumps(samples))

    def test_malformed_values_cannot_copy_raw_objects_into_diagnostics(self):
        value=payload();track=value['data']['playlistV2']['content']['items'][0]['itemV2']['data']
        track['artists']['totalCount']=True
        track['trackDuration']['totalMilliseconds']=float('inf')
        first=playlist_track_shapes(value)[0]
        self.assertIsNone(first['artist_total_count']);self.assertIsNone(first['track_duration_total_milliseconds'])
        self.assertEqual(playlist_track_shapes({'data':None}),[])

    def test_schema_diagnostic_is_emitted_once_for_first_page_even_after_other_trace_limit(self):
        browser=SpotifyBrowser(Mock(),queue.Queue(),threading.Event())
        browser.playlist={'request_id':'shape-test','playlist_id':'p'*22,'offset':0,'total':None,
                          'pages':set(),'diagnostics':40}
        value=payload()
        with patch('spotify_worker.emit') as emit:
            browser.accept_playlist_page(value,0)
            value['data']['playlistV2']['content']['pagingInfo']['offset']=1
            value['data']['playlistV2']['content']['items'][0]['itemV2']['data']['uri']='spotify:track:'+'b'*22
            browser.accept_playlist_page(value,1)
        traces=[call.kwargs['value'] for call in emit.call_args_list if call.args==('catalog_playlist_diagnostic',)]
        self.assertEqual(len(traces),1);self.assertEqual(traces[0]['phase'],'public_track_shape')


if __name__=='__main__':unittest.main()
