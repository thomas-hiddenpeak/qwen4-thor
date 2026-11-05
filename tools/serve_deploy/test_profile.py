"""CPU-only selection identity and mismatch/cleanup contracts; no listeners."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tools.serve_deploy import profile as p
from tools.serve_service import test_service as fixture


class Combination(unittest.TestCase):
    def setUp(self):
        self.backend = dict(settings=p.pkg.settings(18080, 0, operations_port=18081),
                            client_policy=dict(enabled=True, sha256='a', clients=2))
        self.frontend = dict(upstream_host='127.0.0.1',
            policy=dict(upstream_port=18080, upstream_operations_port=18081,
                        listen_port=18443), client_policy=self.backend['client_policy'],
            tls=dict(trust='explicit_ca'))

    def test_optional_gateway_does_not_force_deployment(self):
        p.combination(self.backend, None)
        p.combination(self.backend, self.frontend)

    def test_mismatched_ports_policy_and_missing_ca_rejected(self):
        changes = [('policy', 'upstream_port', 18082),
                   ('policy', 'upstream_operations_port', 18082),
                   ('policy', 'listen_port', 18080),
                   ('policy', 'listen_port', 18081),
                   ('tls', 'trust', 'not_configured'),
                   ('', 'client_policy', None),
                   ('', 'upstream_host', '192.168.1.2')]
        for parent, key, value in changes:
            with self.subTest(parent=parent, key=key, value=value):
                changed = copy.deepcopy(self.frontend)
                (changed[parent] if parent else changed)[key] = value
                with self.assertRaises(RuntimeError):
                    p.combination(self.backend, changed)

    def test_operations_pair_must_be_explicit_on_both_sides(self):
        self.frontend['policy'].pop('upstream_operations_port')
        with self.assertRaisesRegex(RuntimeError, 'operations'):
            p.combination(self.backend, self.frontend)
        self.backend['settings'].pop('operations_port')
        p.combination(self.backend, self.frontend)


class Selection(unittest.TestCase):
    setUp = fixture.Case.setUp
    fixture = fixture.PackageTests.fixture

    def create(self):
        binary, release, model = self.fixture()
        row = p.pkg.prepare(binary, release, model, self.root / 'backend')
        ref = p.reference(row['package'], row['manifest_sha256'])
        return p.prepare(self.root / 'selection', ref, None, ref, None)

    def test_real_package_check_is_offline_and_never_acceptance(self):
        receipt = self.create()
        result = p.check(receipt['profile'], receipt['sha256'])
        self.assertTrue(result['checked'])
        self.assertTrue(all(result[name] is False for name in p.BOUNDARIES))
        self.assertIsNone(result['summaries']['selected']['gateway'])

    def test_package_mutation_rejected_with_unchanged_profile(self):
        receipt = self.create()
        binary = self.root / 'backend/q4t'
        binary.chmod(0o600)
        binary.write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'file changed'):
            p.check(receipt['profile'], receipt['sha256'])

    def test_profile_mutation_and_claim_upgrade_rejected(self):
        receipt = self.create()
        path = Path(receipt['profile'])
        value = p.pkg.read(path)
        value['boundaries']['business_acceptance'] = True
        p.pkg.save(path, value)
        with self.assertRaisesRegex(RuntimeError, 'profile changed'):
            p.check(path, receipt['sha256'])
        with self.assertRaisesRegex(RuntimeError, 'unknown profile'):
            p.check(path, p.pkg.sha(path))

    def test_recomputed_sha_cannot_hide_false_summary(self):
        receipt = self.create()
        path = Path(receipt['profile'])
        value = p.pkg.read(path)
        value['summaries']['selected']['host_resources'] = {'hard_ram': True}
        p.pkg.save(path, value)
        with self.assertRaisesRegex(RuntimeError, 'summary changed'):
            p.check(path, p.pkg.sha(path))

    def test_source_drift_and_unpinned_rollback_rejected(self):
        receipt = self.create()
        path = Path(receipt['profile'])
        value = p.pkg.read(path)
        value['sources']['tools/serve_deploy/profile.py'] = '0' * 64
        p.pkg.save(path, value)
        with self.assertRaisesRegex(RuntimeError, 'verifier revision'):
            p.check(path, p.pkg.sha(path))
        value['sources'] = p.sources()
        value['rollback']['backend']['manifest_sha256'] = '0' * 64
        p.pkg.save(path, value)
        with self.assertRaisesRegex(RuntimeError, 'manifest changed'):
            p.check(path, p.pkg.sha(path))

    def test_selection_cannot_overwrite_prior_artifacts(self):
        receipt = self.create()
        old = Path(receipt['profile']).read_bytes()
        row = p.pkg.read(receipt['profile'])['selected']['backend']
        with self.assertRaises(FileExistsError):
            p.prepare(self.root / 'selection', row, None, row, None)
        self.assertEqual(old, Path(receipt['profile']).read_bytes())

    def test_symlink_reference_and_non_digest_rejected(self):
        receipt = self.create()
        link = self.root / 'alias'
        link.symlink_to(self.root / 'backend', target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'canonical'):
            p.reference(link, receipt['sha256'])
        with self.assertRaisesRegex(RuntimeError, 'SHA256'):
            p.reference(self.root / 'backend', 'latest')


class Driver(unittest.TestCase):
    def test_cleanup_failure_preserves_environment_abort(self):
        from tools.serve_deploy.stage import failed_status
        self.assertEqual(failed_status('ENV_ABORT', 'cleanup incomplete'), 'ENV_ABORT')
        self.assertEqual(failed_status('FAILED', 'ENV_ABORT: competing process'), 'ENV_ABORT')
        self.assertEqual(failed_status('PASS_BOUNDED_DEPLOYMENT', 'incomplete'), 'FAILED')

    def test_observer_failure_still_runs_parent_cleanup(self):
        from tools.serve_deploy import stage as s
        run = object.__new__(s.Run)
        run.capture = Mock()
        run.capture.close.side_effect = RuntimeError('observation unavailable')
        run.calls = []
        with patch.object(s.base.Run, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'observation unavailable'):
                run.cleanup()
            cleanup.assert_called_once()

    def test_environment_abort_is_not_a_protective_stop(self):
        from tools.serve_service.test_memory_acceptance import TerminalTests
        from tools.serve_deploy.stage import protected_terminal
        value = TerminalTests().fixture()
        value['guard']['status'] = 'ENV_ABORT'
        with self.assertRaises(RuntimeError):
            protected_terminal(value)


if __name__ == '__main__':
    unittest.main()
