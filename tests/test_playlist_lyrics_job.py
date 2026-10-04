import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from lyrics import Line
from playlist_lyrics_job import PlaylistLyricsJob,coverage
from playlist_lyrics_store import PlaylistLyricsStore
from storage import atomic_json


PLAYLIST='p'*22
TRACK={'id':'a'*22,'title':'Fixture song','artists':['Fixture artist'],'album':'Fixture album','duration':200.0}
SECOND={**TRACK,'id':'b'*22,'title':'Second fixture'}


def entry(index,track=TRACK):
    return {'index':index,'status':'track','track_id':track['id'],'track':dict(track)}


def saved(track=TRACK,translated=True,timed=True):
    lines=(Line('fixture first','','첫 구절' if translated else ''),Line('fixture second','','둘째 구절' if translated else ''))
    return {'track':dict(track),'lines':lines,'direct':{'source':'Spotify','provider_id':track['id'],
            'lines':lines,'times':[1.,2.] if timed else [None,None]}}


class MemoryStore:
    def __init__(self):self.records={};self.writes=[]
    def load(self,identifier):return copy.deepcopy(self.records.get(identifier))
    def save(self,track,lines,direct,sources=()):
        self.writes.append(track['id'])
        self.records[track['id']]={'track':dict(track),'lines':tuple(lines),'direct':direct,'sources':list(sources)}
        return True


class PlaylistLyricsJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        (self.directory/'playlist_lyrics_job.json').write_text(json.dumps({'enabled':True,'playlist_id':PLAYLIST,'phase':'playlist'}))
        self.worker=Mock()
        self.service=SimpleNamespace(job=self.worker,spotify_authenticated=True,spotify_pending=False,
                                     metadata_pending=False,connect=Mock())
        self.app=SimpleNamespace(store=SimpleNamespace(directory=self.directory),background_lyrics=self.service,
                                 lyric_cache=SimpleNamespace(load=Mock(return_value=None)),
                                 catalog_sync=SimpleNamespace(active=None,store_tracks=Mock()))
        self.job=PlaylistLyricsJob(self.app);self.job.store=MemoryStore()
        self.now=100.
        clock=patch('playlist_lyrics_job.time.monotonic',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)
        self.threads=[]
        class DeferredThread:
            def __init__(inner,target,**kwargs):self.threads.append(target)
            def start(inner):pass
        thread=patch('playlist_lyrics_job.threading.Thread',DeferredThread);thread.start();self.addCleanup(thread.stop)

    def tick(self,elapsed=1.):
        self.now+=elapsed;self.job.tick()

    def manifest(self,tracks=(TRACK,),phase='spotify',complete=True):
        self.job.manifest={'schema_version':1,'playlist_id':PLAYLIST,'entries':[entry(i,t) for i,t in enumerate(tracks)],
                           'total':len(tracks),'complete':complete}
        self.job.value['phase']=phase;self.job.tracks=None

    def response(self,**values):
        request=self.job.active
        event={'kind':'batch_lyrics_result','request_id':request['request_id'],'provider_id':request['track']['id'],
               'source':'Spotify','lines':[{'text':'fixture first','time':1.},{'text':'fixture second','time':2.}],
               'code':'','error':'','synced':True,**values}
        return self.job.receive(self.worker,event)

    def reviewed_store(self, tracks=(TRACK,)):
        self.manifest(tracks,phase='review')
        self.job.enabled=False;self.job.value['enabled']=False
        self.job.store=PlaylistLyricsStore(self.directory)
        for track in tracks:
            value=saved(track,translated=False)
            self.assertTrue(self.job.store.save(track,value['lines'],value['direct']))
        atomic_json(self.job.manifest_path,self.job.manifest)
        return self.directory/'playlist_lyrics'/'reviewed'

    def reviewed_payload(self, track=TRACK):
        return {'schema_version':1,'track':dict(track),
                'lines':[{'original':row.original,'reading':'reviewed reading','korean':'reviewed translation'}
                         for row in saved(track)['lines']]}

    def test_manifest_preserves_order_duplicate_positions_and_unavailable_rows(self):
        self.tick();request_id=self.job.active['request_id']
        event={'kind':'catalog_playlist_progress','request_id':request_id,'playlist_id':PLAYLIST,
               'entries':[entry(0),entry(1)],'start':0,'offset':2,'total':3,'complete':False}
        self.assertTrue(self.job.receive(self.worker,event))
        self.assertEqual(len(self.job.manifest['entries']),2);self.assertEqual(self.job.value['phase'],'playlist')
        unavailable={'index':2,'status':'unavailable','track_id':'','track':None}
        final={**event,'kind':'catalog_playlist_result','entries':[unavailable],'start':2,'offset':3,'complete':True}
        self.assertTrue(self.job.receive(self.worker,final))
        manifest=json.loads(self.job.manifest_path.read_text(encoding='utf-8'))
        self.assertEqual([row['index'] for row in manifest['entries']],[0,1,2])
        self.assertEqual([row['track_id'] for row in manifest['entries']],[TRACK['id'],TRACK['id'],''])
        self.assertTrue(manifest['complete']);self.assertEqual(len(self.job._tracks()),1)
        self.assertEqual(self.job.value['phase'],'spotify')

    def test_invalid_page_positions_do_not_commit_or_complete_manifest(self):
        self.tick()
        self.job.receive(self.worker,{'kind':'catalog_playlist_result','request_id':self.job.active['request_id'],
            'entries':[entry(1)],'start':0,'offset':1,'total':1,'complete':True})
        self.assertFalse(self.job.manifest.get('complete'))
        self.assertEqual(self.job.manifest.get('entries',[]),[])
        self.assertEqual(self.job.value['errors']['playlist']['code'],'playlist_page_mismatch')

    def test_partial_page_cannot_claim_playlist_complete(self):
        self.tick()
        self.job.receive(self.worker,{'kind':'catalog_playlist_result','request_id':self.job.active['request_id'],
            'entries':[entry(0)],'start':0,'offset':1,'total':2,'complete':True})
        self.assertFalse(self.job.manifest['complete']);self.assertEqual(self.job.value['phase'],'playlist')
        self.tick(2)
        self.assertEqual(self.worker.send.call_args.args[0]['offset'],1)

    def test_old_worker_and_wrong_request_cannot_change_manifest(self):
        self.tick();active=self.job.active
        event={'kind':'catalog_playlist_result','request_id':active['request_id'],'entries':[entry(0)],
               'start':0,'offset':1,'total':1,'complete':True}
        self.assertFalse(self.job.receive(Mock(),event))
        self.assertFalse(self.job.receive(self.worker,{**event,'request_id':'old-request'}))
        self.assertIs(self.job.active,active);self.assertEqual(self.job.manifest,{})

    def test_not_found_advances_to_next_track_without_claiming_lyrics_exist(self):
        self.manifest((TRACK,SECOND));self.tick()
        self.assertTrue(self.response(code='not_found',lines=[],synced=False))
        result=self.job.value['spotify'][TRACK['id']]
        self.assertEqual(result['status'],'not_found');self.assertFalse(result['complete'])
        self.assertEqual(self.job.store.writes,[])
        self.tick(4);self.assertEqual(self.worker.send.call_args.args[0]['track']['id'],SECOND['id'])

    def test_timeout_cancels_then_retries_and_late_old_response_is_ignored(self):
        self.manifest();self.tick();old_id=self.job.active['request_id']
        self.tick(91)
        self.assertIsNone(self.job.active)
        self.assertEqual(self.worker.send.call_args.args[0],{'action':'catalog_cancel','request_id':old_id})
        self.assertNotIn(TRACK['id'],self.job.value.get('spotify',{}))
        self.tick(11);self.assertNotEqual(self.job.active['request_id'],old_id)
        active=self.job.active
        self.assertFalse(self.job.receive(self.worker,{'kind':'batch_lyrics_result','request_id':old_id,
            'provider_id':TRACK['id'],'lines':[{'text':'wrong old row','time':1.}]}))
        self.assertIs(self.job.active,active);self.assertEqual(self.job.store.writes,[])
        self.response();self.assertEqual(self.job.store.writes,[TRACK['id']])

    def test_rate_limit_waits_full_delay_and_does_not_mark_track_done(self):
        self.manifest();self.tick();sent=self.worker.send.call_count
        self.response(code='rate_limited',retry_after=120,lines=[])
        self.assertNotIn(TRACK['id'],self.job.value.get('spotify',{}))
        self.assertNotIn(TRACK['id'],self.job.value.get('errors',{}))
        self.tick(119);self.assertEqual(self.worker.send.call_count,sent)
        self.tick(2);self.assertEqual(self.worker.send.call_count,sent+1)

    def test_wrong_provider_id_and_invalid_timing_never_reach_store(self):
        self.manifest();self.tick()
        self.response(provider_id=SECOND['id'])
        self.assertEqual(self.job.store.writes,[])
        self.tick(11);self.response(lines=[{'text':'fixture','time':999.}])
        self.assertEqual(self.job.store.writes,[])
        self.assertNotIn(TRACK['id'],self.job.value.get('spotify',{}))

    def test_rate_limit_deadline_survives_restart(self):
        self.manifest();self.tick()
        with patch('playlist_lyrics_job.time.time',return_value=1000.):
            self.response(code='rate_limited',retry_after=120,lines=[])
            restarted=PlaylistLyricsJob(self.app)
        self.assertEqual(restarted.value['retry_at'],1120.)
        self.assertAlmostEqual(restarted.next_at-self.now,120.)
        self.assertNotIn(TRACK['id'],restarted.value.get('spotify',{}))

    def test_repeated_fetch_errors_move_to_reviewable_enrichment_instead_of_claiming_success(self):
        self.manifest()
        for _ in range(3):
            self.tick(11);self.response(code='response',lines=[])
        self.assertEqual(self.job.value['spotify'][TRACK['id']]['status'],'error')
        self.tick(11);self.assertEqual(self.job.value['phase'],'enrichment')
        self.assertFalse(self.job.value.get('all_complete',False))

    def test_partial_enrichment_finishes_in_review_with_exact_duplicate_missing_count(self):
        self.manifest((TRACK,TRACK),phase='enrichment');self.tick();request=self.job.enrichment_active
        result=saved(translated=False,timed=True)
        self.job.events.put((request,{**result,'sources':[],'missing':['translation']},None))
        self.tick();self.tick(2)
        self.assertEqual(self.job.value['phase'],'review');self.assertFalse(self.job.value['all_complete'])
        self.assertEqual(self.job.value['summary']['playlist_entries'],2)
        self.assertEqual(self.job.value['summary']['unique_tracks'],1)
        self.assertEqual(self.job.value['summary']['missing_entries'],2)
        self.assertEqual(self.job.value['summary']['synced_entries'],2)
        self.assertTrue(self.job.value['collection_pass_finished']);self.assertFalse(self.job.enabled)

    def test_full_timing_and_translation_are_both_required_per_playlist_entry(self):
        self.manifest((TRACK,TRACK,SECOND),phase='enrichment')
        self.job.store.records={TRACK['id']:saved(),SECOND['id']:saved(SECOND)}
        self.job.finish()
        self.assertEqual(self.job.value['phase'],'complete');self.assertTrue(self.job.value['all_complete'])
        self.assertEqual(self.job.value['summary']['complete_entries'],3)
        report=json.loads((self.directory/'playlist_lyrics'/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['all_complete']);self.assertEqual(len(report['records']),3)

    def test_unknown_or_local_entry_blocks_all_complete_even_when_known_tracks_are_ready(self):
        self.manifest(phase='enrichment');self.job.store.records[TRACK['id']]=saved()
        self.job.manifest['entries'].append({'index':1,'status':'local','track_id':'','track':None})
        self.job.manifest['total']=2
        self.job.finish()
        self.assertEqual(self.job.value['phase'],'review');self.assertFalse(self.job.value['all_complete'])
        self.assertEqual(self.job.value['summary']['missing_entries'],1)

    def test_stale_enrichment_callback_cannot_save_for_a_replacement_request(self):
        self.manifest(phase='enrichment');self.tick();old=self.job.enrichment_active
        replacement={**old,'started':self.now+1};self.job.enrichment_active=replacement
        self.job.events.put((old,{**saved(),'sources':[]},None));self.tick()
        self.assertIs(self.job.enrichment_active,replacement);self.assertEqual(self.job.store.writes,[])

    def test_enrichment_commits_while_spotify_request_is_inflight(self):
        self.manifest((TRACK,SECOND));self.tick()
        self.assertIsNone(self.job.enrichment_active)
        self.response();self.tick(4)
        enrich=self.job.enrichment_active;spotify=self.job.active
        self.assertEqual(enrich['track']['id'],TRACK['id'])
        self.assertEqual(spotify['track']['id'],SECOND['id'])
        self.job.events.put((enrich,{**saved(),'sources':[]},None));self.tick()
        self.assertIs(self.job.active,spotify)
        self.assertTrue(self.job.value['enrichment'][TRACK['id']]['complete'])
        self.assertNotIn(SECOND['id'],self.job.value['enrichment'])
        self.assertEqual(len(self.threads),1)

    def test_spotify_rate_limit_does_not_delay_independent_enrichment(self):
        self.manifest((TRACK,SECOND));self.tick();self.response();self.tick(4)
        enrich=self.job.enrichment_active
        self.response(code='rate_limited',retry_after=120,lines=[])
        deadline=self.job.next_at;sent=self.worker.send.call_count
        self.job.events.put((enrich,{**saved(),'sources':[]},None));self.tick(4)
        self.assertTrue(self.job.value['enrichment'][TRACK['id']]['complete'])
        self.assertEqual(self.job.next_at,deadline)
        self.assertIn('retry_at',self.job.value)
        self.assertEqual(self.worker.send.call_count,sent)

    def test_enrichment_rate_limit_survives_restart_without_blocking_spotify(self):
        self.manifest((TRACK,SECOND));self.tick();self.response();self.tick()
        enrich=self.job.enrichment_active
        with patch('playlist_lyrics_job.time.time',return_value=1000.):
            self.job.events.put((enrich,None,('rate_limited',120)))
            self.tick()
            restarted=PlaylistLyricsJob(self.app)
        self.assertEqual(restarted.value['enrichment_retry_at'],1120.)
        self.assertAlmostEqual(restarted.enrichment_next_at-self.now,120.)
        self.assertNotIn('retry_at',self.job.value)
        self.tick(3)
        self.assertEqual(self.job.active['track']['id'],SECOND['id'])
        self.assertIsNone(self.job.enrichment_active)
        self.assertNotIn(TRACK['id'],self.job.value['enrichment'])
        self.assertEqual(len(self.threads),1)

    def test_spotify_finishing_does_not_finish_or_duplicate_active_enrichment(self):
        self.manifest();self.tick();self.response();self.tick(4)
        enrich=self.job.enrichment_active
        self.assertEqual(self.job.value['phase'],'enrichment')
        self.tick(5)
        self.assertIs(self.job.enrichment_active,enrich)
        self.assertFalse(self.job.value.get('collection_pass_finished',False))
        self.assertEqual(len(self.threads),1)
        self.job.events.put((enrich,{**saved(),'sources':[]},None));self.tick()
        self.assertTrue(self.job.value['collection_pass_finished'])
        self.assertEqual(self.job.value['phase'],'complete')

    def test_deferred_track_is_pending_while_other_tracks_continue(self):
        self.manifest((TRACK,SECOND),phase='enrichment')
        with patch('playlist_lyrics_job.time.time',side_effect=lambda:1000+self.now):
            self.tick();request=self.job.enrichment_active
            result={**saved(translated=False),'attempts':[{'source':'wiki_browser',
                    'state':'deferred_cooldown','retry_after':120}], 'missing':['korean_translation']}
            self.job.events.put((request,result,None));self.tick()
            pending=self.job.value['enrichment_pending'][TRACK['id']]
            self.assertEqual(pending['retry_at'],1000+self.now+120)
            self.assertEqual(pending['rounds'],0)
            self.assertNotIn(TRACK['id'],self.job.value.get('enrichment',{}))
            self.tick(2)
            self.assertEqual(self.job.enrichment_active['track']['id'],SECOND['id'])
            self.assertFalse(self.job.value.get('collection_pass_finished',False))

    def test_pending_deadline_survives_restart_and_cannot_finish_early(self):
        self.manifest(phase='enrichment')
        self.job.manifest_path.parent.mkdir(parents=True)
        self.job.manifest_path.write_text(json.dumps(self.job.manifest),encoding='utf-8')
        with patch('playlist_lyrics_job.time.time',side_effect=lambda:1000+self.now):
            self.tick();request=self.job.enrichment_active
            result={**saved(translated=False),'attempts':[{'source':'wiki_browser',
                    'state':'browser_timeout','retry_after':120}]}
            self.job.events.put((request,result,None));self.tick()
            deadline=self.job.value['enrichment_pending'][TRACK['id']]['retry_at']
            self.job=PlaylistLyricsJob(self.app);self.job.store=MemoryStore()
            self.assertEqual(self.job.value['enrichment_pending'][TRACK['id']]['retry_at'],deadline)
            self.tick(119)
            self.assertIsNone(self.job.enrichment_active)
            self.assertFalse(self.job.value.get('collection_pass_finished',False))
            self.tick(2)
            self.assertEqual(self.job.enrichment_active['track']['id'],TRACK['id'])

    def test_three_actual_source_failures_stop_with_error_and_incomplete_coverage(self):
        self.manifest(phase='enrichment')
        with patch('playlist_lyrics_job.time.time',side_effect=lambda:1000+self.now):
            for round_number in range(1,4):
                self.tick(31);request=self.job.enrichment_active
                self.assertIsNotNone(request)
                result={**saved(translated=False),'attempts':[{'source':'wiki_browser','state':'browser_timeout'}],
                        'missing':['korean_translation']}
                self.job.events.put((request,result,None));self.tick()
                if round_number<3:
                    self.assertEqual(self.job.value['enrichment_pending'][TRACK['id']]['rounds'],round_number)
            outcome=self.job.value['enrichment'][TRACK['id']]
            self.assertEqual((outcome['status'],outcome['code'],outcome['rounds']),('error','source_retry_exhausted',3))
            self.assertNotIn(TRACK['id'],self.job.value['enrichment_pending'])
            self.assertFalse(outcome['complete']);self.assertEqual(self.job.value['phase'],'review')
            self.assertEqual(len(self.threads),3)

    def test_cooldown_only_rounds_do_not_exhaust_failures_and_success_clears_pending(self):
        self.manifest(phase='enrichment')
        with patch('playlist_lyrics_job.time.time',side_effect=lambda:1000+self.now):
            for _ in range(4):
                self.tick(31);request=self.job.enrichment_active
                result={**saved(translated=False),'attempts':[{'source':'wiki_browser','state':'deferred_cooldown',
                                                              'retry_after':30}]}
                self.job.events.put((request,result,None));self.tick()
                self.assertEqual(self.job.value['enrichment_pending'][TRACK['id']]['rounds'],0)
                self.assertNotIn(TRACK['id'],self.job.value.get('enrichment',{}))
            self.tick(31);request=self.job.enrichment_active
            self.job.events.put((request,saved(),None));self.tick()
            self.assertNotIn(TRACK['id'],self.job.value['enrichment_pending'])
            self.assertEqual(self.job.value['phase'],'complete')

    def test_invalid_lyrics_diagnostics_store_only_shape_counts_and_stage(self):
        self.manifest();self.tick()
        self.response(lines=[None])
        self.assertEqual(self.job.value['invalid_lyrics_diagnostics'][TRACK['id']]['stage'],'rows')
        self.tick(11);self.response(lines=[{'text':'PRIVATE_LYRIC_TEXT','time':300}])
        self.assertEqual(self.job.value['invalid_lyrics_diagnostics'][TRACK['id']]['stage'],'times')
        self.tick(11)
        self.job.store.save=Mock(return_value=False)
        self.response(lines=[{'text':'PRIVATE_LYRIC_TEXT','time':1},{'text':'  ','time':2},{'text':'♪','time':3}])
        diagnostic=self.job.value['invalid_lyrics_diagnostics'][TRACK['id']]
        self.assertEqual(diagnostic,{'stage':'store','rows':3,'blankRows':1,'symbolRows':1,'timedCount':3})
        self.assertNotIn('PRIVATE_LYRIC_TEXT',self.job.path.read_text(encoding='utf-8'))
        self.assertNotIn('♪',json.dumps(diagnostic,ensure_ascii=False))

    def test_reviewed_import_in_disabled_review_ignores_clocks_and_preserves_existing_annotations(self):
        directory=self.reviewed_store()
        old=saved(translated=False)
        old['lines']=(Line(old['lines'][0].original,'existing reading','existing translation'),old['lines'][1])
        self.assertTrue(self.job.store.save(TRACK,old['lines'],old['direct']))
        payload=self.reviewed_payload();payload.update(times=[999,999],direct={'source':'AI','times':[-1]})
        path=directory/(TRACK['id']+'.json');atomic_json(path,payload)
        self.job.value['enrichment_pending']={TRACK['id']:{'retry_at':9999999}}
        self.job.next_at=self.now+10000
        self.tick()
        actual=self.job.store.load(TRACK['id'])
        self.assertEqual(actual['direct'],old['direct'])
        self.assertEqual(actual['lines'][0],old['lines'][0])
        self.assertEqual(actual['lines'][1].korean,'reviewed translation')
        receipt=self.job.value['reviewed_imports'][TRACK['id']]
        self.assertEqual(receipt['status'],'applied');self.assertTrue(receipt['coverage']['complete'])
        self.assertEqual(len(receipt['sha256']),64)
        self.assertNotIn(TRACK['id'],self.job.value['enrichment_pending'])
        self.assertEqual(self.job.value['enrichment'][TRACK['id']]['status'],'reviewed')
        self.assertEqual(self.job.value['phase'],'review');self.assertFalse(self.job.value.get('all_complete',False))
        self.assertTrue(path.exists());self.worker.send.assert_not_called()

    def test_reviewed_rejects_save_that_reports_success_without_persisting_translation(self):
        directory=self.reviewed_store();payload=self.reviewed_payload()
        atomic_json(directory/(TRACK['id']+'.json'),payload)
        before=self.job.store.load(TRACK['id'])
        with patch.object(self.job.store,'save',return_value=True):
            self.tick()
        self.assertEqual(self.job.store.load(TRACK['id']),before)
        receipt=self.job.value['reviewed_imports'][TRACK['id']]
        self.assertEqual(receipt['status'],'rejected')
        self.assertEqual(receipt['code'],'save_verification_failed')
        self.assertNotIn(TRACK['id'],self.job.value.get('enrichment',{}))

    def test_reviewed_original_array_requires_character_exact_identity(self):
        directory=self.reviewed_store();payload=self.reviewed_payload()
        payload['lines'][0]['original']+=' '
        atomic_json(directory/(TRACK['id']+'.json'),payload);self.tick()
        receipt=self.job.value['reviewed_imports'][TRACK['id']]
        self.assertEqual(receipt['code'],'originals_mismatch')
        self.assertEqual(coverage(self.job.store.load(TRACK['id']))['translated'],0)
        self.assertNotIn(payload['lines'][0]['original'],self.job.path.read_text(encoding='utf-8'))

    def test_reviewed_requires_manifest_id_and_matching_full_metadata(self):
        directory=self.reviewed_store();payload=self.reviewed_payload()
        atomic_json(directory/(SECOND['id']+'.json'),payload);self.tick()
        self.assertEqual(self.job.value['reviewed_imports'][SECOND['id']]['code'],'unknown_track')
        for field,other in (('id',SECOND['id']),('title','Wrong title'),('album','Wrong album'),('duration',201)):
            payload=self.reviewed_payload();payload['track'][field]=other
            atomic_json(directory/(TRACK['id']+'.json'),payload);self.tick(5)
            self.assertEqual(self.job.value['reviewed_imports'][TRACK['id']]['code'],'track_mismatch')
        self.assertEqual(coverage(self.job.store.load(TRACK['id']))['translated'],0)

    def test_reviewed_receipt_skips_identical_sha_after_atomic_rewrite_and_restart(self):
        directory=self.reviewed_store();payload=self.reviewed_payload()
        path=directory/(TRACK['id']+'.json');atomic_json(path,payload);self.tick()
        original=self.job.value['reviewed_imports'][TRACK['id']].copy()
        atomic_json(path,payload)
        self.job=PlaylistLyricsJob(self.app)
        with patch.object(self.job.store,'save',wraps=self.job.store.save) as save:
            self.tick(5)
            save.assert_not_called()
        self.assertEqual(self.job.value['reviewed_imports'][TRACK['id']],original)

    def test_reviewed_restart_skips_applied_files_and_processes_only_one_new_file(self):
        third={**TRACK,'id':'c'*22,'title':'Third fixture'}
        fourth={**TRACK,'id':'d'*22,'title':'Fourth fixture'}
        directory=self.reviewed_store((TRACK,SECOND,third,fourth))
        for track in (TRACK,SECOND):
            atomic_json(directory/(track['id']+'.json'),self.reviewed_payload(track))
        self.tick();self.tick(5)
        applied=copy.deepcopy(self.job.value['reviewed_imports'])
        for track in (third,fourth):
            atomic_json(directory/(track['id']+'.json'),self.reviewed_payload(track))
        self.job=PlaylistLyricsJob(self.app)
        self.assertEqual(self.job.reviewed_seen,{})
        with patch.object(self.job.store,'save',wraps=self.job.store.save) as save:
            self.tick(5)
            self.assertEqual(save.call_count,1)
            self.assertEqual(self.job.value['reviewed_imports'][third['id']]['status'],'applied')
            self.assertNotIn(fourth['id'],self.job.value['reviewed_imports'])
            for identifier,receipt in applied.items():
                self.assertEqual(self.job.value['reviewed_imports'][identifier],receipt)
                self.assertIn(identifier+'.json',self.job.reviewed_seen)
            self.tick(4);self.assertEqual(save.call_count,1)
            self.tick();self.assertEqual(save.call_count,2)
            self.assertEqual(self.job.value['reviewed_imports'][fourth['id']]['status'],'applied')

    def test_reviewed_processes_one_file_per_five_seconds_and_rejects_oversize_and_blank_rows(self):
        directory=self.reviewed_store((TRACK,SECOND))
        for track in (TRACK,SECOND):atomic_json(directory/(track['id']+'.json'),self.reviewed_payload(track))
        self.tick();self.assertEqual(len(self.job.value['reviewed_imports']),1)
        self.tick(4);self.assertEqual(len(self.job.value['reviewed_imports']),1)
        self.tick();self.assertEqual(len(self.job.value['reviewed_imports']),2)
        path=directory/(TRACK['id']+'.json')
        path.write_bytes(b'x'*(1024*1024+1));self.tick(5)
        self.assertEqual(self.job.value['reviewed_imports'][TRACK['id']]['code'],'file_too_large')
        payload=self.reviewed_payload();payload['lines'][0]['original']=' '
        atomic_json(path,payload);self.tick(5)
        self.assertEqual(self.job.value['reviewed_imports'][TRACK['id']]['code'],'invalid_rows')

    def test_inflight_source_callback_cannot_erase_imported_translation(self):
        directory=self.reviewed_store()
        old=saved(translated=False,timed=False)
        # Use a fresh document with a partial clock, leaving enrichment active
        # after translation import so its old result exercises store merging.
        temporary=PlaylistLyricsStore(self.directory/'partial')
        old['direct']['times']=[1,None]
        self.assertTrue(temporary.save(TRACK,old['lines'],old['direct']))
        self.job.store=temporary
        self.job.value['phase']='enrichment';self.job.enabled=True
        request={'phase':'enrichment','track':TRACK,'started':self.now}
        self.job.enrichment_active=request
        atomic_json(directory/(TRACK['id']+'.json'),self.reviewed_payload())
        self.tick()
        self.assertIs(self.job.enrichment_active,request)
        self.assertEqual(coverage(self.job.store.load(TRACK['id']))['translated'],2)
        self.job.events.put((request,saved(translated=False),None));self.tick()
        actual=self.job.store.load(TRACK['id'])
        self.assertEqual([row.korean for row in actual['lines']],['reviewed translation']*2)
        self.assertTrue(coverage(actual)['complete'])

    def test_close_cancels_both_lanes_and_ignores_late_enrichment(self):
        self.manifest((TRACK,SECOND));self.tick();self.response();self.tick(4)
        enrich=self.job.enrichment_active;spotify=self.job.active
        self.job.close()
        self.assertTrue(self.job.cancel.is_set())
        self.worker.send.assert_called_with({'action':'catalog_cancel','request_id':spotify['request_id']})
        self.job.events.put((enrich,{**saved(),'sources':[]},None));self.tick(5)
        self.assertNotIn(TRACK['id'],self.job.value['enrichment'])
        self.assertEqual(self.job.store.writes,[TRACK['id']])

    def test_coverage_does_not_count_unsynced_originals_as_synchronized(self):
        value=coverage(saved(translated=True,timed=False))
        self.assertEqual(value,{'lines':2,'translated':2,'timed':0,'complete':False})

    def test_priority_enrichment_only_uses_selected_tracks_with_stored_originals(self):
        empty={**TRACK,'id':'c'*22,'title':'Empty stored fixture'}
        selected={**TRACK,'id':'d'*22,'title':'Selected fixture'}
        self.manifest((TRACK,SECOND,empty,selected),phase='enrichment')
        self.job.value['priority_track_ids']=[SECOND['id'],empty['id'],selected['id'],selected['id']]
        self.job.store.records={TRACK['id']:saved(translated=False),
                                empty['id']:{'track':empty,'lines':(),'direct':None},
                                selected['id']:saved(selected,translated=False)}
        self.app.lyric_cache.load.return_value=saved(SECOND,translated=False)
        with patch.object(self.job,'_existing',wraps=self.job._existing) as fallback:
            self.tick()
            fallback.assert_not_called()
        self.assertEqual(self.job.enrichment_active['track']['id'],selected['id'])
        self.assertEqual(len(self.threads),1)
        self.app.lyric_cache.load.assert_not_called()
        self.worker.send.assert_not_called()

    def test_absent_priority_list_keeps_enrichment_cache_fallback(self):
        self.manifest(phase='enrichment')
        self.app.lyric_cache.load.return_value=saved(translated=False)
        self.tick()
        self.app.lyric_cache.load.assert_called_once()
        self.assertEqual(self.job.store.writes,[TRACK['id']])
        self.assertEqual(self.job.enrichment_active['track']['id'],TRACK['id'])

    def test_empty_or_invalid_priority_list_never_expands_scope_or_finishes_collection(self):
        self.manifest(phase='enrichment')
        self.job.store.records[TRACK['id']]=saved()
        self.job.value['enrichment']={TRACK['id']:{'status':'existing',**coverage(saved())}}
        for supplied in ([],None,TRACK['id'],42,[None,{},'unknown']):
            with self.subTest(supplied=supplied):
                self.job.value['priority_track_ids']=supplied
                before=copy.deepcopy(self.job.value['enrichment'])
                self.tick();self.job.finish()
                self.assertEqual(self.job.value['enrichment'],before)
                self.assertEqual(self.job.value['phase'],'enrichment')
                self.assertTrue(self.job.enabled)
                self.assertNotIn('collection_pass_finished',self.job.value)
                self.assertNotIn('all_complete',self.job.value)
                self.assertFalse((self.directory/'playlist_lyrics'/'report.json').exists())
        self.assertEqual(self.threads,[]);self.worker.send.assert_not_called()

    def test_priority_restart_preserves_global_and_track_retries_and_closed_errors(self):
        self.manifest((TRACK,SECOND),phase='enrichment')
        self.job.value.update(priority_track_ids=[SECOND['id'],TRACK['id']],retry_at=1100.,
                              enrichment_retry_at=1120.,
                              enrichment={SECOND['id']:{'status':'error','code':'source_retry_exhausted',
                                                        'attempts':[{'state':'browser_timeout'}],'rounds':3}},
                              errors={SECOND['id']:{'phase':'enrichment','code':'save_failed','attempts':3}},
                              enrichment_pending={TRACK['id']:{'retry_at':1150.,'rounds':2,
                                  'attempts':[{'state':'deferred_cooldown','retry_after':150}]}})
        preserved={key:copy.deepcopy(self.job.value[key]) for key in
                   ('retry_at','enrichment_retry_at','enrichment','enrichment_pending','errors')}
        atomic_json(self.job.manifest_path,self.job.manifest)
        self.job.save()
        with patch('playlist_lyrics_job.time.time',return_value=1000.) as wall:
            self.job=PlaylistLyricsJob(self.app);self.job.store=MemoryStore()
            self.job.store.records={TRACK['id']:saved(translated=False),
                                    SECOND['id']:saved(SECOND,translated=False)}
            wall.return_value=1119.;self.tick(119)
            self.assertIsNone(self.job.enrichment_active)
            wall.return_value=1121.;self.tick(2)
            self.assertIsNone(self.job.enrichment_active)
            self.assertEqual(self.threads,[])
            wall.return_value=1151.;self.tick(30)
            self.assertEqual(self.job.enrichment_active['track']['id'],TRACK['id'])
        for key,value in preserved.items():self.assertEqual(self.job.value[key],value)
        self.assertEqual(len(self.threads),1)

    def test_priority_exhaustion_keeps_full_collection_and_closed_results_unchanged(self):
        self.manifest((TRACK,SECOND),phase='enrichment')
        self.job.value['priority_track_ids']=[TRACK['id']]
        self.job.store.records={TRACK['id']:saved(translated=False),SECOND['id']:saved(SECOND,translated=False)}
        self.job.value['enrichment']={SECOND['id']:{'status':'error','code':'source_retry_exhausted','rounds':3}}
        self.job.value['errors']={SECOND['id']:{'phase':'enrichment','code':'save_failed','attempts':3}}
        self.job.value['collection_pass_finished']=False
        self.job.value['all_complete']=False
        previous=copy.deepcopy(self.job.value['enrichment'][SECOND['id']])
        self.tick();request=self.job.enrichment_active
        self.job.events.put((request,{**saved(translated=False),'attempts':[],
                                     'missing':['korean_translation']},None))
        self.tick();closed=copy.deepcopy(self.job.value['enrichment'][TRACK['id']])
        for _ in range(4):self.tick(300)
        self.assertEqual(len(self.threads),1)
        self.assertEqual(self.job.value['enrichment'][TRACK['id']],closed)
        self.assertEqual(self.job.value['enrichment'][SECOND['id']],previous)
        self.assertEqual(self.job.value['errors'][SECOND['id']]['attempts'],3)
        self.assertEqual(self.job.value['phase'],'enrichment');self.assertTrue(self.job.enabled)
        self.assertFalse(self.job.value['collection_pass_finished']);self.assertFalse(self.job.value['all_complete'])
        self.assertFalse((self.directory/'playlist_lyrics'/'report.json').exists())
        self.assertIn('실행 가능한 대상 없음',self.job.enrichment_status)

    def test_priority_filter_does_not_block_reviewed_import_for_other_manifest_tracks(self):
        directory=self.reviewed_store((TRACK,SECOND))
        self.job.value['priority_track_ids']=[TRACK['id']]
        atomic_json(directory/(SECOND['id']+'.json'),self.reviewed_payload(SECOND))
        self.tick()
        self.assertEqual(self.job.value['reviewed_imports'][SECOND['id']]['status'],'applied')
        self.assertTrue(coverage(self.job.store.load(SECOND['id']))['complete'])
        self.assertEqual(self.job.value['phase'],'review')
        self.worker.send.assert_not_called()

    def test_playlist_takeover_drains_old_catalog_error_before_starting_playlist(self):
        from catalog_sync import CatalogSync
        (self.directory/'spotify_catalog_sync.json').write_text(json.dumps({'enabled':True}))
        self.app.explorer_dialog=None
        sync=CatalogSync(self.app);sync.catalog=SimpleNamespace(songs=[]);sync.pool_stamp=0
        sync.active={'job':self.worker,'title':'Old search'}
        self.app.catalog_sync=sync
        self.tick();self.assertIsNone(self.job.active)
        sync.events.put(('search_error',(sync.active,'interrupted')))
        sync.tick(allow_search=False)
        self.assertIsNone(sync.active);self.worker.send.assert_not_called()
        self.tick()
        self.assertEqual(self.worker.send.call_args.args[0]['action'],'catalog_playlist')

    def test_catalog_success_is_committed_during_takeover_without_starting_another_search(self):
        from catalog_sync import CatalogSync
        (self.directory/'spotify_catalog_sync.json').write_text(json.dumps({'enabled':True}))
        self.app.explorer_dialog=None
        sync=CatalogSync(self.app);sync.catalog=SimpleNamespace(songs=[]);sync.pool_stamp=0
        sync.links=SimpleNamespace(apply=Mock(return_value=0),coverage=Mock(return_value={'checked_titles':1}))
        sync.store_tracks=Mock();sync.library=Mock(return_value=Mock())
        request={'job':self.worker,'title':TRACK['title']};sync.active=request
        sync.events.put(('search',(request,{'id':'fixture','title':TRACK['title']},[])))
        sync.tick(allow_search=False)
        self.assertIsNone(sync.active);sync.links.apply.assert_called_once()
        self.assertEqual(sync.value['last_search'],TRACK['title'])
        self.worker.send.assert_not_called()


if __name__=='__main__':unittest.main()
