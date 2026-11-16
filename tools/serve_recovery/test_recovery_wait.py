"""Actual native Guardian against synthetic CPU journals; no model/GPU proof."""
import copy
import json
import os
from pathlib import Path
import signal
import unittest
import uuid

from tools.serve_host import native
from tools.serve_recovery import journal, ledger, test_runtime


def exhausted(role, *, cooldown=False):
    def seed(writer):
        def call(operation, **args):
            return writer.transact(dict(id=str(uuid.uuid4()),
                expected_revision=writer.state['revision'], operation=operation,
                args=args))['result']
        proof = {key: True for key in ledger.PROOFS}
        call('desired', state='RUNNING')
        controller = None
        if role == 'model':
            controller = call('authorize', role='controller', proof=proof)['grant']
            call('spawned', role='controller', ticket=controller['ticket'],
                 owner=dict(pid=os.getpid(), start_ticks=journal.process_start(os.getpid()),
                            boot_id=writer.state['clock']['boot_id']))
        # These are synthetic historical grants, not process launch evidence.
        for _ in range(4):
            grant = call('authorize', role=role, proof=proof)['grant']
            call('retired', role=role, ticket=grant['ticket'], verified=True)
        if cooldown:
            denied = call('authorize', role=role, proof=proof)
            if denied['allowed'] or denied['reason'] != 'budget_exhausted':
                raise AssertionError('CPU fixture failed to exhaust original budget')
        if controller is not None:
            call('retired', role='controller', ticket=controller['ticket'], verified=True)
        call('desired', state='STOPPED')
    return seed


class RuntimeWaitTests(unittest.TestCase):
    setUpClass = classmethod(test_runtime.RuntimeTests.setUpClass.__func__)
    tearDownClass = classmethod(test_runtime.RuntimeTests.tearDownClass.__func__)
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    launch = test_runtime.RuntimeTests.launch

    def assert_wait(self, role, *, cooldown=False):
        service = self.launch(models=role == 'model', gateway=role != 'controller',
                              recovery_fixture=exhausted(role, cooldown=cooldown))
        result = service.wait(lambda s: s['state'] == 'CIRCUIT_OPEN')
        wait = result['recovery_wait']
        self.assertEqual(wait['role'], role)
        reason = 'cooldown' if cooldown else 'budget_exhausted'
        self.assertEqual(wait['reason'], reason)
        self.assertEqual(result['reason'], role+'_'+reason)
        self.assertEqual(result['retry_after_ms'], wait['retry_after_ms'])
        self.assertGreater(wait['retry_after_ms'], 600000)
        self.assertFalse(result['business_ready'])
        self.assertIsNone(result['model_pid'])
        before = copy.deepcopy(service.persistent())
        later = service.wait(lambda s: s['retry_after_ms'] < wait['retry_after_ms']-100)
        self.assertEqual(later['recovery_wait']['role'], role)
        after = service.persistent()
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(after['generations'], before['generations'])
        self.assertEqual(after['generations'][role], 4)
        stopped = service.stop()
        self.assertTrue(stopped['cleanup_confirmed'])
        self.assertIsNone(stopped['recovery_wait'])
        self.assertEqual(stopped['retry_after_ms'], 0)
        self.assertEqual(service.persistent()['attempts'], before['attempts'])
        service.assert_lease_free()

    def test_gateway_exhaustion_is_visible_before_controller_launch(self):
        self.assert_wait('gateway')

    def test_controller_exhaustion_keeps_original_budget(self):
        self.assert_wait('controller')

    def test_model_exhaustion_is_visible_with_live_controller(self):
        self.assert_wait('model')

    def test_restarted_guardian_reports_persisted_cooldown(self):
        self.assert_wait('gateway', cooldown=True)

    def test_gateway_backoff_recovers_without_restarting_healthy_model(self):
        service = self.launch(models=True, gateway=True)
        first = service.wait(lambda s: s['business_ready'])
        self.assertIsNone(first['recovery_wait'])
        test_runtime.signal_identity(first['gateway_pid'], first['gateway_start_ticks'],
                                     signal.SIGKILL)
        waiting = service.wait(lambda s: s.get('recovery_wait') is not None)
        self.assertEqual(waiting['recovery_wait']['role'], 'gateway')
        self.assertEqual(waiting['recovery_wait']['reason'], 'backoff')
        self.assertEqual(waiting['state'], 'RECOVERING')
        ready = service.wait(lambda s: s['business_ready'] and s['gateway_generation'] == 2)
        self.assertIsNone(ready['recovery_wait'])
        self.assertEqual(ready['retry_after_ms'], 0)
        for role in ('model', 'controller'):
            self.assertEqual(first[role+'_pid'], ready[role+'_pid'])
        self.assertTrue(service.stop()['cleanup_confirmed'])


class ProjectionTests(unittest.TestCase):
    def snapshot(self):
        path = test_runtime.ROOT/'docs/evidence/serve-progress-business-20261109/business-02/execution/ready-2.json'
        value = json.loads(path.read_text())
        value.update(state='CIRCUIT_OPEN', business_ready=False,
            recovery_wait=dict(role='gateway', reason='budget_exhausted', retry_after_ms=900000))
        return value

    def test_typed_wait_exports_and_becomes_unknown_when_stale(self):
        snapshot = self.snapshot(); projected = native.project_status(snapshot)
        self.assertEqual(projected['recovery_wait'], snapshot['recovery_wait'])
        value = native.empty();value.update(projected)
        self.assertIn('q4t_service_recovery_wait_seconds{role="gateway",reason="budget_exhausted"} 900.0',
                      native.metrics(value))
        stale = native.current(value, now=dict(value['sampled'],
            boottime=value['sampled']['boottime']+4))
        self.assertIsNone(stale['recovery_wait'])

    def test_bad_or_legacy_auxiliary_diagnostic_cannot_override_health(self):
        value = self.snapshot()
        for change in ({'role':'SECRET'}, {'reason':'SECRET'},
                       {'retry_after_ms':True}, {'retry_after_ms':-1},
                       {'retry_after_ms':86400001}, {'extra':'SECRET'}):
            result = native.project_status(dict(value,
                recovery_wait=dict(value['recovery_wait'], **change)))
            self.assertIsNone(result['recovery_wait'])
            self.assertFalse(result['business_ready'])
        value.pop('recovery_wait')
        self.assertIsNone(native.project_status(value)['recovery_wait'])
        for state in ('READY', 'STOPPED', 'DRAINING'):
            value = self.snapshot();value.update(state=state, business_ready=state == 'READY')
            self.assertIsNone(native.project_status(value)['recovery_wait'])


if __name__ == '__main__': unittest.main()
