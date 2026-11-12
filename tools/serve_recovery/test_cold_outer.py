"""Real native CPU role lifecycle with explicit fake cold issuance.

These tests cover per-generation orchestration and stop races, not BPF/token
enforcement. The unchanged CPU role fixture strips cold credentials before
native Guardian dispatch; dedicated issuer/kernel tests cover those boundaries.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import cold_admission, cold_client, control, device_control
from tools.serve_recovery import ledger, outer_control, outer_tree, store
from tools.serve_recovery import test_outer_runtime as fixtures, test_runtime


def worker(path):
    args = json.loads(path.read_text()); state = Path(args['state']); mode = args['cold_scenario']
    class FakeIssuer:
        def __init__(self): self.revoked = False
        def available(self):
            (state/'cold-status-checked').touch()
            return mode != 'unavailable'
        def check(self):
            ledger.require(not self.revoked, 'injected revoked cold host')
        def request(self, attempt, lease, instance, *, tick):
            tick()
            row = dict(ticket=attempt['ticket'], serial=attempt['serial'], custody=attempt['custody'],
                original_intent=attempt, core=json.loads((state/store.STATE).read_text())['state'])
            with (state/'cpu-cold-requests.jsonl').open('a') as out: out.write(json.dumps(row)+'\n')
            if mode == 'request-failure': raise TimeoutError('injected absent issuer reply')
            if mode == 'stop-during-request':
                with store.Store(state, args['service_id']) as core:
                    core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                        operation='desired', args=dict(state='STOPPED')))
            if mode == 'host-revoked': self.revoked = True
            return device_control.Reply(dict(cpu_fixture=True), [])
    original = cold_admission.configuration
    def fake_configuration(service_id, digest, lease, *, descriptors=None):
        if descriptors is not None:
            ledger.require(descriptors == [], 'CPU fixture never creates kernel credentials')
            return None, ()
        return original(service_id, digest, lease)
    with ExitStack() as patches:
        patches.enter_context(mock.patch.object(cold_client, 'inherited', side_effect=lambda *_a: FakeIssuer()))
        patches.enter_context(mock.patch.object(cold_admission, 'configuration', fake_configuration))
        fixtures.worker(path)


class RuntimeTests(fixtures.RuntimeTests):
    def start_cold(self, scenario):
        config = self.state/'worker.json'
        config.write_text(json.dumps(dict(state=str(self.state), package=str(self.package), digest=self.digest,
            base=self.fixture.base, mode='simple-gateway', activate=True,
            service_id=self.sid, cold_scenario=scenario)))
        self.tree = outer_tree.Tree().__enter__()
        self.tree.launch([sys.executable, '-I', '-B', __file__, '--worker', str(config)], env=dict(os.environ))
        self.process = self.tree.root

    def requests(self):
        path = self.state/'cpu-cold-requests.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stop_outer(self):
        outer_control.request(self.sid, 'stop', timeout=2); self.finish()

    def test_unavailable_issuer_waits_without_spending_outer_attempts(self):
        self.start_cold('unavailable')
        self.wait(lambda: (self.state/'cold-status-checked').exists())
        self.assertEqual(self.policy()['total'], 0); self.assertEqual(self.requests(), [])
        self.assertFalse(any(self.core()['generations'].values()))
        self.stop_outer()

    def test_stop_after_credential_request_prevents_native_root_dispatch(self):
        self.start_cold('stop-during-request'); self.finish()
        self.assertEqual(self.policy()['total'], 1); self.assertEqual(len(self.requests()), 1)
        self.assertFalse(any(self.core()['generations'].values()))

    def test_lost_issuer_reply_retires_intent_without_refund_or_role_launch(self):
        self.start_cold('request-failure')
        self.wait(lambda: self.policy()['total'] == 1 and self.policy()['pending'] is None)
        self.assertEqual(len(self.requests()), 1); self.assertFalse(any(self.core()['generations'].values()))
        self.stop_outer(); self.assertEqual(self.policy()['total'], 1)

    def test_host_revoked_after_issuance_cannot_fork_or_change_running_intent(self):
        self.start_cold('host-revoked'); self.assertNotEqual(self.process.wait(timeout=6), 0)
        self.assertEqual(self.policy()['total'], 1); self.assertIsNone(self.policy()['pending'])
        self.assertEqual(self.core()['desired'], 'RUNNING')
        self.assertFalse(any(self.core()['generations'].values()))

    def test_healthy_native_core_does_not_request_more_cold_credentials(self):
        self.start_cold('normal'); first = self.ready(); count = len(self.requests())
        later = self.wait(lambda: (v if (v := control.request(self.sid, 'status'))
            ['controller_events'] > first['controller_events']+2 else None))
        self.assertEqual(count, 1); self.assertEqual(len(self.requests()), 1)
        for role in ('guardian', 'controller', 'gateway', 'model'):
            self.assertEqual(first[role+'_pid'], later[role+'_pid'])
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed']); self.finish()

    def test_actual_native_root_exit_requires_a_new_persisted_attempt_after_cleanup(self):
        self.start_cold('normal'); first = self.ready()
        test_runtime.signal_identity(first['guardian_pid'], first['guardian_start_ticks'], signal.SIGKILL)
        second = self.ready(generation=first['model_generation']+1, seconds=39)
        requests = self.requests(); self.assertEqual(len(requests), 2)
        self.assertEqual([row['serial'] for row in requests], [1, 2])
        self.assertNotEqual(requests[0]['ticket'], requests[1]['ticket'])
        self.assertEqual(requests[0]['custody']['instance_token'], requests[1]['custody']['instance_token'])
        self.assertNotEqual(requests[0]['custody']['tree_scope'], requests[1]['custody']['tree_scope'])
        self.assertTrue(all(not any(row['core']['active'].values()) and
            ledger.digest(row['core']) == row['original_intent']['core_sha256'] for row in requests))
        self.assertNotEqual(first['guardian_pid'], second['guardian_pid'])
        self.assertGreaterEqual(self.policy()['starts'][1]['at']-self.policy()['starts'][0]['at'],
                                30*ledger.SECOND)
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed']); self.finish()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker': worker(Path(sys.argv[2]))
    else: unittest.main(module=None)
