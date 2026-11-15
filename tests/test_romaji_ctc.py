import unittest
from unittest.mock import patch

import numpy as np

import romaji_ctc as ctc


VOCAB = {"<blank>": 0, "a": 1, "i": 2, "k": 3, "ka": 4, "na": 5, "o": 6}


def emissions(frames, peaks):
    logits = np.full((frames, len(VOCAB)), -12, dtype=np.float32)
    logits[:, 0] = 0
    for time, token in peaks:
        logits[time, :] = -12
        logits[time, VOCAB[token]] = 0
    return ctc.Emissions(logits, VOCAB, 0)


class RomajiAlignmentTests(unittest.TestCase):
    def test_composed_and_atomic_tokens_share_a_target(self):
        audio = emissions(60, [(10, "ka"), (13, "na"), (37, "k"), (39, "a"), (43, "na")])
        result = ctc.align_lines(audio, [("k", "a", "n", "a")] * 2)
        self.assertEqual([(row.start_ms, row.end_ms) for row in result], [(200, 280), (740, 880)])
        self.assertFalse(any(row.ambiguous for row in result))

    def test_equal_occurrences_are_ambiguous_until_context_resolves_them(self):
        audio = emissions(200, [(20, "ka"), (24, "i"), (60, "na"),
                                (130, "ka"), (134, "i")])
        target = ("k", "a", "i")
        first = ctc.align_lines(audio, [target])[0]
        self.assertTrue(first.ambiguous)
        result = ctc.align_lines(audio, [target, ("n", "a"), target])
        self.assertEqual([row.start_ms for row in result], [400, 1200, 2600])
        self.assertFalse(any(row.ambiguous for row in result))
        restricted = ctc.align_lines(audio, [target], search_bounds=[(1000, 3000)])[0]
        self.assertEqual(restricted.start_ms, 2600)
        self.assertFalse(restricted.ambiguous)

    def test_long_instrumental_unknown_and_omitted_lines_are_skipped(self):
        audio = emissions(2000, [(400, "ka"), (410, "a"), (1700, "na")])
        result = ctc.align_lines(audio, [("k", "a", "a"), ("i", "i"), ("foo",), ("n", "a")])
        self.assertEqual([row.start_ms if row else None for row in result], [8000, None, None, 34000])
        self.assertEqual(ctc.align_lines(audio, [("a", "n", "a")]), [None])

    def test_repeated_tokens_require_a_blank_but_marked_long_vowel_can_sustain(self):
        sustained = emissions(30, [(8, "o"), (9, "o"), (10, "o")])
        self.assertEqual(ctc.align_lines(sustained, [("o", "o")]), [None])
        self.assertEqual(ctc.align_lines(sustained, [("o:",)])[0].start_ms, 160)
        repeated = emissions(30, [(8, "o"), (11, "o")])
        self.assertEqual(ctc.align_lines(repeated, [("o", "o")])[0].start_ms, 160)

    def test_weak_interior_evidence_is_allowed_only_with_a_supported_onset(self):
        audio = emissions(50, [(10, "a"), (15, "o"), (20, "k")])
        audio.log_probs[13, VOCAB["i"]] = -4
        target = ("a", "i", "o", "k")
        self.assertEqual(ctc.align_lines(audio, [target])[0].start_ms, 200)
        audio.log_probs[10, VOCAB["a"]] = -4
        self.assertEqual(ctc.align_lines(audio, [target]), [None])

    def test_cancel_alignment(self):
        with self.assertRaises(ctc.AlignmentCancelled):
            ctc.align_lines(emissions(10, []), [("o",)], cancelled=lambda: True)

    def test_optional_sung_vowel_preserves_normal_target_and_reports_the_ctc_path(self):
        audio = emissions(30, [(8, "ka"), (15, "na")])
        target = ("k", "a", "a", "n", "a")
        self.assertIsNone(ctc.align_lines(audio, [target])[0])
        diagnostics = []
        row = ctc.align_lines(audio, [target], pronunciation=True, diagnostics=diagnostics)[0]
        self.assertEqual((row.start_ms, row.end_ms), (160, 320))
        self.assertEqual([phone[0] for phone in diagnostics[0]["phone_path_ms"]], ["ka", "na"])

    def test_recovery_fills_only_the_gap_and_keeps_the_original_anchors(self):
        audio = emissions(100, [(10, "o"), (40, "ka"), (45, "na"), (80, "o")])
        phones = [("o",), ("k", "a", "n", "a"), ("o",)]
        bounds = [(0, 2000)] * 3
        anchors = [ctc.LineAlignment(200, 220, 0., 1., False), None,
                   ctc.LineAlignment(1600, 1620, 0., 1., False)]
        result = ctc.recover_gaps(audio, phones, anchors, bounds, relaxed=True)
        self.assertIs(result[0], anchors[0])
        self.assertIs(result[2], anchors[2])
        self.assertTrue(result[1].recovered)
        self.assertEqual(result[1].start_ms, 800)

    def test_uncertain_recovery_requires_an_anchor_and_respects_the_entire_bound(self):
        audio = emissions(100, [(40, "ka"), (45, "na")])
        target = ("k", "a", "n", "a")
        self.assertEqual(ctc.recover_uncertain(audio, [target], [None], [(0, 2000)],
                                              min_coverage=.7), [None])
        anchor = ctc.LineAlignment(200, 220, 0., 1., False)
        result = ctc.recover_uncertain(audio, [(), target, target], [anchor, None, None],
                                       [(0, 2000), (700, 880), (900, 1800)], min_coverage=.7)
        self.assertEqual(result, [anchor, None, None])
        result = ctc.recover_uncertain(audio, [(), target, target], [anchor, None, None],
                                       [(0, 2000), (700, 940), (1000, 1800)], min_coverage=.7)
        self.assertIs(result[0], anchor)
        self.assertEqual(result[1].start_ms, 800)
        self.assertTrue(result[1].recovered)
        self.assertIsNone(result[2])

    def test_onset_refinement_respects_the_neighbor_and_ignores_blank_frames(self):
        audio = emissions(35, [(10, "o"), (11, "o"), (12, "o"), (25, "o")])
        token = VOCAB["o"]
        placement = [ctc.LineAlignment(240, 260, 0., 1., False, first_token=token)]
        self.assertEqual(ctc.refine_onsets(audio, placement, [(0, 700)])[0].start_ms, 200)
        self.assertEqual(ctc.refine_onsets(audio, placement, [(220, 700)])[0].start_ms, 220)


class RomajiInferenceTests(unittest.TestCase):
    def test_overlapping_model_chunks_preserve_the_global_frame_grid(self):
        model = object.__new__(ctc._RomajiASR)
        model.vocabulary = VOCAB
        model.blank_id = 0
        model._fixed_samples = None
        model.provider = "CPUExecutionProvider"

        def fake_infer(audio):
            labels = audio[::ctc.FRAME_HOP][:(len(audio) - ctc.RECEPTIVE_FIELD) // ctc.FRAME_HOP + 1]
            return np.eye(len(VOCAB), dtype=np.float32)[labels.astype(int)][None, :, :]

        model._infer = fake_infer
        labels = np.arange(12) % len(VOCAB)
        audio = np.r_[np.repeat(labels, ctc.FRAME_HOP), np.zeros(ctc.RECEPTIVE_FIELD - ctc.FRAME_HOP)]
        with patch.object(ctc, "_CORE_FRAMES", 4), patch.object(ctc, "_CONTEXT_FRAMES", 2):
            result = model.emissions(audio)
        np.testing.assert_array_equal(result.log_probs.argmax(axis=1), labels)

    def test_gpu_failure_retries_on_cpu(self):
        model = object.__new__(ctc._RomajiASR)
        model.vocabulary = VOCAB
        model.blank_id = 0
        model._fixed_samples = None
        model.provider = "CUDAExecutionProvider"

        def fake_infer(_):
            if model.provider == "CUDAExecutionProvider":
                raise RuntimeError("CUDA failed")
            return np.zeros((1, 1, len(VOCAB)), dtype=np.float32)

        model._infer = fake_infer
        model._open_session = lambda provider: setattr(model, "provider", provider)
        result = model.emissions(np.zeros(ctc.RECEPTIVE_FIELD))
        self.assertEqual(result.log_probs.shape, (1, len(VOCAB)))
        self.assertEqual(model.provider, "CPUExecutionProvider")
        self.assertEqual(model.fallback_reason, "CUDA failed")
