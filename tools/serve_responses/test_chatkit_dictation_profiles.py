"""Verify audio discovery and the original canceled job across a profile handoff."""
import copy
import unittest

from tools.serve_recovery import profile_live_reuse as reuse, profile_migration
from tools.serve_responses import chatkit_dictation_profiles as profiles
from tools.serve_service import package


class DictationProfiles(unittest.TestCase):
    def test_discovery_tracks_asr_and_preserves_existing_fields(self):
        for enabled in (False, True):
            old = dict(chatkit=reuse.UPLOAD_CHATKIT, transcriptions=dict(configured=enabled))
            target = dict(old, chatkit=reuse.composer_capability(old))
            self.assertTrue(profile_migration.explicit_extension(old, target))
            self.assertFalse(profile_migration.explicit_extension(target, old))
            for field in ('entity_mentions', 'two_phase_document_upload', 'thread_history'):
                self.assertFalse(profile_migration.explicit_extension(
                    old, dict(target, chatkit=dict(target['chatkit'], **{field: False}))))
            incorrect = dict(target['chatkit'], input_transcribe=dict(operation='input.transcribe',
                                                                     configured=not enabled))
            self.assertFalse(profile_migration.explicit_extension(old, dict(target, chatkit=incorrect)))

    def test_recovery_receipt_checks_original_job_authority_and_both_formats(self):
        proof = package.read(profiles.PROOF)['sdk']
        row = dict(proof, canceled_run=dict(eval_id='eval_'+'a'*32, run_id='evalrun_'+'b'*32,
                                            status='canceled', foreign_status=404, sha256='c'*64))
        profiles.check_result(row)
        profiles.check_result(row, row)
        for group, key, value in [('transcripts', 'webm', 'different'), ('inputs', 'wav', 'd'*64),
                                  ('canceled_run', 'run_id', 'evalrun_'+'d'*32),
                                  ('canceled_run', 'sha256', 'd'*64), ('canceled_run', 'foreign_status', 200),
                                  ('canceled_run', 'status', 'completed')]:
            candidate = copy.deepcopy(row)
            candidate[group][key] = value
            with self.subTest(group=group, key=key), self.assertRaises(RuntimeError):
                profiles.check_result(candidate, row)
        for i in range(len(row['observations'])):
            candidate = copy.deepcopy(row)
            del candidate['observations'][i]
            with self.subTest(index=i), self.assertRaises(RuntimeError):
                profiles.check_result(candidate)
