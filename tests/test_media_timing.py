import unittest

from ravora.media_timing import capture_tick, indexed_video_timestamps, media_timing_replay


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
