from pathlib import Path
import unittest
from unittest.mock import patch

try:
    import av
    import numpy as np
    from ravora.live_bgm import LoopingMusic, validate_bgm_db
except ModuleNotFoundError:
    av = None


@unittest.skipUnless(av, 'youtube-live extra required')
class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tracks = {
            Path('A.mp3'): np.array([[1000, -1000], [2000, -2000], [3000, -3000]], dtype='<i2'),
            Path('B.mp3'): np.array([[4000, -4000], [5000, -5000]], dtype='<i2'),
        }

    def player(self, paths, db):
        def blocks(path):
            if path not in self.tracks:
                raise OSError('Missing file')
            yield self.tracks[path][:1]
            yield self.tracks[path][1:]
        self.enterContext(patch('ravora.live_bgm.stereo_blocks', blocks))
        player = LoopingMusic(paths, db)
        self.addCleanup(player.close)
        return player

    def test_two_tracks_wrap_inside_frames_without_dropping_a_sample(self):
        player = self.player(self.tracks, 0)
        one_loop = np.concatenate(list(self.tracks.values()))
        voice = np.full(4, -100, dtype='<i2')
        heard = np.concatenate([player.mix(voice).reshape(-1, 2) for _ in range(4)])
        np.testing.assert_array_equal(heard, np.tile(one_loop, (4, 1))[:16] - 100)
        self.assertEqual(player.status()['track'], 'A')

    def test_fader_mutes_only_music_and_advances_while_silent(self):
        player = self.player(self.tracks, -60)
        voice = np.full(3, 600, dtype='<i2')
        np.testing.assert_array_equal(player.mix(voice), np.full((1, 6), 600))
        player.set_volume(0)
        ramp = player.mix(voice).reshape(-1, 2)
        np.testing.assert_array_equal(ramp, [[600, 600], [3100, -1900], [1600, -400]])
        loud = np.full(3, 32760, dtype='<i2')
        self.assertEqual(player.mix(loud).max(), 32767)
        self.assertEqual(loud.tolist(), [32760]*3)
        for bad in (True, -61, 1, float('nan'), '0'):
            with self.assertRaises(ValueError):
                validate_bgm_db(bad)

    def test_missing_music_skips_to_good_track_or_keeps_voice_alone(self):
        for paths,available in [(['bad.mp3','A.mp3'],True),(['bad.mp3'],False)]:
            with self.subTest(paths=paths):
                player = self.player(paths, 0)
                voice = np.full(5, 1000, dtype='<i2')
                result = player.mix(voice).reshape(-1, 2)
                self.assertEqual(player.status()['available'],available)
                self.assertEqual(player.status()['failed_tracks'],1)
                self.assertEqual(result[-1].tolist(),[3000,-1000] if available else [1000,1000])
