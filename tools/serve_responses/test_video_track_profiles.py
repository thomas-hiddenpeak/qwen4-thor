"""Check alternate-track source and receipt boundaries without starting a model."""
import unittest
from unittest import mock

from tools.serve_responses import video_track_profiles as tracks
from tools.serve_service import package


class AlternateTrackProfiles(unittest.TestCase):
    def test_short_rotated_stream_is_bound_to_original_uploaded_bytes(self):
        proof = package.read(tracks.PROOF)
        tracks.verify_source(proof)
        for field, wrong in [('duration_ns', 3000000000), ('width', 96), ('height', 64),
                             ('sha256', '0'*64), ('verified_against_original', False)]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                tracks.verify_source(dict(proof, stored=dict(proof['stored'], **{field:wrong})))

    def test_original_or_non_flag_changes_do_not_qualify(self):
        proof = package.read(tracks.PROOF)
        original = tracks.ROOT/'tests/fixtures/file_video/alternate-tracks.mp4'
        with mock.patch.object(tracks, 'CLIP', original), self.assertRaises(RuntimeError):
            tracks.verify_source(proof)
        for offsets in ([], [0, 1], list(reversed(proof['fixture']['changed_bytes']))):
            with self.subTest(offsets=offsets), self.assertRaises(RuntimeError):
                tracks.verify_source(dict(proof, fixture=dict(proof['fixture'], changed_bytes=offsets)))
