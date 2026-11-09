"""Managed limits CPU integration; native policy IPC with synthetic core readiness."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import select
import socket
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_manager, access_store, bundle, controller, gateway
from tools.serve_recovery import ledger, native_ingress, quota_control, quota_manager as manager, quota_policy
from tools.serve_recovery import quota_runtime, service, store
from tools.serve_recovery.test_gateway import snapshot
from tools.serve_recovery import test_native_bundle as bundle_test, test_progress as progress_test
from tools.serve_recovery.test_quota_admin import Server
from tools.serve_service import supervisor


def save(path, value):
    path.write_bytes(ledger.canonical(value));path.chmod(0o600)


class ManagedQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture = bundle_test.BundleTests();self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        for path in (self.fixture.backend, self.fixture.policy):
            value = json.loads(path.read_bytes());value['clients'] = value['clients'][:1];save(path, value)
        receipt = self.fixture.prepare();self.package = Path(receipt['package'])
        self.digest = receipt['manifest_sha256'];self.manifest = bundle.verify(self.package, self.digest)
        self.state = self.fixture.directory/'limits-state'
        service.initialize(self.package, self.digest, self.state)
        self.desc = service.descriptor(self.state, self.package, self.digest)
        self.binding = access_store.binding(self.state, self.desc['service_id'])
        self.initial = manager.base(self.desc)

    def cli(self, operation, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = service.main([operation, '--state', str(self.state), *map(str, arguments)],
                package=self.package, digest=self.digest)
        text = out.getvalue()+err.getvalue()
        for key in ('A'*40, 'B'*40, 'C'*40):self.assertNotIn(key, text)
        return result, json.loads(text)

    def limits(self, clients):
        path = self.fixture.directory/('limits-'+str(uuid.uuid4())+'.json')
        save(path, dict(version=1, clients=clients));return path

    def propose(self, clients):
        path = self.fixture.directory/('proposal-'+str(uuid.uuid4())+'.json')
        result, value = self.cli('limits-propose', '--limits', self.limits(clients), '--proposal', path)
        self.assertEqual(result, 0, value);return path

    def until(self, step, predicate, seconds=4):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            step()
            if predicate():return
            time.sleep(.01)
        self.fail('managed CPU deadline')

    def policies(self):
        result = quota_runtime.Policies(self.binding)
        def close():
            deadline = time.monotonic()+3
            while not result.close() and time.monotonic()<deadline:time.sleep(.01)
            self.assertIsNone(result.reader.process)
        self.addCleanup(close);return result

    def model(self, generation=1):
        env = dict(os.environ, Q4T_SERVICE_ID=self.desc['service_id'], Q4T_MODEL_GENERATION=str(generation),
            Q4T_POLICY_PARENT_PID=str(os.getpid()), Q4T_POLICY_PARENT_START=str(controller.journal.process_start(os.getpid())))
        server = Server(quota_policy.runtime(self.initial, 'a'*64), mode='environment', env=env,
                        address=quota_control.address(self.desc['service_id'], generation))
        self.addCleanup(server.close);return server

    def test_cli_commit_replay_status_capacity_and_separate_key_policy(self):
        before = (self.state/'ledger.json').read_bytes();keys = (self.state/'client-access/policy.json').read_bytes()
        clients = copy.deepcopy(self.initial['clients']);clients[0]['requests_per_minute'] = 1
        path = self.propose(clients)
        result, receipt = self.cli('limits-apply', '--proposal', path, '--wait-seconds', 0)
        self.assertEqual(result, 0);self.assertTrue(receipt['durable']);self.assertFalse(receipt['live_applied'])
        self.assertTrue(self.cli('limits-apply', '--proposal', path, '--wait-seconds', 0)[1]['replayed'])
        request = json.loads(path.read_bytes())['request_id']
        result, status = self.cli('limits-status', '--request-id', request)
        self.assertEqual(result, 0);self.assertEqual(status['durable']['revision'], 1)
        self.assertTrue(status['receipt_known']);self.assertEqual(status['receipt']['id'], request)
        changed = json.loads(path.read_bytes());changed['request_id'] = str(uuid.uuid4());save(path, changed)
        self.assertEqual(self.cli('limits-apply', '--proposal', path, '--wait-seconds', 0)[1]['code'], 'limits_revision_conflict')
        clients += [dict(clients[0], id='beta'), dict(clients[0], id='gamma')]
        result, failed = self.cli('limits-propose', '--limits', self.limits(clients),
            '--proposal', self.fixture.directory/'impossible.json')
        self.assertEqual(result, 1);self.assertEqual(failed['code'], 'limits_capacity_exceeded')
        self.assertEqual((self.state/'ledger.json').read_bytes(), before)
        self.assertEqual((self.state/'client-access/policy.json').read_bytes(), keys)

    def test_new_identity_requires_durable_limits_then_actual_model_installation(self):
        model = self.model();policies = self.policies()
        cfg = bundle.gateway_config_for(self.package, self.manifest, self.fixture.directory, access=self.binding)
        current = snapshot(cfg, token='a'*64, owner=supervisor.process_identity(model.process.pid))
        channel = mock.Mock(service_id=self.desc['service_id'])
        admission = gateway.Admission(cfg, channel);self.addCleanup(admission.close)
        policies.attach(current['model_owner'], 1, 'a'*64)
        keys = self.fixture.directory/'new-keys.json';proposal = self.fixture.directory/'new-key-proposal.json'
        save(keys, dict(version=1, clients=[dict(id='alpha', api_keys=['A'*40]), dict(id='beta', api_keys=['B'*40])]))
        with self.assertRaisesRegex(ValueError, 'membership'):
            access_manager.propose(self.state, self.desc, keys, proposal)
        # Only immutable HTTP/core readiness is synthetic here. Policy socket
        # credentials, exact process birth, journals and publication are real.
        with mock.patch.object(gateway.control, 'binding', return_value=current), \
             mock.patch.object(supervisor, 'probe', return_value={}), \
             mock.patch.object(supervisor, 'classify', return_value='READY'):
            self.until(policies.step, lambda: policies.installed)
            self.until(admission.update, lambda: admission.verified_clients == ('alpha',))
            self.assertIn('port', admission.request('data', 'alpha'))
            address = 'q4t-membership-cpu-'+str(uuid.uuid4())
            native = native_ingress.AdmissionSocket(address, admission.verified_clients,
                admission.request, os.getpid, include_client=True)
            self.addCleanup(native.close)
            def request(client):
                with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                    sock.settimeout(1);sock.connect('\0'+address)
                    sock.send(ledger.canonical(dict(version=1, client=client, lane='data')))
                    self.until(lambda: native.step(0), lambda: select.select([sock], [], [], 0)[0])
                    raw = sock.recv(4096)
                    return json.loads(raw) if raw else None
            self.assertIn('port', request('alpha'));self.assertIsNone(request('beta'))
            clients = copy.deepcopy(self.initial['clients']);clients.append(dict(clients[0], id='beta'))
            path = self.propose(clients)
            receipt = self.cli('limits-apply', '--proposal', path, '--wait-seconds', 0)[1]
            self.assertTrue(receipt['durable']);self.assertFalse(receipt['live_applied'])
            access_manager.propose(self.state, self.desc, keys, proposal)
            access_manager.apply(self.state, self.desc, self.manifest, proposal, wait_seconds=0)
            self.assertIn('error', admission.request('data', 'beta'))
            policies.reader.next_read = 0
            self.until(policies.step, lambda: model.status()['revision'] == 1)
            self.until(admission.update, lambda: admission.verified_clients == ('alpha', 'beta'))
            self.assertIn('port', admission.request('data', 'beta'))
            native.clients = frozenset(admission.verified_clients)
            self.assertIn('port', request('beta'))
            # This fixture already substitutes release/Guardian readiness.
            # Keep that seam explicit for the new read-only operator route;
            # native PID/birth/IPC remains real. Full release/parent identity
            # observation is covered by test_usage_management.
            observed = dict(model_generation=1, model_pid=model.process.pid,
                            model_start_ticks=controller.journal.process_start(model.process.pid))
            with mock.patch.object(manager.model_admin, 'observe', return_value=observed), \
                 mock.patch.object(manager.model_admin, 'recheck', return_value=observed):
                self.assertTrue(self.cli('limits-status')[1]['live_applied'])
            with mock.patch.object(admission.policy_channel, 'step', side_effect=AttributeError('CPU observer fault')):
                admission.update();self.assertIn('port', admission.request('data', 'alpha'))
            changed = dict(current, model_generation=2, gateway_bound_model=0)
            with mock.patch.object(gateway.control, 'binding', return_value=changed):
                admission.retry_at = 0;admission.update()
                self.assertFalse(admission.verified_clients)
                self.assertIn('error', admission.request('data', 'beta'))
        channel.begin.assert_not_called();self.assertIsNone(model.process.poll())

    def test_limits_loss_does_not_prevent_existing_key_revocation_or_reinitialize(self):
        shutil.rmtree(self.state/'client-limits')
        keys = self.fixture.directory/'revoked.json';proposal = self.fixture.directory/'revoke-proposal.json'
        save(keys, dict(version=1, clients=[dict(id='alpha', api_keys=[])]))
        access_manager.propose(self.state, self.desc, keys, proposal)
        receipt = access_manager.apply(self.state, self.desc, self.manifest, proposal, wait_seconds=0)
        self.assertTrue(receipt['durable'])
        with self.assertRaises(OSError):manager.initialize(self.state, self.desc, self.manifest)
        self.assertEqual(self.cli('limits-status')[0], 1)
        self.assertFalse((self.state/'client-limits').exists())
        with access_manager.open_journal(self.state, self.desc['service_id']) as current:
            self.assertFalse(current.head['state']['policy']['clients'][0]['key_sha256'])

    def test_new_model_requires_post_attach_read_and_helpers_do_not_bypass_empty_tree(self):
        policies = self.policies();policies.step(allow_start=False)
        self.assertEqual(policies.reader.started, 0)
        model = self.model();owner = supervisor.process_identity(model.process.pid)
        policies.attach(owner, 1, 'a'*64)
        self.until(policies.step, lambda: policies.installed)
        path = self.state/'client-limits/policy.json';original = path.read_bytes();path.write_bytes(b'{')
        second = self.model(2);policies.attach(supervisor.process_identity(second.process.pid), 2, 'a'*64)
        self.until(policies.step, lambda: policies.reader.error == 'state_invalid')
        self.assertFalse(policies.installed);self.assertIsNone(policies.channel.wanted)
        path.write_bytes(original);policies.reader.next_read = 0
        self.until(policies.step, lambda: policies.installed)
        self.assertIsNone(model.process.poll());self.assertIsNone(second.process.poll())

    def test_old_limits_migration_is_stopped_exclusive_and_preserves_core_ledger(self):
        state = self.fixture.directory/'old-state';state.mkdir(mode=0o700)
        desc = dict(self.desc, service_id=str(uuid.uuid4()));old = dict(self.manifest, version=7)
        with store.Store(state, desc['service_id'], initialize=True):pass
        fd = os.open(state/'guardian.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):manager.migrate(state, desc, old)
        finally:os.close(fd)
        with store.Store(state, desc['service_id']) as core:
            value = copy.deepcopy(core.state);value['desired'] = 'RUNNING';core.commit(value)
        with self.assertRaisesRegex(ValueError, 'STOPPED'):manager.migrate(state, desc, old)
        with store.Store(state, desc['service_id']) as core:
            value = copy.deepcopy(core.state);value['desired'] = 'STOPPED';core.commit(value)
        before = (state/'ledger.json').read_bytes()
        self.assertTrue(manager.migrate(state, desc, old)['initialized'])
        self.assertFalse(manager.migrate(state, desc, old)['initialized'])
        self.assertEqual((state/'ledger.json').read_bytes(), before)

    def test_controller_policy_errors_never_restart_core_and_start_permission_is_explicit(self):
        actor = progress_test.ControllerProgressTests().actor();actor.policies = mock.Mock()
        actor.model = None;self.assertFalse(actor.tick());actor.policies.step.assert_called_with(allow_start=False)
        model = actor.model = mock.Mock()
        actor.tick(allow_query=False);actor.policies.step.assert_called_with(allow_start=False)
        actor.tick();actor.policies.step.assert_called_with(allow_start=True)
        actor.policies.step.side_effect = AttributeError('CPU policy collector bug')
        self.assertFalse(actor.tick());actor.transaction.assert_not_called()
        self.assertEqual(actor.policies.error, 'client_policy_auxiliary_unavailable')
        model.terminate.assert_not_called();model.kill.assert_not_called()


if __name__ == '__main__':unittest.main()
