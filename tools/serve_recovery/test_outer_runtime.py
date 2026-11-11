"""Actual bounded outer dispatch with CPU roles, private locks and no GPU."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, control, deployment, ledger, outer_control, outer_manager
from tools.serve_recovery import outer_runtime, outer_store, outer_tree, service, store
from tools.serve_recovery import test_lifecycle, test_native_bundle, test_runtime


def worker(path):
    args = json.loads(path.read_text()); state = Path(args['state']); package = Path(args['package'])
    private_lease = state/'private-runtime-cpu-lease'
    original_launch = outer_tree.Tree.launch; original_lease = service.acquire_lease
    original_transaction = outer_store.Store.transact
    original_tick = outer_runtime.Runtime.tick; original_observation = control.outer_observation
    def launch(tree, command, **options):
        config_path = Path(command[command.index('--config')+1]); config = json.loads(config_path.read_text())
        config.update(usage_handoff=False, quota_enforced=False, version_recovery=None,
            cold_admission=None, lease_path=str(private_lease), term_ms=100, kill_ms=200,
            heartbeat_ms=1000, critical_memory_mib=64,
            controller=[sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/fixture_controller.py'),
                        '--model-fixture', str(state), '--bind-model'],
            gateway=[sys.executable, '-I', '-B', str(ROOT/'tools/serve_recovery/fixture_gateway.py')])
        if args['mode'] == 'simple-gateway':
            config['gateway'] = [sys.executable, '-I', '-B', __file__, '--simple-gateway']
        config_path.write_text(json.dumps(config))
        if args['mode'] == 'fork-failure':
            with mock.patch.object(outer_tree.subprocess, 'Popen', side_effect=OSError('injected fork failure')):
                return original_launch(tree, command, **options)
        return original_launch(tree, command, **options)
    def transact(current, request_id, revision, candidate):
        result = original_transaction(current, request_id, revision, candidate)
        operation = candidate['last']['operation']
        if (args['mode'], operation) in (('charge-receipt', 'request'), ('spawn-receipt', 'spawned')):
            raise OSError('injected durable outer receipt loss')
        return result
    def tick(runtime):
        if args['mode'] == 'loop-error' and (state/'inject').exists():
            raise OSError('injected auxiliary loop error')
        return original_tick(runtime)
    def observation(*a, **kw):
        if args['mode'] == 'observation-error' and (state/'inject').exists():
            raise TimeoutError('injected auxiliary observation timeout')
        return original_observation(*a, **kw)
    with ExitStack() as patches:
        patches.enter_context(mock.patch.object(bundle.engine, 'verify', return_value=args['base']))
        patches.enter_context(mock.patch.object(service, 'acquire_lease', side_effect=lambda _path: original_lease(private_lease)))
        patches.enter_context(mock.patch.object(outer_tree.Tree, 'launch', launch))
        patches.enter_context(mock.patch.object(outer_store.Store, 'transact', transact))
        patches.enter_context(mock.patch.object(outer_runtime.Runtime, 'tick', tick))
        patches.enter_context(mock.patch.object(control, 'outer_observation', observation))
        try:
            value = outer_runtime.run(state, package, args['digest'], activate=args['activate'])
            (state/'worker-result.json').write_text(json.dumps(dict(ok=True, result=value)))
        except BaseException as error:
            (state/'worker-result.json').write_text(json.dumps(dict(ok=False, error=type(error).__name__,
                                                                  message=str(error))))
            raise


class RuntimeTests(unittest.TestCase):
    command = test_lifecycle.LifecycleTests.command

    def test_plain_service_launch_cannot_bypass_unresolved_outer_attempt(self):
        from tools.serve_recovery import outer_instance
        import uuid
        with store.Store(self.state, self.sid) as core:
            core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                               operation='desired', args=dict(state='RUNNING')))
        with outer_instance.Instance(self.state, self.sid) as instance, outer_tree.Tree() as tree:
            binding = outer_manager.ready(self.state, self.desc)
            outer_store.commit_once(binding, 'request', dict(core=self.core(), ticket=str(uuid.uuid4()),
                manifest_sha256=self.digest, cleanup_verified=True, identity_valid=True,
                custody=tree.binding(instance)))
        before = self.core(); policy = self.policy()
        with mock.patch.object(service, 'acquire_lease', side_effect=AssertionError('GPU forbidden')):
            with self.assertRaisesRegex(ValueError, 'unresolved outer attempt'):
                service.launch(self.package, self.digest, self.state)
        self.assertEqual(self.core(), before); self.assertEqual(self.policy(), policy)

    def setUp(self):
        self.fixture = test_native_bundle.BundleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        shutil.copyfile(test_runtime.BINARY, self.fixture.guardian)
        receipt = json.loads(self.fixture.build.read_text())
        receipt['guardian_sha256'] = bundle.engine.sha(self.fixture.guardian)
        self.fixture.save(self.fixture.build, receipt)
        prepared = self.fixture.prepare(); self.package = Path(prepared['package'])
        self.digest = prepared['manifest_sha256']; self.state = self.fixture.directory/'state'
        service.initialize(self.package, self.digest, self.state)
        self.desc = service.descriptor(self.state, self.package, self.digest); self.sid = self.desc['service_id']
        outer_manager.initialize(self.state, self.package, self.digest)
        outer_manager.configure(self.state, self.package, self.digest, enabled=True)
        self.tree = None; self.process = None
        self.addCleanup(self.cleanup)

    def core(self): return json.loads((self.state/store.STATE).read_text())['state']
    def policy(self): return json.loads((self.state/'outer-recovery/policy.json').read_text())['state']['policy']

    def start(self, mode='normal', activate=True):
        config = self.state/'worker.json'
        config.write_text(json.dumps(dict(state=str(self.state), package=str(self.package), digest=self.digest,
            base=self.fixture.base, mode=mode, activate=activate)))
        self.tree = outer_tree.Tree().__enter__()
        self.tree.launch([sys.executable, '-I', '-B', __file__, '--worker', str(config)], env=dict(os.environ))
        self.process = self.tree.root

    def wait(self, predicate, seconds=6):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            try:
                value = predicate()
                if value: return value
            except (OSError, ValueError): pass
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError((self.state/'worker-result.json').read_text())
            time.sleep(.02)
        raise TimeoutError('bounded CPU outer scenario did not reach expected state')

    def ready(self, generation=1, seconds=6):
        def observed():
            value = control.request(self.sid, 'status', timeout=.3)
            return value if value['business_ready'] and value['model_generation'] >= generation else None
        return self.wait(observed, seconds)

    def cleanup(self):
        if self.tree is None: return
        try:
            try: control.request(self.sid, 'stop', timeout=3)
            except (OSError, ValueError): pass
            if self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired: pass
        finally:
            self.tree.drain(term_seconds=.2, kill_seconds=1); self.tree.close()

    def finish(self):
        self.assertEqual(self.process.wait(timeout=6), 0, (self.state/'worker-result.json').read_text()
                         if (self.state/'worker-result.json').exists() else '')
        self.assertEqual(self.core()['desired'], 'STOPPED')
        self.assertFalse(any(self.core()['active'].values())); self.assertIsNone(self.policy()['pending'])
        fd = os.open(self.state/'guardian.lock', os.O_RDWR)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)

    def test_stopped_resume_and_disabled_activation_never_create_roles_or_take_gpu(self):
        before = self.core()
        with mock.patch.object(service, 'acquire_lease', side_effect=AssertionError('GPU forbidden')):
            result = outer_runtime.run(self.state, self.package, self.digest)
            self.assertEqual(result['state'], 'STOPPED'); self.assertTrue(result['cleanup_confirmed'])
            outer_manager.configure(self.state, self.package, self.digest, enabled=False)
            with self.assertRaisesRegex(ValueError, 'activation'):
                outer_runtime.run(self.state, self.package, self.digest, activate=True)
        self.assertEqual(self.core(), before); self.assertEqual(self.policy()['total'], 0)
        self.assertEqual(list((self.state/'runs').iterdir()), [])

    def test_real_dispatch_and_explicit_stop_preserve_outer_charge_and_release_instance(self):
        self.start(); ready = self.ready(); self.assertTrue(ready['outer_supervised'])
        self.assertEqual(self.policy()['total'], 1)
        self.assertEqual(self.policy()['pending']['owner']['pid'], ready['guardian_pid'])
        rc, value = self.command('stop'); self.assertEqual(rc, 0, value)
        self.finish(); self.assertEqual(self.policy()['total'], 1)

    def test_actual_root_exit_reaps_old_tree_then_restarts_after_real_cooldown(self):
        self.start(); first = self.ready(); original = self.core()
        old_start = self.policy()['starts'][0]['at']
        test_runtime.signal_identity(first['guardian_pid'], first['guardian_start_ticks'], signal.SIGKILL)
        second = self.ready(generation=first['model_generation']+1, seconds=39)
        self.assertNotEqual(first['guardian_pid'], second['guardian_pid'])
        self.assertNotEqual(first['model_pid'], second['model_pid'])
        policy = self.policy(); self.assertEqual(policy['total'], 2)
        self.assertGreaterEqual(policy['starts'][1]['at']-old_start, 30*ledger.SECOND)
        self.assertEqual(self.core()['desired'], 'RUNNING')
        self.assertIsNone(self.core()['quarantine'])
        for role in ('model', 'controller', 'gateway'):
            self.assertGreater(self.core()['generations'][role], original['generations'][role])
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed']); self.finish()

    def test_operator_stop_between_roots_prevents_the_next_start(self):
        self.start('simple-gateway'); first = self.ready()
        test_runtime.signal_identity(first['guardian_pid'], first['guardian_start_ticks'], signal.SIGKILL)
        def waiting():
            value = outer_control.request(self.sid, 'status', timeout=.3)
            return value if value.get('reason') == 'cooldown' and value['cleanup_confirmed'] else None
        self.wait(waiting)
        rc, value = self.command('stop'); self.assertEqual(rc, 0, value)
        self.assertTrue(value['stop_durable']); self.finish()
        self.assertEqual(self.policy()['total'], 1)

    def test_stop_waits_for_durable_intent_even_when_tree_is_already_empty(self):
        self.start('simple-gateway'); first = self.ready()
        test_runtime.signal_identity(first['guardian_pid'], first['guardian_start_ticks'], signal.SIGKILL)
        self.wait(lambda: (v if (v:=outer_control.request(self.sid, 'status'))
                  .get('reason') == 'cooldown' and v['cleanup_confirmed'] else None))
        with deployment.claim(self.state, name=store.LOCK, create=False):
            value = outer_control.request(self.sid, 'stop')
            self.assertTrue(value['stop_requested']); self.assertFalse(value['stop_durable'])
            self.assertTrue(value['tree_cleanup_verified']); self.assertFalse(value['cleanup_confirmed'])
            self.assertFalse(value['business_ready']); self.assertIsNone(self.process.poll())
            self.assertEqual(self.core()['desired'], 'RUNNING')
        self.finish(); self.assertEqual(self.policy()['total'], 1)

    def test_lost_spawn_receipt_does_not_signal_or_duplicate_the_healthy_root(self):
        self.start('spawn-receipt'); first = self.ready()
        current = self.wait(lambda: (v if (v:=control.request(self.sid, 'status'))
            ['controller_events'] > first['controller_events']+2 else None))
        self.assertEqual(current['model_pid'], first['model_pid']); self.assertEqual(self.policy()['total'], 1)
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed']); self.finish()

    def test_failed_fork_and_lost_charge_receipt_retire_only_local_unstarted_attempt(self):
        for mode in ('fork-failure', 'charge-receipt'):
            with self.subTest(mode=mode):
                if self.tree is not None:
                    self.tree.drain(term_seconds=.2, kill_seconds=1); self.tree.close(); self.tree = None
                    # A separate explicit service fixture keeps the two initial
                    # charge failures independent without resetting any journal.
                    self.state = self.fixture.directory/('state-'+mode)
                    service.initialize(self.package, self.digest, self.state)
                    self.desc = service.descriptor(self.state, self.package, self.digest)
                    self.sid = self.desc['service_id']
                    outer_manager.initialize(self.state, self.package, self.digest)
                    outer_manager.configure(self.state, self.package, self.digest, enabled=True)
                self.start(mode); self.assertNotEqual(self.process.wait(timeout=5), 0)
                self.assertEqual(self.policy()['total'], 1); self.assertIsNone(self.policy()['pending'])
                self.assertEqual(self.core()['desired'], 'RUNNING')
                self.assertFalse(any(self.core()['generations'].values()))
                self.assertFalse(any(self.core()['active'].values()))

    def test_observation_timeout_only_degrades_then_recovers_same_core(self):
        self.start('observation-error'); first = self.ready(); original = self.core()
        (self.state/'inject').touch()
        self.wait(lambda: control.request(self.sid, 'status')['outer_owner_lost'], seconds=5)
        value = control.request(self.sid, 'status'); self.assertTrue(value['business_ready'])
        self.assertEqual(value['model_pid'], first['model_pid']); self.assertEqual(self.core(), original)
        (self.state/'inject').unlink()
        self.wait(lambda: not control.request(self.sid, 'status')['outer_owner_lost'])
        self.assertEqual(self.policy()['total'], 1)
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed']); self.finish()

    def test_outer_loop_exception_leaves_healthy_guardian_running_without_stop_intent(self):
        self.start('loop-error'); first = self.ready(); before = self.core()
        (self.state/'inject').touch(); self.assertNotEqual(self.process.wait(timeout=5), 0)
        # The actual surviving root is adopted by this test's cleanup subreaper.
        value = control.request(self.sid, 'status')
        self.assertTrue(value['business_ready']); self.assertFalse(value['stop_requested'])
        for key in ('guardian_pid', 'controller_pid', 'gateway_pid', 'model_pid'):
            self.assertEqual(value[key], first[key])
        self.assertEqual(self.core(), before); self.assertEqual(self.policy()['total'], 1)
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed'])


if __name__ == '__main__':
    if sys.argv[1:] == ['--simple-gateway']:
        channel = control.GatewayChannel(); channel.event('ready')
        while True:
            channel.event('heartbeat'); snapshot = control.binding(channel.service_id)
            if snapshot['model_ready'] and snapshot['binding']: channel.bound(snapshot['model_generation'])
            else: channel.unbound()
            time.sleep(.05)
    elif len(sys.argv) == 3 and sys.argv[1] == '--worker': worker(Path(sys.argv[2]))
    else: unittest.main(module=None)
