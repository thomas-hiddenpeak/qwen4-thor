"""CPU-only host recovery preflight: actual locks, no metadata-only restart."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import deployment, host_install as host
from tools.serve_recovery import resource_install as resources, resource_kernel as kernel
from tools.serve_recovery import test_host_install as fixtures
from tools.serve_recovery import test_resource_launch as launch_fixtures


class EnsureTests(fixtures.HostTests):
    def pending_marker(self):
        path = Path(self.fixture['state'])/'ledger.pending'
        path.write_bytes(b'{'); path.chmod(0o600); os.chown(path, self.uid, self.gid)
        return path

    def assert_empty(self):
        with kernel.Scope(resources.load(self.directory)['record']) as scope:
            self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))

    def test_stopped_preflight_does_not_spawn_outer_or_modify_intent(self):
        self.initialize_host(); host.configure(self.host, enabled=True); before = self.state_files()
        result = self.enter(operation='ensure')
        self.assertEqual(result['status'], 'NO_ACTION'); self.assertEqual(result['reason'], 'operator_stopped')
        self.assertFalse(result['resume_needed']); self.assertFalse(result['entry_owned'])
        self.assertEqual(before, self.state_files()); self.assert_empty()

        self.assertFalse((Path(self.fixture['state'])/'outer-runs').exists())

    def test_disabled_host_does_not_read_ambiguous_core_or_start_service(self):
        self.initialize_host(); marker = self.pending_marker()
        result = self.enter(operation='ensure')
        self.assertEqual(result['reason'], 'host_disabled'); self.assertFalse(result['model_actions'])
        self.assertEqual(marker.read_bytes(), b'{'); self.assert_empty()

    def test_actual_existing_entry_lock_prevents_restart_despite_unknown_core(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        process = subprocess.Popen([sys.executable, '-I', '-B', __file__, '--hold', self.fixture['state']],
            user=self.uid, group=self.gid, extra_groups=[], env=host.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        ready, _, _ = select.select([process.stdout], [], [], 3); self.assertTrue(ready)
        self.assertEqual(process.stdout.readline().strip(), b'owned')
        marker = self.pending_marker(); result = self.enter(operation='ensure')
        self.assertTrue(result['entry_owned']); self.assertFalse(result['resume_needed'])
        self.assertEqual(result['reason'], 'entry_already_owned'); self.assertIsNone(process.poll())
        self.assertEqual(marker.read_bytes(), b'{'); self.assert_empty()

    def test_absent_running_entry_is_eligible_without_resetting_or_starting(self):
        self.initialize_host(); host.configure(self.host, enabled=True); self.helper('running')
        before = self.state_files()
        result = host.check_runtime(self.host, host.load(self.host), probe=True)
        self.assertTrue(result['resume_needed']); self.assertFalse(result['entry_owned'])
        self.assertEqual(result['reason'], 'entry_absent'); self.assertFalse(result['model_actions'])
        self.assertEqual(before, self.state_files()); self.assert_empty()

    def test_unknown_journal_without_live_entry_cannot_authorize_automatic_start(self):
        self.initialize_host(); host.configure(self.host, enabled=True); self.helper('running')
        before = self.state_files(); marker = self.pending_marker()
        self.assertIn('verification failed', self.enter(operation='ensure', success=False))
        self.assertEqual(marker.read_bytes(), b'{'); self.assertEqual(before, self.state_files())
        self.assert_empty(); self.assertFalse((Path(self.fixture['state'])/'outer-runs').exists())

    def test_outer_disabled_preserves_original_running_intent_without_new_entry(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        self.helper('running'); self.helper('outer-disabled'); before = self.state_files()
        result = self.enter(operation='ensure')
        self.assertEqual(result['reason'], 'outer_disabled'); self.assertFalse(result['resume_needed'])
        self.assertEqual(before, self.state_files()); self.assert_empty()


class NativeEnsureTests(launch_fixtures.LaunchTests):
    helper = fixtures.HostTests.helper
    initialize_host = fixtures.HostTests.initialize_host
    enter = fixtures.HostTests.enter

    def test_ensure_adopts_live_native_core_after_outer_death_without_model_restart(self):
        self.host = self.parent/'host'; self.fixture = self.helper('prepare-native')
        self.sid = self.fixture['service_id']; self.selected = kernel.policy(512, 768, 128)
        self.initialize(); self.initialize_host(); host.configure(self.host, enabled=True)
        self.helper('live-prepare')
        command = [sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/resource_launch.py'),
            '--directory', str(self.directory), '--', sys.executable, '-I', '-B',
            str(ROOT/'tools/serve_recovery/test_outer_runtime.py'), '--worker',
            str(self.path/'runtime-config.json')]
        self.tree.launch(command, env=host.environment())
        initial = self.helper('live-status'); state = Path(self.fixture['state'])
        policy = json.loads((state/'outer-recovery/policy.json').read_text())['state']['policy']
        self.assertEqual(policy['total'], 1)
        self.tree.root.kill(); self.tree.root.wait(timeout=3)
        result = self.enter(operation='ensure')
        self.assertTrue(result['business_ready']); self.assertTrue(result['startup_confirmed'])
        after = self.helper('live-status')
        for role in ('guardian', 'controller', 'gateway', 'model'):
            self.assertEqual(after[role+'_pid'], initial[role+'_pid'])
        current = json.loads((state/'outer-recovery/policy.json').read_text())['state']['policy']
        self.assertEqual(current, policy)
        self.assertTrue(self.enter(operation='stop')['cleanup_confirmed'])
        self.tree.drain(term_seconds=.2, kill_seconds=2)
        self.assertTrue(resources.remove(self.directory)['removed'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--hold':
        with deployment.claim(Path(sys.argv[2]), name='outer-runtime.lock'):
            print('owned', flush=True); time.sleep(15)
    else: unittest.main(module=None)
