import unittest

from singing_analysis import analysis_windows, matched_profiles, summarize_windows
from singing_assignment import assign_singers, assignment_features


def linear(feature):
    return {'columns': [feature], 'centers': [0.], 'scales': [1.],
            'weights': [1., 0., 0.], 'lower': -.2, 'upper': .2}


class SingingAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.overlap = {'status': 'overlap', 'min_count': 2, 'count': None, 'score': .8}
        self.mixed = {'sony': {'A': .5, 'B': .5, 'C': -.5, 'D': None}}
        self.stems = [{'sony': {'A': -.4, 'B': .8, 'C': .1, 'D': None}},
                      {'sony': {'A': .8, 'B': -.4, 'C': .1, 'D': None}}]
        self.model = {'presence': linear('sony:mixed'), 'edge': linear('sony:stem'), 'required_backends': ['sony']}

    def test_two_permutations_and_missing_candidate(self):
        evidence = assignment_features(self.mixed, self.stems, self.overlap)
        result = assign_singers(evidence, self.overlap, self.model)
        self.assertEqual(result['assignment']['stems'], ['B', 'A'])
        self.assertEqual(result['singers'], ['B', 'A'])
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['decisions'], {'A': 'present', 'B': 'present', 'C': 'absent', 'D': 'uncertain'})
        self.assertIsNone(result['scores']['D'])

    def test_count_conflict_and_duplicate_stems_do_not_force_assignment(self):
        for overlap in ({**self.overlap, 'status': 'solo'},
                        {**self.overlap, 'features': {'waveform_correlation': .999}}):
            with self.subTest(overlap=overlap):
                evidence = assignment_features(self.mixed, self.stems, overlap)
                result = assign_singers(evidence, overlap, self.model)
                self.assertEqual(result['singers'], [])
                self.assertIsNone(result['estimated_count'])

    def test_joint_pair_verifier_can_resolve_two_uncertain_edge_candidates(self):
        mixed = {'sony': {'A': .1, 'B': .1, 'C': -.5}}
        evidence = assignment_features(mixed, self.stems, self.overlap)
        self.assertEqual(assign_singers(evidence, self.overlap, self.model)['singers'], [])
        model = {**self.model, 'pair': linear('weaker_edge')}
        result = assign_singers(evidence, self.overlap, model)
        self.assertEqual(set(result['singers']), {'A', 'B'})
        self.assertEqual(result['decisions']['C'], 'absent')
        self.assertEqual(result['status'], 'estimated')

    def test_window_clocks_and_sequential_soloists(self):
        windows = analysis_windows(1000, 6700)
        self.assertEqual((windows[0][0], windows[-1][1]), (1000, 6700))
        self.assertTrue(all(1000 <= start <= end <= 6700 for _, _, start, end in windows))
        self.assertTrue(all(a[3] >= b[2] for a, b in zip(windows, windows[1:])))
        results = []
        for name, other in (('A', 'B'), ('B', 'A')):
            results.append({'singers': [name], 'status': 'estimated', 'ranked': [name], 'estimated_count': 1,
                            'decisions': {name: 'present', other: 'absent'}, 'scores': {name: .5, other: -.5},
                            'overlap': {'status': 'solo', 'count': 1, 'min_count': 1}})
        row = summarize_windows(results, ['A', 'B'])
        self.assertEqual(row['overlap']['count'], 1)
        self.assertEqual(row['singers'], [])
        self.assertEqual(row['decisions'], {'A': 'uncertain', 'B': 'uncertain'})

    def test_profile_cohorts_are_matched_by_recording_hash(self):
        profiles = {'sony': {'A': [{'audio_sha256': 'shared'}, {'audio_sha256': 'byol-only'}], 'B': []},
                    'sony-contrastive': {'A': [{'audio_sha256': 'shared'}], 'B': []},
                    'ecapa': {'A': [{'audio_sha256': 'shared'}], 'B': []}}
        self.assertEqual(matched_profiles(profiles)['sony'], {'A': [{'audio_sha256': 'shared'}], 'B': []})
