import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

from catalog_link_queue import CatalogLinkQueue
from explorer_library import ExplorerLibrary
from tests import test_explorer_dialog as fixtures


class QueueTests(unittest.TestCase):
    def test_songs_are_searched_first_without_excluding_other_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('voice','spoken'),fixtures.song('inst','instrumental'),fixtures.song('vocal')]
            queue=CatalogLinkQueue(directory,songs,ExplorerLibrary(directory))
            visited=[]
            while (song:=queue.next_song()) is not None:
                visited.append(song['id']);queue.apply(song,[])
            self.assertEqual(visited,['vocal','inst','voice'])
    def test_search_subset_does_not_override_ambiguity_in_collected_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            song=fixtures.song('a');library=ExplorerLibrary(directory)
            queue=CatalogLinkQueue(directory,[song],library)
            queue.pool=[fixtures.track(),fixtures.track('b')]
            self.assertEqual(queue.apply(song,[fixtures.track()]),0)
            self.assertIsNone(library.linked_track(song['id']))

    def test_deferred_failure_does_not_block_remaining_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('a'),fixtures.song('b')]
            queue=CatalogLinkQueue(directory,songs,ExplorerLibrary(directory))
            self.assertEqual(queue.next_song(skip={'songa'})['id'],'b')
    def test_selected_song_first_then_resume_without_repeating_checked_title(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('a'),fixtures.song('b')];library=ExplorerLibrary(directory)
            queue=CatalogLinkQueue(directory,songs,library)
            self.assertEqual(queue.next_song([songs[1]])['id'],'b')
            queue.apply(songs[1],[])
            resumed=CatalogLinkQueue(directory,songs,library)
            self.assertEqual(resumed.next_song([songs[1]])['id'],'a')
            self.assertEqual(library.value['spotify'],{})

    def test_unsearched_titles_precede_expired_checks_after_restart(self):
        with tempfile.TemporaryDirectory() as directory,patch('catalog_link_queue.time.time',return_value=200000):
            songs=[fixtures.song('old'),fixtures.song('recent'),fixtures.song('new')]
            library=ExplorerLibrary(directory)
            queue=CatalogLinkQueue(directory,songs,library)
            saved={'schema_version':1,'checked':{'songold':100000,'songrecent':199999}}
            queue.path.write_text(json.dumps(saved),encoding='utf-8')
            resumed=CatalogLinkQueue(directory,songs,library)
            self.assertEqual(resumed.next_song()['id'],'new')
            self.assertEqual(json.loads(queue.path.read_text(encoding='utf-8')),saved)
            self.assertEqual(resumed.next_song([songs[0]])['id'],'old')
            resumed.apply(songs[2],[])
            self.assertEqual(resumed.next_song()['id'],'old')
            resumed.apply(songs[0],[])
            self.assertIsNone(resumed.next_song())

    def test_unsearched_priority_preserves_exclusions_skips_and_title_groups(self):
        with tempfile.TemporaryDirectory() as directory,patch('catalog_link_queue.time.time',return_value=200000):
            songs=[fixtures.song('old'),fixtures.song('linked'),fixtures.song('removed'),
                   fixtures.song('new'),{**fixtures.song('new'),'id':'copy','title':'Song NEW'}]
            library=ExplorerLibrary(directory)
            library.link_spotify('linked',fixtures.track())
            library.unlink_spotify('removed')
            queue=CatalogLinkQueue(directory,songs,library)
            queue.checked={'songold':100000}
            self.assertEqual(queue.next_song()['id'],'new')
            self.assertEqual(queue.next_song(skip={'songnew'})['id'],'old')
            queue.apply(songs[3],[])
            self.assertEqual(queue.next_song()['id'],'old')
            self.assertEqual(queue.coverage()['checked_titles'],2)

    def test_full_search_keeps_unsearched_work_active_and_respects_existing_waits(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from catalog_sync import CatalogSync
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('old'),fixtures.song('recent'),fixtures.song('new')]
            service=SimpleNamespace(job=Mock(),spotify_authenticated=True,
                                    spotify_pending=False,metadata_pending=False)
            app=SimpleNamespace(store=SimpleNamespace(directory=directory),
                                explorer_dialog=None,background_lyrics=service)
            sync=CatalogSync(app)
            sync.enabled=True
            sync.catalog=SimpleNamespace(songs=songs)
            sync.pool_stamp=0
            sync.links=CatalogLinkQueue(directory,songs,ExplorerLibrary(directory))
            sync.links.checked={'songold':100000,'songrecent':199999}
            with patch('catalog_sync.threading.Thread') as worker, \
                 patch('catalog_sync.time.monotonic',return_value=100), \
                 patch('catalog_link_queue.time.time',return_value=200000):
                sync.tick()
                self.assertEqual(sync.active['title'],songs[2]['title'])
                self.assertTrue(sync.enabled)
                self.assertFalse(sync.value.get('search_pass_finished',False))
                worker.return_value.start.assert_called_once_with()
                sync.last_tick=0
                sync.tick()
                self.assertEqual(worker.call_count,1)
                sync.active=None;sync.last_tick=0;sync.next_at=200
                sync.tick()
                self.assertEqual(worker.call_count,1)

    def test_query_applies_all_matching_entries_preserves_manual_choice_and_optout(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[{**fixtures.song('a'),'id':identifier} for identifier in ('a','copy','manual','removed')]
            library=ExplorerLibrary(directory)
            library.link_spotify('manual',fixtures.track('z'));library.unlink_spotify('removed')
            queue=CatalogLinkQueue(directory,songs,library)
            self.assertEqual(queue.apply(songs[0],[fixtures.track()]),2)
            self.assertEqual(library.linked_track('manual')['id'],'z'*22)
            self.assertIsNone(library.linked_track('removed'))
            self.assertIsNone(queue.next_song())

    def test_coverage_separates_recordings_titles_search_attempts_and_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('a'),{**fixtures.song('a'),'id':'a-copy'},
                   fixtures.song('b'),fixtures.song('c'),fixtures.song('voice','spoken'),
                   fixtures.song('instrument','instrumental')]
            library=ExplorerLibrary(directory)
            for identifier in ('a','b','instrument'):
                library.link_spotify(identifier,fixtures.track())
            library.unlink_spotify('c')
            queue=CatalogLinkQueue(directory,songs,library)
            keys=list(queue.groups)
            queue.checked={keys[0]:100,keys[1]:200,'removed-dictionary-title':300,
                           'songvoice':400}
            coverage=queue.coverage()
            self.assertEqual(coverage['total_recordings'],6)
            self.assertEqual(coverage['total_title_groups'],5)
            self.assertEqual(coverage['eligible_recordings'],6)
            self.assertEqual(coverage['excluded_recordings'],0)
            self.assertEqual(coverage['title_groups'],5)
            self.assertEqual(coverage['linked_recordings'],3)
            self.assertEqual(coverage['linked_all_titles'],3)
            self.assertEqual(coverage['linked_eligible_recordings'],3)
            self.assertEqual(coverage['linked_titles'],3)
            self.assertEqual(coverage['fully_linked_titles'],2)
            self.assertEqual(coverage['checked_titles'],3)
            self.assertEqual(coverage['unchecked_titles'],2)
            self.assertEqual(coverage['checked_unresolved_titles'],2)
            self.assertEqual(coverage['pending_titles'],2)
            self.assertEqual(coverage['opted_out_recordings'],1)
            self.assertNotIn('대사·연주 제외',queue.status_text())
            self.assertIn('자동 연결 해제 1음원',queue.status_text())

    def test_recently_exhausted_searches_report_unresolved_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            songs=[fixtures.song('a'),fixtures.song('voice','spoken')]
            queue=CatalogLinkQueue(directory,songs,ExplorerLibrary(directory))
            queue.apply(songs[0],[])
            queue.apply(songs[1],[])
            self.assertIsNone(queue.next_song())
            coverage=queue.coverage()
            self.assertEqual(coverage['checked_titles'],2)
            self.assertEqual(coverage['unchecked_titles'],0)
            self.assertEqual(coverage['checked_unresolved_titles'],2)
            self.assertEqual(coverage['linked_recordings'],0)
            text=queue.status_text()
            self.assertIn('자동검색 대상 2곡 중 2곡 시도',text)
            self.assertIn('시도 후 미확정 2곡',text)
            self.assertNotIn('완료',text)
            self.assertNotIn('없음',text)

    def test_coverage_updates_after_link_and_catalog_changes_without_changing_saved_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            song=fixtures.song('a');library=ExplorerLibrary(directory)
            queue=CatalogLinkQueue(directory,[song],library)
            queue.apply(song,[])
            self.assertEqual(queue.coverage()['checked_unresolved_titles'],1)
            library.link_spotify('a',fixtures.track())
            self.assertEqual(queue.coverage()['linked_recordings'],1)
            self.assertEqual(queue.coverage()['checked_unresolved_titles'],0)
            queue.update([fixtures.song('new')])
            self.assertEqual(queue.coverage()['checked_titles'],0)
            self.assertEqual(queue.coverage()['unchecked_titles'],1)
            self.assertEqual(queue.coverage()['linked_recordings'],0)
            saved=json.loads(queue.path.read_text(encoding='utf-8'))
            self.assertEqual(set(saved),{'schema_version','checked'})
            resumed=CatalogLinkQueue(directory,[fixtures.song('new')],library)
            self.assertEqual(resumed.coverage()['checked_titles'],0)

    def test_invalid_saved_check_timestamps_do_not_inflate_attempt_count(self):
        with tempfile.TemporaryDirectory() as directory:
            stamps=[True,False,0,-1,'123',float('nan'),float('inf'),10**1000,123]
            songs=[fixtures.song(str(index)) for index in range(len(stamps))]
            queue=CatalogLinkQueue(directory,songs,ExplorerLibrary(directory))
            queue.checked=dict(zip(queue.groups,stamps))
            self.assertEqual(queue.coverage()['checked_titles'],1)
            self.assertEqual(queue.coverage()['unchecked_titles'],8)
            # Malformed records are also unseen for search ordering, even when
            # a valid expired title appears first in the dictionary.
            queue.update([songs[-1],*songs[:-1]])
            self.assertEqual(queue.next_song()['id'],'0')


class AutomaticDictionaryTests(unittest.TestCase):
    setUp=fixtures.ExplorerDialogTests.setUp
    tearDown=fixtures.ExplorerDialogTests.tearDown

    def test_cancel_after_music_commit_refreshes_automatic_queue_and_people(self):
        from unittest.mock import Mock
        self.dialog.auto_links=CatalogLinkQueue(self.directory,self.songs,self.dialog.library,self.people)
        self.dialog.music_update_active=True
        updated=[fixtures.song('new')]
        people=[{**self.people[0],'aliases':['New alias']}]
        self.catalog.reload.side_effect=lambda:setattr(self.catalog,'songs',updated)
        self.dialog.cancel.set()
        done=Mock()
        self.dialog.events.put(('done',done,{'updated':True,'people':{'cancelled':True}}))
        self.dialog.window.after_cancel(self.dialog.poll_id)
        with patch('explorer_dialog.performers',return_value=people):self.dialog.poll()
        done.assert_not_called()
        self.assertEqual(self.dialog.auto_links.next_song()['id'],'new')
        self.assertEqual(self.dialog.auto_links.people,people)
        self.assertFalse(self.dialog.music_update_active)

    def test_explicit_play_links_unique_match_without_selection_dialog(self):
        self.spotify.connected=True
        self.spotify.search.return_value=[fixtures.track()]
        with patch.object(self.dialog,'run_job',side_effect=lambda label,work,done:done(work())), \
             patch.object(self.dialog,'play_selected') as play,patch.object(self.dialog,'candidates_dialog') as choose:
            self.dialog.search_spotify(play_after=True)
        self.assertEqual(self.dialog.library.linked_track('a')['id'],'a'*22)
        play.assert_called_once_with();choose.assert_not_called()

    def test_auto_result_does_not_hijack_selected_song_or_manual_job(self):
        self.dialog.auto_links=CatalogLinkQueue(self.directory,self.songs,self.dialog.library,self.people)
        self.dialog.choose_song(self.songs[1]);self.dialog.busy=True
        self.dialog.receive_auto_links(('auto_links',self.songs[0],[fixtures.track()],threading.Event()))
        self.assertEqual(self.dialog.selected_song['id'],'b')
        self.assertTrue(self.dialog.busy)
        self.assertEqual(self.dialog.library.linked_track('a')['id'],'a'*22)

    def test_cancelled_search_does_not_write_or_mark_title_checked(self):
        self.dialog.auto_links=CatalogLinkQueue(self.directory,self.songs,self.dialog.library,self.people)
        cancel=threading.Event();cancel.set()
        self.dialog.receive_auto_links(('auto_links',self.songs[0],[fixtures.track()],cancel))
        self.assertIsNone(self.dialog.library.linked_track('a'))
        self.assertEqual(self.dialog.auto_links.checked,{})

    def test_automatic_link_preserves_selected_duplicate_recording(self):
        original={**fixtures.song('a'),'duration':240}
        other={**original,'id':'other','album':'Other Album'}
        self.dialog.catalog.songs=[original,other]
        self.dialog.hide_duplicates.set(True);self.dialog.filter_songs();self.dialog.choose_song(original)
        self.dialog.auto_links=CatalogLinkQueue(self.directory,[original,other],self.dialog.library,self.people)
        self.dialog.receive_auto_links(('auto_links',other,[{**fixtures.track(),'album':'Other Album'}],threading.Event()))
        self.assertEqual(self.dialog.selected_song['id'],'a')
        self.assertIsNone(self.dialog.library.linked_track('a'))
        self.assertIsNotNone(self.dialog.library.linked_track('other'))

    def test_rate_limit_waits_without_marking_a_song_missing(self):
        from spotify_catalog import SpotifyCatalogError
        self.dialog.auto_links=CatalogLinkQueue(self.directory,self.songs,self.dialog.library,self.people)
        with patch('explorer_dialog.time.monotonic',return_value=100):
            self.dialog.receive_auto_links(('auto_link_error',SpotifyCatalogError('limited','rate_limited'),threading.Event()))
        self.assertEqual(self.dialog.auto_next,400)
        self.assertEqual(self.dialog.auto_links.checked,{})


if __name__=='__main__':unittest.main()
