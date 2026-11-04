"""CPU-only configuration matrix and combined supervision failure contracts."""
import copy
import itertools
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from tools.serve_managed_profile import stage as s
from tools.serve_service import test_service as fixture
from tools.serve_clients.test_packages import policy

pkg, sup, resources = s.pkg, s.sup, s.resources


class Profiles(unittest.TestCase):
    setUp = fixture.Case.setUp
    fixture = fixture.PackageTests.fixture

    def prepared(self, **kwargs):
        binary, release, model = self.fixture()
        clients = self.root / 'clients.json'
        pkg.save(clients, policy())
        return pkg.prepare(binary, release, model, self.root / 'combined',
            operations_port=18997, host_resources=resources.policy(32, 64, 8),
            client_policy=clients, **kwargs)

    def test_all_eight_explicit_profiles_keep_independent_choices(self):
        binary, release, model = self.fixture()
        clients = self.root / 'clients.json'
        pkg.save(clients, policy())
        for host, ops, client in itertools.product((False, True), repeat=3):
            with self.subTest(host=host, operations=ops, client=client):
                name = f'profile-{int(host)}{int(ops)}{int(client)}'
                receipt = pkg.prepare(binary, release, model, self.root / name,
                    host_resources=resources.policy(32, 64, 8) if host else None,
                    operations_port=18997 if ops else 0,
                    client_policy=clients if client else None)
                m = pkg.verify(receipt['package'], receipt['manifest_sha256'])
                self.assertEqual(m['version'], 6 if ops else 5 if host else 4 if client else 3)
                self.assertEqual('host_resources' in m, host)
                self.assertEqual('client_policy' in m, client)
                args = pkg.command(receipt['package'], m)
                self.assertEqual('--operations-port' in args, ops)
                self.assertEqual('--client-policy-file' in args, client)
                self.assertEqual(m['release_declarations'], pkg.DECLARATION_PROFILE)
                self.assertTrue(all('declarations/' + n in m['files']
                                    for n in pkg.DECLARATION_FILES))

    def test_combined_declaration_copy_survives_original_loss(self):
        receipt = self.prepared()
        original = self.root / 'declarations/capabilities.json'
        original.unlink()
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['version'], 6)
        package = Path(receipt['package'])
        (package / 'declarations/configuration.json').chmod(0o600)
        (package / 'declarations/configuration.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'file changed'):
            pkg.verify(package, receipt['manifest_sha256'])

    def test_operations_does_not_skip_host_dependency_binding(self):
        receipt = self.prepared()
        path = Path(receipt['package']) / 'manifest.json'
        manifest = pkg.read(path)
        manifest['resource_dependencies']['busctl']['sha256'] = '0' * 64
        pkg.save(path, manifest)
        with self.assertRaisesRegex(RuntimeError, 'manager changed'):
            pkg.verify(path.parent, pkg.sha(path))

    def test_combined_new_package_cannot_omit_release_declarations(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        del metadata['release_declarations']
        pkg.save(release, metadata)
        with self.assertRaisesRegex(RuntimeError, 'declarations required'):
            pkg.prepare(binary, release, model, self.root / 'rejected',
                operations_port=18997, host_resources=resources.policy(32, 64, 8))
        self.assertFalse((self.root / 'rejected').exists())


class CombinedSupervisor(unittest.TestCase):
    setUp = fixture.Case.setUp

    def supervise(self, failure):
        out = self.root / 'run'
        out.mkdir()
        cfg = pkg.settings(18998, 3, operations_port=18997)
        cfg.update(poll_seconds=.01, stop_seconds=.1, kill_seconds=.2)
        selected = resources.policy(32, 64, 8)
        manifest = dict(version=6, settings=cfg, repository=str(pkg.ROOT),
            protocol_sha256='protocol', files={'q4t': 'binary'}, host_resources=selected)
        seen = []
        class Envelope:
            def __init__(self, *args):
                if failure == 'init':
                    raise RuntimeError('delegation unavailable')
                self.selected, self.latest, self.closed = args[0], {}, False
                self.error = None
            def check(self):
                if seen and failure:
                    self.error = failure
                    raise RuntimeError(failure)
            def close(self):
                self.closed = True
        def observe(pid, port, ops):
            self.assertEqual((port, ops), (18998, 18997))
            seen.append(pid)
            if not failure:
                pkg.save(out / 'stop.request.json', {})
            return dict(process=dict(pid=pid, executable_sha256='binary',
                listener_scope='loopback'), protocol='protocol', ready=True,
                metrics={'q4t_gpu_healthy': 1}, operations_port=ops,
                operations_process=dict(pid=pid, executable_sha256='binary',
                                        listener_scope='loopback'), observability_version=5,
                configuration=dict(sup.expected_configuration(), operations_port=ops))
        command = [sys.executable, '-c', 'import time; time.sleep(30)']
        with patch.object(pkg, 'verify', return_value=manifest), \
             patch.object(sup, 'backend_command', return_value=command), \
             patch.object(pkg, 'command', return_value=command), \
             patch.object(resources, 'Envelope', Envelope):
            code = sup.run(self.root, 'fixture', out, observe=observe,
                read_memory=lambda: dict(total_bytes=8*1024**3, available_bytes=4*1024**3))
        return code, pkg.read(out / 'status.json'), seen

    def test_combined_ready_does_not_mask_host_failure_or_enable_restart(self):
        code, state, seen = self.supervise('host_resource_oom')
        self.assertEqual((code, state['attempts'], state['restarts']), (1, 1, 0))
        self.assertEqual(state['error'], 'host_resource_oom')
        self.assertEqual(len(seen), 1)
        self.assertTrue(state['cleanup_confirmed'])

    def test_missing_delegation_still_rejects_before_operations_probe(self):
        code, state, seen = self.supervise('init')
        self.assertEqual((code, state['attempts'], state['restarts']), (1, 0, 0))
        self.assertFalse(seen)
        self.assertTrue(state['cleanup_confirmed'])

    def test_combined_normal_stop_carries_host_terminal(self):
        code, state, seen = self.supervise(None)
        self.assertEqual((code, state['attempts'], state['restarts']), (0, 1, 0))
        self.assertEqual(len(seen), 1)
        self.assertEqual(state['state'], 'STOPPED')
        self.assertTrue(state['host_resources']['payload_removed'])


class Driver(unittest.TestCase):
    def test_resource_observer_failure_cannot_skip_owned_cleanup(self):
        run = object.__new__(s.Run)
        run.capture = Mock()
        run.capture.close.side_effect = RuntimeError('observer failed')
        with patch.object(s.base.Run, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'observer failed'):
                run.cleanup()
            cleanup.assert_called_once()
        self.assertIsNone(run.capture)

    def test_old_ready_without_fresh_host_sample_is_not_combined_evidence(self):
        # Package selection alone never proves live limits/member inheritance.
        from tools.serve_managed_profile.report import audit_samples
        binding = dict(record=dict(root='/sys/fs/cgroup/fixture', policy=s.POLICY),
                       owners=dict(backend=dict(pid=42)))
        with self.assertRaisesRegex(RuntimeError, 'host samples missing'):
            audit_samples([], binding)


if __name__ == '__main__':
    unittest.main()
