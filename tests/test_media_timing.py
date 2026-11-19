import unittest

from ravora.media_timing import capture_tick, indexed_video_timestamps, media_timing_replay, reply_onset_gaps


class MediaTimingTests(unittest.TestCase):
    def test_indexed_clock_preserves_missing_frame_and_absolute_origin(self):
        result = media_timing_replay()
        self.assertEqual(result["source_ticks"], [0, 1, 2, 4, 5])
        self.assertEqual(result["missing_source_ticks"], [3])
        self.assertIn(r"gte(N\,3)", result["video_timestamp_filter"])
        self.assertEqual(capture_tick(1400, 1000), 1)
        self.assertEqual(capture_tick(1399, 1000), 0)
        self.assertEqual(result["external_api_calls"], 0)

    def test_reordered_or_duplicate_frames_cannot_enter_mux(self):
        for ticks in ([], [1, 0], [0, 1, 1], [-1, 0], [False, 1]):
            with self.subTest(ticks=ticks), self.assertRaises(ValueError):
                indexed_video_timestamps(ticks, 30)


class ReplyCadenceTests(unittest.TestCase):
    def test_counts_a_hold_that_begins_before_the_onset_window(self):
        frames = [{'tick': tick, 'raf_ms': tick*1000/30} for tick in (0, 24, 30, *range(31, 61), 70)]
        # Include the leading interval that crosses the left edge, and exclude
        # a later hold that begins exactly at the right edge.
        turns = [{'input_timing': {'reply_signal_context_time': 11.0}}]
        result = reply_onset_gaps(frames, turns, 10.0, 30)
        self.assertEqual(result, [{'turn': 1, 'max_tick_gap': 6, 'max_raf_gap_ms': 200.0}])

    def test_second_reply_is_measured_on_its_own_audio_clock(self):
        ticks = [i for i in range(181) if i not in (112, 113, 114)]
        frames = [{'tick': i, 'raf_ms': i*1000/30} for i in ticks]
        turns = [{'input_timing': {'reply_signal_context_time': t}} for t in (3.0, 5.6)]
        result = reply_onset_gaps(frames, turns, 2.0, 30)
        self.assertEqual([r['max_tick_gap'] for r in result], [1, 4])
        self.assertAlmostEqual(result[1]['max_raf_gap_ms'], 133.3)

    def test_missing_coverage_cannot_pass(self):
        frames = [{'tick': 0, 'raf_ms': 0}, {'tick': 4, 'raf_ms': 134}]
        self.assertEqual(reply_onset_gaps(frames, [{}], 0, 30), [])
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_CAPTURE'):
            reply_onset_gaps(frames, [{'input_timing': {'reply_signal_context_time': 2}}], 0, 30)
