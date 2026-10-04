import unittest
from timing_quality import timing_origins,manual_priority,compare_times,consensus_candidates,recover_legacy_origins
from audio_sync import merge_candidates


class TimingQualityTests(unittest.TestCase):
    def test_legacy_repair_needs_two_matching_records_and_preserves_edits(self):
        current={'times':[10,20,31,40],'offset':2,'fingerprint':'same','source':'직접 기록 / 음원 분석'}
        draft={'times':[12,22,32,42],'fingerprint':'same'}
        before={'times':[10,None,None,None],'fingerprint':'same'}
        updated,count=recover_legacy_origins(current,draft,before)
        self.assertEqual(count,2)
        self.assertEqual(updated['origins'],['manual','audio','manual','audio'])
        self.assertEqual(updated['times'],current['times'])
        self.assertEqual(updated['offset'],2)
        self.assertNotIn('origins',current)
        self.assertEqual(recover_legacy_origins(current,draft,{**before,'fingerprint':'other'}),(current,0))
        self.assertEqual(recover_legacy_origins(updated,draft,before),(updated,0))

    def test_legacy_unknown_and_mixed_records_remain_protected(self):
        for source in ('','직접 기록','직접 기록 / 음원 분석','LRCLIB'):
            self.assertTrue(manual_priority({'times':[10,None],'source':source}))
        self.assertFalse(manual_priority({'times':[10,None],'source':'음원 분석'}))

    def test_repeated_audio_merge_never_turns_machine_results_into_manual(self):
        current={'times':[10,None,None],'offset':0,'source':'음원 분석'}
        updated,added=merge_candidates(current,[10,20,None])
        self.assertEqual(updated['source'],'음원 분석')
        self.assertEqual(timing_origins(updated),['audio','audio',None])
        self.assertFalse(manual_priority(updated))

    def test_manual_anchors_survive_merge_with_numeric_provenance(self):
        current={'times':[8,None,30],'offset':2,'source':'직접 기록'}
        updated,added=merge_candidates(current,[10,20,35])
        self.assertEqual(updated['times'],[8,18,30])
        self.assertEqual(updated['origins'],['manual','audio','manual'])
        self.assertTrue(manual_priority(updated))

    def test_reanalysis_replaces_only_known_audio_rows(self):
        current={'times':[8,20,30,40],'offset':0,'origins':['manual','audio','audio','manual']}
        updated,added=merge_candidates(current,[10,18,None,45],replace_audio=True)
        self.assertEqual(updated['times'],[8,18,None,40])
        self.assertEqual(updated['origins'],['manual','audio',None,'manual'])
        self.assertEqual(current['times'],[8,20,30,40])

    def test_consensus_rejects_singletons_and_disagreement(self):
        runs=[{'times':[10,20,30,None],'duration':100},
              {'times':[10.2,25,None,40],'duration':100},
              {'times':[10.4,29,None,40.3],'duration':100}]
        result=consensus_candidates(runs)
        self.assertEqual(result['times'],[10.2,None,None,40.15])
        self.assertEqual(result['votes'],[3,1,1,2])

    def test_reference_evaluation_does_not_hide_missing_lines(self):
        result=compare_times([10,20,30],[10.2,None,34])
        self.assertEqual((result['compared'],result['total'],result['within_1s']),(2,3,1))
        self.assertEqual(result['max_error'],4)


if __name__=='__main__':unittest.main()
