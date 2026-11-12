"""Root host transfer with real protected copies and CPU package deployment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, deployment, device_domain
from tools.serve_recovery import host_handoff as handoff, host_install as host
from tools.serve_recovery import host_systemd as adapter, store
from tools.serve_recovery import test_host_install as fixtures
from tools.serve_recovery import test_host_systemd as systemd_fixtures


def deploy_fixture(path, reverse=False):
    value = json.loads((path/'fixture.json').read_text())
    base = json.loads((path/'cpu-engine.json').read_text())
    with mock.patch.object(bundle.engine, 'verify', return_value=base):
        if not reverse:
            package = Path(value['package']); target = package.parent/'second-package'
            shutil.copytree(package, target)
            name = 'source/tools/serve_recovery/host_entry.py'; script = target/name
            script.chmod(0o600); script.write_text(script.read_text()+'\n# CPU second protected generation\n')
            script.chmod(0o400)
            manifest = json.loads((target/'manifest.json').read_text())
            manifest['files'][name] = bundle.engine.sha(script)
            bundle.engine.save(target/'manifest.json', manifest)
            digest = bundle.engine.sha(target/'manifest.json'); launcher = target/'bin/q4t'
            launcher.chmod(0o600); launcher.write_bytes(bundle.launcher(digest, manifest['python']['path']))
            launcher.chmod(0o500)
            selected = dict(value, package=str(target), digest=digest)
            deployment.switch(Path(value['state']), target, digest, expected=value['digest'],
                              request_id=str(uuid.uuid4()))
            (path/'second.json').write_text(json.dumps(selected))
        else:
            selected = json.loads((path/'second.json').read_text())
            deployment.switch(Path(value['state']), value['package'], value['digest'],
                expected=selected['digest'], request_id=str(uuid.uuid4()), rollback=True)
            selected = value
    print(json.dumps(selected), flush=True)


class HandoffTests(fixtures.HostTests):
    def setUp(self):
        super().setUp(); self.initialize_host(); host.configure(self.host, enabled=True)
        self.target = self.parent/'next-host'; self.request = str(uuid.uuid4())
        self.prepare_target(self.fixture)

    def prepare_target(self, value):
        return host.initialize(self.target, value['package'], value['digest'],
            value['state'], value['service_id'], self.uid, self.gid, self.directory)

    def transfer(self): return handoff.transfer(self.host, self.target, self.request)
    def record(self): return device_domain.read_record(handoff.record_path(self.target, self.request))

    def invoke(self, directory, *, trigger=False):
        value = host.load(directory)
        arguments = [value['artifact']['python']['path'], '-I', '-B',
            str(directory/'source/tools/serve_recovery/host_entry.py'),
            '--ensure', '--directory', str(directory)]
        if trigger: arguments.append('--systemd-trigger')
        result = subprocess.run(arguments, env=host.environment(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def fake_manager(self):
        roots = {s:self.parent/('units-'+s) for s in ('runtime', 'persistent')}
        for path in roots.values(): path.mkdir(mode=0o755)
        manager = systemd_fixtures.Manager(roots)
        for patch in (mock.patch.object(adapter, 'DIRECTORIES', roots),
                      mock.patch.object(adapter, 'manager', side_effect=manager)):
            patch.start(); self.addCleanup(patch.stop)
        adapter.install(self.host, 'runtime'); adapter.configure(self.host, enabled=True)
        adapter.install(self.target, 'runtime')
        return manager

    def test_native_transfer_replay_and_reverse_preserve_original_core_and_budgets(self):
        before = self.state_files(); old = host.load(self.host)['artifact']
        result = self.transfer(); self.assertTrue(result['complete'])
        self.assertFalse(host.load(self.host)['enabled']); self.assertTrue(host.load(self.target)['enabled'])
        self.assertTrue(self.transfer()['replayed'])
        self.assertEqual(self.invoke(self.host)['reason'], 'host_entry_retired')
        self.assertEqual(self.invoke(self.target)['reason'], 'operator_stopped')
        with self.assertRaisesRegex(ValueError, 'retired'): host.configure(self.host, enabled=True)
        handoff.transfer(self.target, self.host, str(uuid.uuid4()))
        self.assertEqual(self.invoke(self.target)['reason'], 'host_entry_retired')
        self.assertEqual(self.invoke(self.host)['reason'], 'operator_stopped')
        self.assertEqual(host.load(self.host)['artifact'], old)
        self.assertEqual(before, self.state_files())

    def test_optional_trigger_moves_without_core_mutation_and_old_trigger_cannot_resume(self):
        manager = self.fake_manager(); before = self.state_files(); self.transfer()
        old_timer = adapter.names(host.load(self.host))[1]; new_timer = adapter.names(host.load(self.target))[1]
        self.assertFalse(manager.active[old_timer]); self.assertTrue(manager.active[new_timer])
        self.assertEqual(self.invoke(self.host, trigger=True)['reason'], 'host_entry_retired')
        with self.assertRaisesRegex(ValueError, 'retired'): adapter.configure(self.host, enabled=True)
        self.assertEqual(before, self.state_files())

    def test_initial_private_partial_write_resumes_without_any_published_gate(self):
        original = store.Store.write_all; once = False
        def partial(fd, raw):
            nonlocal once
            if not once and handoff.FORMAT.encode() in raw:
                once = True; os.write(fd, raw[:17]); raise OSError('injected handoff first write')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=partial), self.assertRaises(OSError):
            self.transfer()
        self.assertFalse((self.host/'admin'/handoff.MARKER).exists())
        self.assertTrue(host.load(self.host)['enabled']); self.assertFalse(host.load(self.target)['enabled'])
        self.assertTrue(self.transfer()['complete'])

    def test_partial_first_marker_blocks_entry_and_resumes_same_transaction(self):
        original = device_domain.write_record; once = False
        def partial(path, value):
            nonlocal once
            if Path(path) == self.host/'admin'/handoff.MARKER and not once:
                once = True; raise OSError('injected first marker publication')
            return original(path, value)
        with mock.patch.object(device_domain, 'write_record', side_effect=partial), self.assertRaises(OSError):
            self.transfer()
        before = self.record()
        with self.assertRaisesRegex(ValueError, 'publication needs resume'): host.configure(self.host, enabled=True)
        self.assertTrue(self.transfer()['complete'])
        self.assertEqual(handoff.identity(self.record()), handoff.identity(before))

    def test_source_disable_receipt_loss_keeps_both_entries_gated_until_resume(self):
        original = host.configure_locked
        def partial(directory, value, **options):
            result = original(directory, value, **options)
            if directory == self.host: raise OSError('injected source disable receipt')
            return result
        before = self.state_files()
        with mock.patch.object(host, 'configure_locked', side_effect=partial), self.assertRaises(OSError):
            self.transfer()
        for directory in (self.host, self.target):
            self.assertEqual(self.invoke(directory)['reason'], 'host_handoff_pending')
            with self.assertRaisesRegex(ValueError, 'handoff'): host.remove(directory)
        self.assertTrue(self.transfer()['complete']); self.assertEqual(before, self.state_files())

    def test_target_enable_before_final_receipt_cannot_resume_business(self):
        original = device_domain.write_record
        def partial(path, value):
            if Path(path) == handoff.record_path(self.target, self.request) and value['phase'] == 'complete':
                raise OSError('injected final handoff commit')
            return original(path, value)
        before = self.state_files()
        with mock.patch.object(device_domain, 'write_record', side_effect=partial), self.assertRaises(OSError):
            self.transfer()
        self.assertTrue(host.load(self.target)['enabled'])
        self.assertEqual(self.invoke(self.target)['reason'], 'host_handoff_pending')
        self.assertTrue(self.transfer()['complete']); self.assertEqual(before, self.state_files())

    def test_lost_published_history_or_marker_never_bootstraps_or_reenables_old_entry(self):
        self.transfer()
        paths = [handoff.record_path(self.target, self.request), self.host/'admin'/handoff.MARKER,
                 self.target/'admin'/handoff.MARKER, self.host/'admin'/handoff.BOUND]
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes(); path.unlink()
                with self.assertRaises((OSError, ValueError)): host.configure(self.host, enabled=True)
                with self.assertRaises((OSError, ValueError)):
                    handoff.transfer(self.host, self.target, str(uuid.uuid4()))
                path.write_bytes(raw); path.chmod(0o600)

    def test_disabled_native_admission_is_preserved(self):
        host.configure(self.host, enabled=False); before = self.state_files()
        self.assertFalse(self.transfer()['native_enabled'])
        self.assertFalse(host.load(self.target)['enabled'])
        self.assertEqual(self.invoke(self.target)['reason'], 'host_disabled')
        self.assertEqual(before, self.state_files())

    def test_corrupt_permanent_binding_cannot_authorize_active_target(self):
        self.transfer(); before = self.state_files()
        path = self.target/'admin'/handoff.BOUND; path.write_bytes(b'{}')
        with self.assertRaises(ValueError): host.configure(self.target, enabled=True)
        self.assertEqual(host.status(self.target)['handoff_admission'], 'UNKNOWN')
        self.assertEqual(before, self.state_files())

    def test_manager_failure_keeps_original_transaction_and_healthy_core_untouched(self):
        manager = self.fake_manager(); original = adapter.configure_locked
        def partial(directory, installed, value, **options):
            if directory == self.host: manager.fail = True
            return original(directory, installed, value, **options)
        before = self.state_files()
        with mock.patch.object(adapter, 'configure_locked', side_effect=partial), self.assertRaises(OSError):
            self.transfer()
        self.assertEqual(self.invoke(self.target)['reason'], 'host_handoff_pending')
        self.assertEqual(before, self.state_files()); manager.fail = False
        self.assertTrue(self.transfer()['complete'])
        self.assertEqual(before, self.state_files())

    def test_existing_trigger_cannot_be_silently_dropped_by_native_transfer(self):
        self.fake_manager(); adapter.remove(self.target)
        with self.assertRaisesRegex(ValueError, 'admission intent'): self.transfer()
        self.assertTrue(host.load(self.host)['enabled'])
        self.assertFalse(handoff.record_path(self.target, self.request).exists())

    def test_changed_runtime_or_resource_binding_is_rejected_before_any_gate(self):
        original = host.load(self.target)
        value = dict(original, state=original['state']+'-other')
        host.commit(self.target, value)
        with self.assertRaisesRegex(ValueError, 'identity differs'): self.transfer()
        self.assertFalse(handoff.record_path(self.target, self.request).exists())
        self.assertTrue(host.load(self.host)['enabled'])

    def test_real_cpu_package_deployment_and_rollback_preserve_both_source_generations(self):
        # Target was prepared with the original package; keep it as an unused
        # root snapshot and select a different fresh directory for the upgrade.
        self.target = self.parent/'upgraded-host'
        before = self.state_files(); old = host.load(self.host)['artifact']
        def deploy(reverse=False):
            result = subprocess.run([sys.executable, '-I', '-B', __file__, '--deploy', str(self.path),
                'rollback' if reverse else 'forward'], user=self.uid, group=self.gid, extra_groups=[],
                env=host.environment(), capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr); return json.loads(result.stdout)
        new = deploy(); self.prepare_target(new); self.transfer()
        self.assertNotEqual(old['manifest_sha256'], host.load(self.target)['artifact']['manifest_sha256'])
        self.assertEqual(host.load(self.host)['artifact'], old)
        deploy(reverse=True); handoff.transfer(self.target, self.host, str(uuid.uuid4()))
        self.assertEqual(self.invoke(self.host)['reason'], 'operator_stopped')
        after = self.state_files()
        for name in ('ledger.json', 'outer-recovery/policy.json',
                     'client-access/policy.json', 'client-limits/policy.json', 'client-usage/policy.json'):
            self.assertIn(name, before); self.assertEqual(before[name], after[name])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--deploy':
        deploy_fixture(Path(sys.argv[2]), reverse=sys.argv[3] == 'rollback')
    else: unittest.main(module=None)
