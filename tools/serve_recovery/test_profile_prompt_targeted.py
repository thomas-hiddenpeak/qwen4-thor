"""CPU evidence transport and bounded-work checks; no model is launched."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_bundle, profile_manager, profile_record
from tools.serve_recovery import profile_prompt_targeted as stage
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]


class Targeted(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT / 'build/prompt-profile-tests'))
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_qualification_large_source_roundtrip_does_not_expand_general_metadata(self):
        # I/O-only carrier, not a semantic qualification. The actual evidence
        # validator remains mandatory at the packaging/verification boundary.
        value = dict(format='q4t-profile-business-v18',
            reused_business=dict(format='q4t-prompt-profile-source-reuse-v1',
                                 record='x' * (3 << 20)))
        path = self.directory / 'profile.json'
        profile_record.save(path, value)
        self.assertEqual(profile_record.read(path), value)
        with self.assertRaisesRegex(RuntimeError, 'metadata size limit'): package.read(path)
        self.assertEqual(path.read_bytes(), ledger.canonical(value) + b'\n')

    def test_reader_rejects_oversize_legacy_duplicate_nonfinite_and_symlink(self):
        path = self.directory / 'profile.json'
        for raw in (b' ' * (profile_record.MAX_BYTES + 1),
                    json.dumps(dict(format='q4t-profile-business-v17', data='x' * (2 << 20))).encode(),
                    b'{"a":1,"a":2}', b'{"a":NaN}'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError): profile_record.read(path)
        path.write_text('{}')
        link = self.directory / 'link.json'; link.symlink_to(path)
        with self.assertRaises(RuntimeError): profile_record.read(link)
        self.assertEqual(profile_record.read(path), {})

    def test_packaging_and_migration_use_profile_reader_and_keep_digest_gate(self):
        value = dict(format='q4t-profile-business-v18',
            reused_business=dict(format='q4t-prompt-profile-source-reuse-v1', record='x' * (3 << 20)))
        manifest = dict(files={})
        target = self.directory / 'package'; target.mkdir()
        registry = self.directory / 'registry.json'; package.save(registry, {})
        for name in ('primary', 'light'):
            profile_record.save(self.directory / (name + '.json'), value)
        # Semantic checking is spied on, not replaced in production. This
        # transport test proves the complete parsed bytes reach that checker.
        with mock.patch.object(profile_bundle.profile_contract, 'validate', return_value={}), \
             mock.patch.object(profile_bundle, 'compatible'), \
             mock.patch.object(profile_bundle.profile_evidence, 'validate') as check:
            profile_bundle.prepare(target, {}, {}, registry, None)
            self.assertEqual(check.call_count, 2)
            for call in check.call_args_list: self.assertEqual(call.args[0], value)
        for name in ('primary', 'light'):
            filename = 'profile-' + name + '.json'
            manifest['files'][filename] = package.sha(target / filename)
        result = profile_manager.evidence(target, manifest)
        self.assertEqual(result['primary']['record'], value)
        manifest['files']['profile-light.json'] = '0' * 64
        with self.assertRaises(ValueError): profile_manager.evidence(target, manifest)

    def test_partial_or_expanded_actual_work_cannot_be_accepted(self):
        value = dict(status=stage.SUCCESS, binary_sha256=stage.reuse.REVIEW['candidate_binary'],
            seconds=100, counts={k: v for k, v in stage.LIMITS.items() if k != 'seconds'})
        value['counts']['controls'] = 20
        stage.check_result(value)
        for key in value['counts']:
            changed = copy.deepcopy(value); changed['counts'][key] = stage.LIMITS[key] + 1
            with self.assertRaises(RuntimeError): stage.check_result(changed)
            changed['counts'][key] = False
            with self.assertRaises(RuntimeError): stage.check_result(changed)
        for update in (dict(status='FAILED'), dict(seconds=900), dict(binary_sha256='0' * 64)):
            changed = dict(value, **update)
            with self.assertRaises(RuntimeError): stage.check_result(changed)

    def test_unreviewed_plan_cannot_initialize_lease_or_model_work(self):
        with mock.patch.object(stage, 'check_plan', side_effect=ValueError('review mismatch')), \
             mock.patch.object(stage.encrypted.Stage, '__init__') as initialize, \
             mock.patch.object(stage.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'review mismatch'):
                stage.Stage({})
            initialize.assert_not_called()
            spawn.assert_not_called()


if __name__ == '__main__': unittest.main()
