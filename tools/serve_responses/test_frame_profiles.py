"""Mutate actual wire results to exercise the managed frame qualification."""
from copy import deepcopy
import unittest

from tools.serve_recovery import ledger, profile_live_reuse as reuse, profile_record
from tools.serve_responses import frame_profiles as frames
from tools.serve_service import package


class FrameProfileTests(unittest.TestCase):
    def setUp(self):
        self.first = package.read(frames.PROOF)['sdk']
        self.next = deepcopy(self.first)
        old = self.first['file_video']
        self.next['file_video']['id'] = 'resp_' + '0' * 32
        self.next['restored'] = dict(id=old['id'], parts=deepcopy(old['parts']), peer=404)

    def test_identity_content_and_peer(self):
        frames.check(self.next, self.first)
        for field, value in [('id', self.next['file_video']['id']), ('peer', 200),
                             ('parts', [dict(type='refusal', refusal='blue')])]:
            row = deepcopy(self.next)
            row['restored'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                frames.check(row, self.first)

    def test_observations_stream_and_clip(self):
        for key, field, value in [('chat_video', 'content', 'blue'), ('chat_video', 'refusal', 'green'),
                                  ('chat_untyped_image', 'finish', 'length'),
                                  ('file_video', 'sha256', '0' * 64), ('file_video', 'peer', 200)]:
            row = deepcopy(self.first)
            row[key][field] = value
            with self.subTest(key=key, field=field), self.assertRaises(RuntimeError):
                frames.check(row)
        row = deepcopy(self.first)
        row['previews'][1]['prompt_tokens'] += 1
        with self.assertRaises(RuntimeError):
            frames.check(row)

    def test_record_format_keeps_issued_history(self):
        row = dict(format=reuse.FRAME_RECORD, reused_business=dict(format=reuse.FRAME_FORMAT))
        profile_record.shape(row, profile_record.LEGACY_BYTES + 1)
        row['reused_business']['format'] = reuse.VISUAL_ANSWER_FORMAT
        with self.assertRaises(ledger.LedgerError):
            profile_record.shape(row, profile_record.LEGACY_BYTES + 1)
