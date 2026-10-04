import tempfile
from pathlib import Path
import unittest
import time

from lyrics import Line
from playlist_lyrics_store import PlaylistLyricsStore
from storage import atomic_json
from tools.audit_playlist_lyrics import audit


class PlaylistAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.track={'id':'a'*22,'title':'Audit song','artists':['Audit artist'],'album':'Album','duration':100.}
        self.state={'playlist_id':'p'*22,'expected_entries':2,'collection_pass_finished':True,
                    'shutdown_requested':True,'shutdown_policy':'all_complete'}
        self.manifest={'playlist_id':'p'*22,'complete':True,'total':2,'entries':[
            {'index':i,'status':'track','track_id':self.track['id'],'track':self.track} for i in range(2)]}
        atomic_json(self.directory/'playlist_lyrics_job.json',self.state)
        atomic_json(self.directory/'playlist_lyrics'/'manifest.json',self.manifest)
        atomic_json(self.directory/'spotify_catalog_sync.json',{'search_pass_finished':True,'enabled':False})

    def write_lyrics(self,translated=True):
        lines=(Line('Audit original','','번역' if translated else ''),)
        self.assertTrue(PlaylistLyricsStore(self.directory).save(self.track,lines,
            {'source':'Spotify','provider_id':self.track['id'],'lines':lines,'times':[1.]}))

    def test_duplicate_rows_count_but_shared_file_is_checked(self):
        self.write_lyrics()
        summary=audit(self.directory)['summary']
        self.assertEqual(summary['unique_tracks'],1)
        self.assertEqual(summary['complete_entries'],2)
        self.assertTrue(summary['shutdown_ready'])

    def test_missing_translation_blocks_shutdown_even_after_both_passes(self):
        self.write_lyrics(False)
        self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])

    def test_catalog_still_running_prevents_shutdown(self):
        self.write_lyrics()
        atomic_json(self.directory/'spotify_catalog_sync.json',{'search_pass_finished':False,'enabled':True})
        self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])

    def test_explicit_partial_policy_still_requires_reviewed_report(self):
        self.state['shutdown_policy']='after_reviewed_pass'
        atomic_json(self.directory/'playlist_lyrics_job.json',self.state)
        self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])
        self.state['reviewed_incomplete_report']=True
        atomic_json(self.directory/'playlist_lyrics_job.json',self.state)
        self.assertTrue(audit(self.directory)['summary']['shutdown_ready'])

    def test_missing_or_changed_manifest_never_authorizes_shutdown(self):
        self.write_lyrics()
        self.manifest['entries'].pop()
        atomic_json(self.directory/'playlist_lyrics'/'manifest.json',self.manifest)
        result=audit(self.directory)['summary']
        self.assertFalse(result['shutdown_ready'])
        self.assertIn('incomplete_manifest',result['integrity_errors'])

    def quota(self,used=70,age=0,enabled=True,reset=None):
        atomic_json(self.directory/'playlist_lyrics'/'shutdown_policy.json',{
            'policy':'all_complete_or_quota','enabled':enabled,'user_authorized':True,
            'remaining_percent_threshold':30})
        atomic_json(self.directory/'playlist_lyrics'/'quota_snapshot.json',{
            'source':'codex_get_usage_limits','limit_id':'codex','checked_at':time.time()-age,
            'windows':[{'name':'primary','used_percent':used,'resets_at':reset}]})

    def test_fresh_quota_threshold_stops_even_incomplete_active_collection(self):
        self.quota()
        atomic_json(self.directory/'spotify_catalog_sync.json',{'enabled':True})
        summary=audit(self.directory)['summary']
        self.assertTrue(summary['shutdown_ready'])
        self.assertFalse(summary['all_complete'])
        self.assertEqual(summary['complete_entries'],0)
        self.assertEqual(summary['shutdown_reason'],'quota_threshold')

    def test_quota_does_not_confuse_used_and_remaining(self):
        self.quota(used=30)
        summary=audit(self.directory)['summary']
        self.assertEqual(summary['quota_remaining_percent'],70)
        self.assertFalse(summary['shutdown_ready'])

    def test_stale_missing_invalid_or_reset_quota_never_authorizes_shutdown(self):
        for kwargs in ({'age':301},{'used':None},{'used':True},{'reset':time.time()-1},{'enabled':False}):
            with self.subTest(kwargs=kwargs):
                self.quota(**kwargs)
                self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])
        (self.directory/'playlist_lyrics'/'quota_snapshot.json').unlink()
        self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])

    def test_consumed_shutdown_request_prevents_repeated_quota_shutdown(self):
        self.quota()
        self.state['shutdown_requested']=False
        atomic_json(self.directory/'playlist_lyrics_job.json',self.state)
        self.assertFalse(audit(self.directory)['summary']['shutdown_ready'])

    def test_either_current_quota_window_can_reach_threshold(self):
        self.quota(used=10)
        atomic_json(self.directory/'playlist_lyrics'/'quota_snapshot.json',{
            'source':'codex_get_usage_limits','limit_id':'codex','checked_at':time.time(),
            'windows':[{'name':'primary','used_percent':10},
                       {'name':'secondary','used_percent':71,'resets_at':time.time()+3600}]})
        summary=audit(self.directory)['summary']
        self.assertEqual(summary['quota_remaining_percent'],29)
        self.assertTrue(summary['shutdown_ready'])
