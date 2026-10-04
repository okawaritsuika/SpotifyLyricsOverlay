"""Opt-in catalog traces contain operational facts, never account or song text."""
import json
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlencode

from background_lyrics import BackgroundLyrics
from spotify_catalog import catalog_diagnostic_record,catalog_request_diagnostic,catalog_query_diagnostic,catalog_page_kind
from spotify_worker import SpotifyBrowser


class CatalogDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.window=Mock();self.window.get_current_url.return_value='https://open.spotify.com/'
        self.browser=SpotifyBrowser(self.window,queue.Queue(),threading.Event(),diagnostics=True)
        navigation=patch.object(self.browser,'navigate_search',side_effect=self.window.load_url)
        navigation.start();self.addCleanup(navigation.stop)
        self.browser.authenticated=True
        self.command={'action':'catalog_search','request_id':'private-request','title':'Private song title'}

    def test_disabled_diagnostics_do_not_emit_or_change_search(self):
        self.browser.diagnostics=False
        with patch('spotify_worker.emit') as emit:
            self.browser.command(self.command)
            self.browser.finish_catalog(code='timeout',error='Existing timeout')
        self.assertEqual(emit.call_count,1)
        self.assertEqual(emit.call_args.args,('catalog_result',))
        self.assertEqual(emit.call_args.kwargs['code'],'timeout')

    def test_navigation_and_timeout_describe_stage_without_song_or_request_identity(self):
        with patch('spotify_worker.emit') as emit:
            self.browser.command(self.command)
            self.browser.tick_catalog(self.browser.catalog_deadline+1)
        values=[call.kwargs['value'] for call in emit.call_args_list if call.args==('catalog_diagnostic',)]
        self.assertEqual([value['phase'] for value in values],['queued','navigation_requested','navigation','finished'])
        self.assertEqual(values[0]['attempt'],1)
        self.assertTrue(values[0]['catalog_pending'])
        self.assertTrue(values[-1]['catalog_active'])
        self.assertEqual(values[2]['page'],'home')
        self.assertEqual(values[2]['navigation'],'load')
        self.assertEqual(values[-1]['result'],'timeout')
        serialized=json.dumps(values)
        for private in ('Private song title','private-request','https://','open.spotify.com'):
            self.assertNotIn(private,serialized)

    def test_pending_lyric_fetch_is_distinguishable_from_active_catalog_timeout(self):
        self.browser.fetching=True
        with patch('spotify_worker.emit') as emit:
            self.browser.command(self.command)
            deadline=self.browser.catalog_pending['deadline']
            self.browser.tick_catalog(deadline+1)
        values=[call.kwargs['value'] for call in emit.call_args_list if call.args==('catalog_diagnostic',)]
        self.assertEqual([value['phase'] for value in values],['queued','finished'])
        self.assertTrue(values[-1]['lyric_fetch_pending'])
        self.assertTrue(values[-1]['catalog_pending'])
        self.assertFalse(values[-1]['catalog_active'])
        self.window.load_url.assert_not_called()

    def test_delayed_observation_cannot_attach_to_replacement_catalog_request(self):
        with patch('spotify_worker.emit'):
            self.browser.command(self.command)
            self.browser.finish_catalog(code='cancelled')
            self.browser.command({**self.command,'request_id':'replacement'})
        with patch('spotify_worker.emit') as emit:
            self.browser.receive(('catalog_diagnostic','private-request','parsed',{}))
            emit.assert_not_called()
            self.browser.receive(('catalog_diagnostic','replacement','parsed',{}))
        self.assertEqual(emit.call_args.kwargs['value']['attempt'],2)

    def test_only_bounded_allowlisted_values_survive(self):
        value={'phase':'response','operation':'searchFutureV3','elapsed':4.126,'http_status':429,
               'authenticated':True,'query_matches':False,'title':'private title','url':'private URL',
               'body':'private body','headers':{'Authorization':'private token'},'request_id':'private ID'}
        record=catalog_diagnostic_record(value)
        self.assertEqual(record,{'phase':'response','operation':'searchFutureV3','elapsed':4.1,
                                 'http_status':429,'authenticated':True,'query_matches':False})
        for operation in ('Bearer token','https://private', 'a'*81,42):
            self.assertNotIn('operation',catalog_diagnostic_record({**value,'operation':operation}))
        for elapsed in (float('nan'),float('inf'),10**1000,True,'1'):
            self.assertNotIn('elapsed',catalog_diagnostic_record({**value,'elapsed':elapsed}))
        for bad in (None,[],{'phase':'private unknown phase'}):self.assertEqual(catalog_diagnostic_record(bad),{})

    def test_operation_metadata_recognizes_new_shapes_without_retaining_variables(self):
        endpoint='https://api-partner.spotify.com/pathfinder/v2/query'
        body=json.dumps({'operationName':'searchFutureV3','variables':{'query':'private query'},'token':'private token'})
        record=catalog_request_diagnostic(endpoint,body,'POST',True)
        self.assertEqual(record,{'phase':'response','endpoint':'pathfinder_v2','method':'POST',
                                 'request_format':'object','operation':'searchFutureV3'})
        query=catalog_request_diagnostic(endpoint+'?'+urlencode({'operationName':'searchTracks','variables':'private query'}))
        self.assertEqual(query['operation'],'searchTracks')
        self.assertEqual(query['request_format'],'query')
        for body,shape in (('', 'empty'),('[{}]','array'),('{invalid','invalid')):
            self.assertEqual(catalog_request_diagnostic(endpoint,body,'POST',True)['request_format'],shape)
        self.assertEqual(catalog_request_diagnostic(endpoint,method='POST')['request_format'],'unread')
        self.assertEqual(catalog_request_diagnostic('https://api-partner.spotify.com/pathfinder/v9/query')['endpoint'],'pathfinder_other')
        self.assertEqual(catalog_request_diagnostic('https://api-partner.spotify.com/new-api')['endpoint'],'partner_other')
        self.assertEqual(catalog_request_diagnostic('https://example.com/?operationName=private'),{})

    def test_page_categories_never_include_path_or_query(self):
        for url,expected in (('https://open.spotify.com/','home'),
                             ('https://open.spotify.com/intl-ko/search/private-title','search'),
                             ('https://open.spotify.com/track/private-id','track'),
                             ('https://accounts.spotify.com/login?token=private','login'),
                             ('https://other.example/private','other'),(None,'other')):
            self.assertEqual(catalog_page_kind(url),expected)

    def test_query_mismatch_classification_has_lengths_and_booleans_only(self):
        for query,requested,field in (('PRIVATE','private','query_casefold_matches'),
                ('ＰＲＩＶＡＴＥ','PRIVATE','query_nfkc_matches'),
                (' private ','private','query_trim_matches'),
                ('ＰＲＩＶＡＴＥ','private','query_normalized_matches'),
                ('"private"','private','query_unquoted_matches'),
                ('private%20query','private query','query_percent_decoded_matches')):
            with self.subTest(field=field):
                facts=catalog_query_diagnostic(query,requested)
                record=catalog_diagnostic_record({'phase':'response',**facts})
                self.assertTrue(record[field])
                self.assertEqual(record['query_length'],len(query))
                self.assertEqual(record['requested_length'],len(requested))
                self.assertTrue(all(type(value) in (int,bool) for value in facts.values()))
        self.assertFalse(any(value for key,value in catalog_query_diagnostic('different','unrelated').items()
                             if key.endswith('_matches')))
        self.assertEqual(catalog_query_diagnostic(None,'private'),{})

    def test_diagnostic_query_similarity_does_not_broaden_response_acceptance(self):
        with patch('spotify_worker.emit') as emit:
            self.browser.command(self.command)
            query=self.command['title'].casefold()
            self.browser.receive(('catalog_diagnostic',self.command['request_id'],'response',
                                  catalog_query_diagnostic(query,self.command['title'])))
            self.browser.receive(('search',query,{'data':{'searchV2':{}}}))
        self.assertTrue(emit.call_args.kwargs['value']['query_casefold_matches'])
        self.assertIsNotNone(self.browser.catalog_active)
        self.assertFalse(any(call.args==('catalog_result',) for call in emit.call_args_list))

    def test_service_keeps_twenty_records_from_current_worker_only_and_resets_on_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            service=BackgroundLyrics('.',directory,queue.Queue(),job_factory=Mock())
            service.job=Mock()
            for attempt in range(1,26):
                service.receive(service.job,{'kind':'catalog_diagnostic','value':{'phase':'queued','attempt':attempt,
                                                                                'token':'private token'}})
            self.assertEqual(len(service.spotify_catalog_diagnostics),20)
            self.assertEqual(service.spotify_catalog_diagnostics[0]['attempt'],6)
            before=list(service.spotify_catalog_diagnostics)
            for sender,value in ((object(),{'phase':'finished'}),(service.job,None),(service.job,{'phase':'invalid'})):
                service.receive(sender,{'kind':'catalog_diagnostic','value':value})
            self.assertEqual(service.spotify_catalog_diagnostics,before)
            self.assertNotIn('private token',json.dumps(service.spotify_catalog_diagnostics))
            service.job=None;service._start_spotify()
            self.assertEqual(service.spotify_catalog_diagnostics,[])
            service.close()

    def hook_response(self,operation='searchTopResultsList',task_state='ready',status=200):
        class Event:
            def __iadd__(self,callback):return self
        class Stream:
            CanSeek=False
            def __init__(self,text):self.text=text
        class Reader:
            def __init__(self,stream,*args):self.text=stream.text;self.offset=0
            def ReadToEnd(self):return self.text
            def Read(self,buffer,start,count):
                value=self.text[self.offset:self.offset+count];self.offset+=len(value)
                buffer[start:start+len(value)]=value
                return len(value)
            def Dispose(self):pass
        class ImmediateThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        core=SimpleNamespace(WebResourceResponseReceived=Event())
        self.window.native=SimpleNamespace(browser=SimpleNamespace(webview=SimpleNamespace(CoreWebView2=core)),
                                           Invoke=lambda callback:callback())
        response=json.dumps({'data':{'searchV2':{'tracksV2':{'items':[]}}},'private':'private response body'})
        stream=None if task_state=='empty' else Stream(response)
        task=SimpleNamespace(IsCompleted=True,IsFaulted=task_state=='failed',IsCanceled=False,Result=stream)
        headers=Mock();headers.Contains.side_effect=lambda name:name=='Authorization'
        headers.GetHeader.return_value='Bearer private token'
        request=SimpleNamespace(Uri='https://api-partner.spotify.com/pathfinder/v2/query',Method='POST',Headers=headers,
            Content=Stream(json.dumps({'operationName':operation,'variables':{'query':'Private song title'}})))
        args=SimpleNamespace(Request=request,Response=SimpleNamespace(StatusCode=status,GetContentAsync=lambda:task))
        modules={'System':SimpleNamespace(Action=lambda fn:fn,Array=SimpleNamespace(CreateInstance=lambda kind,size:['']*size),Char=str),
                 'System.IO':SimpleNamespace(StreamReader=Reader),'System.Text':SimpleNamespace(Encoding=SimpleNamespace(UTF8='utf8'))}
        with patch.dict('sys.modules',modules),patch('spotify_worker.threading.Thread',ImmediateThread),patch('spotify_worker.emit') as emit:
            self.browser.install_hooks();self.browser.command(self.command)
            self.browser._receive(None,args)
            while not self.browser.network.empty():self.browser.receive(self.browser.network.get_nowait())
        return [call.kwargs['value'] for call in emit.call_args_list if call.args==('catalog_diagnostic',)]

    def test_hook_reports_operation_and_content_stages_without_raw_request_or_response(self):
        values=self.hook_response()
        phases=[value['phase'] for value in values]
        self.assertEqual(phases[-5:],['response','task_waiting','stream_reading','parsed','finished'])
        for value in values[-5:-1]:
            self.assertEqual(value['operation'],'searchTopResultsList')
            self.assertTrue(value['query_accepted']);self.assertTrue(value['query_matches'])
            self.assertEqual(value['http_status'],200)
        serialized=json.dumps(values)
        for secret in ('Private song title','private token','private response body','https://'):
            self.assertNotIn(secret,serialized)

    def test_hook_explains_ignored_operation_without_changing_acceptance(self):
        values=self.hook_response(operation='searchFutureV3')
        self.assertEqual(values[-1]['operation'],'searchFutureV3')
        self.assertFalse(values[-1]['query_accepted'])
        self.assertIsNotNone(self.browser.catalog_active)

    def test_hook_distinguishes_failed_task_and_missing_stream(self):
        for state,phase in (('failed','task_failed'),('empty','stream_unavailable')):
            self.browser.catalog_active=None
            values=self.hook_response(task_state=state)
            self.assertEqual(values[-1]['phase'],phase)
            self.assertIsNotNone(self.browser.catalog_active)


if __name__=='__main__':unittest.main()
