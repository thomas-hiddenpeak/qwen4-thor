"""CPU admission tests: actual credentials, locks, journals, domains and IPC.

Observation and BPF/credential issuance are explicit fakes in authority tests.
The live installed issuer remains UNARMED and cannot query/open a GPU. Native
credential/BPF enforcement has separate kernel evidence; no model runs here.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, bundle, cold_authority as authority, cold_channel as wire
from tools.serve_recovery import cold_client, cold_host, cold_issuer, cold_requests as requests
from tools.serve_recovery import device_control, device_domain as domain, device_resource
from tools.serve_recovery import device_store, host_install as host, ledger, outer_instance, outer_manager
from tools.serve_recovery import outer_store, outer_tree, resource_install, resource_launch, store
from tools.serve_recovery import test_host_install as fixtures, test_device_resource as resource_fixtures


def peer_runtime(path, channel_fd):
    args = json.loads(path.read_text()); state = Path(args['state'])
    bundle.engine.verify = lambda *_a, **_kw: args['base']  # Explicit CPU package only.
    capability = resource_launch.inherited(args['service_id'])
    chosen = json.loads((Path(args['package'])/'manifest.json').read_text())
    lease = os.open(chosen['lock_path'], os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with socket.socket(fileno=channel_fd) as control, \
            outer_instance.Instance(state, args['service_id']) as instance, outer_tree.Tree() as tree:
        with store.Store(state, args['service_id']) as core:
            core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                operation='desired', args=dict(state='RUNNING')))
            binding = outer_manager.ready(state, dict(service_id=args['service_id']))
            result = outer_store.commit_once(binding, 'request', dict(core=core.state,
                ticket=str(uuid.uuid4()), manifest_sha256=args['digest'], cleanup_verified=True,
                identity_valid=True, custody=tree.binding(instance), resource=capability.binding()))
        attempt = result['state']['pending']; tree.reserve(instance, attempt)
        value = host.public(args['host']); info = os.fstat(lease)
        selected = dict(version=1, host_id=value['installation_id'], service_id=args['service_id'],
            manifest_sha256=args['digest'], ticket=attempt['ticket'], serial=attempt['serial'],
            boot_id=attempt['boot_id'], custody=attempt['custody'],
            lease_device=info.st_dev, lease_inode=info.st_ino)
        wire.send(control, selected, (lease, instance.fd))
        while True:
            with wire.receive(control, count=0) as message:
                operation = message.result['operation']
            if operation == 'exit': break
            if operation == 'stop-intent':
                with store.Store(state, args['service_id']) as core:
                    core.transact(dict(id=str(uuid.uuid4()), expected_revision=core.state['revision'],
                        operation='desired', args=dict(state='STOPPED')))
                wire.send(control, dict(stopped=True))
            elif operation == 'issuer-request':
                try:
                    with wire.request(selected['host_id'], selected, lease, instance.fd):
                        wire.send(control, dict(issued=True))
                except (OSError, ValueError): wire.send(control, dict(issued=False))
            elif operation == 'client-status':
                client = cold_client.Host(args['host'], state, args['digest'], args['service_id'], capability)
                wire.send(control, dict(available=client.available()))
            else: raise ValueError('unknown CPU peer operation')
    capability.close(); os.close(lease)


class HistoryTests(resource_fixtures.ResourceTests):
    def request_value(self, serial=1):
        now = store.stamp()
        return dict(version=1, host_id=str(uuid.uuid4()), service_id=self.sid,
            manifest_sha256='a'*64, ticket=str(uuid.uuid4()), serial=serial, boot_id=now.boot_id,
            custody=dict(supervisor=dict(pid=os.getpid(), start_ticks=1, boot_id=now.boot_id),
                         instance_token=str(uuid.uuid4()), tree_scope=str(uuid.uuid4())),
            lease_device=os.fstat(self.lease).st_dev, lease_inode=os.fstat(self.lease).st_ino)

    def test_spent_attempt_survives_lost_reply_new_host_and_new_boot(self):
        self.init_device(); config = self.config(); selected = self.request_value()
        original = requests.Store.transact
        def lost(current, *args, **kwargs):
            original(current, *args, **kwargs); raise OSError('injected lost durable reply')
        with mock.patch.object(requests.Store, 'transact', lost), self.assertRaises(OSError):
            requests.spend(self.device, config, selected)
        with self.assertRaisesRegex(ValueError, 'spent'): requests.spend(self.device, config, selected)
        successor = self.request_value(2)
        successor['boot_id'] = successor['custody']['supervisor']['boot_id'] = str(uuid.uuid4())
        self.assertEqual(requests.spend(self.device, config, successor)['revision'], 2)
        self.assertEqual(domain.status(self.device)['charged_total'], 0)

    def test_lost_replay_journal_or_marker_cannot_be_reinitialized(self):
        self.init_device(); config = self.config(); requests.spend(self.device, config, self.request_value())
        paths = (self.device/requests.Store.marker_name,
                 self.device/requests.Store.directory_name/'policy.json')
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes(); path.unlink()
                with self.assertRaises((OSError, ValueError)): domain.resume_initialize(self.device)
                path.write_bytes(raw); path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'spent'):
            requests.spend(self.device, config, self.request_value())

    def test_partial_replay_initialization_resumes_only_unpublished_seed(self):
        original = access_store.bootstrap_bound
        def partial(binding, initial, writer):
            result = original(binding, initial, writer)
            if writer is requests.Store: raise OSError('injected unpublished request initialization')
            return result
        with mock.patch.object(access_store, 'bootstrap_bound', partial), self.assertRaises(OSError):
            self.init_device()
        domain.resume_initialize(self.device)
        self.assertEqual(requests.spend(self.device, self.config(), self.request_value())['revision'], 1)


class AuthorityTests(fixtures.HostTests):
    def helper(self, operation):
        if operation != 'prepare': return super().helper(operation)
        result = subprocess.run([sys.executable, '-I', '-B', __file__, '--prepare', str(self.path)],
            user=self.uid, group=self.gid, extra_groups=[], env=host.environment(),
            capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def setUp(self):
        super().setUp(); self.initialize_host(); host.configure(self.host, enabled=True)
        self.device = self.parent/'device'; binary = ROOT/'.q4t-work/device-detach-build/q4t-device-control'
        domain.initialize(self.device, self.sid, self.uid, self.gid, binary,
                          hashlib.sha256(binary.read_bytes()).hexdigest(), resource_directory=self.directory)
        self.config = domain.configuration(domain.read_record(self.device/'configuration.json'))
        self.operations = []; self.peer = None
        self.addCleanup(cold_host.withdraw, self.host)

    def start_peer(self):
        data = dict(self.fixture, base=json.loads((self.path/'cpu-engine.json').read_text()), host=str(self.host))
        path = self.path/'cold-peer.json'; path.write_text(json.dumps(data)); path.chmod(0o600)
        os.chown(path, self.uid, self.gid)
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        process = subprocess.Popen([sys.executable, '-I', '-B', __file__, '--peer-entry',
            str(self.directory), str(path), str(endpoint.fileno())], pass_fds=(endpoint.fileno(),),
            env=host.environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process); endpoint.close(); parent.settimeout(6)
        self.addCleanup(parent.close)
        try: incoming = wire.receive(parent, count=2)
        except BaseException:
            if process.poll() is not None: self.fail(process.stderr.read().decode())
            raise
        self.addCleanup(incoming.close); self.selected = incoming.result
        self.lease, self.instance = incoming.fds
        self.pidfd = os.pidfd_open(process.pid); self.addCleanup(os.close, self.pidfd)
        self.peer = process; self.peer_control = parent
        return self.selected

    @contextmanager
    def fake_devices(self, *, fail_issue=False):
        pins = self.parent/'cpu-pins'; pins.mkdir(mode=0o700)
        observation = dict(version=1, status='NO_USER_HANDLES_OBSERVED',
            facts=dict(boot_id=store.stamp().boot_id, character_devices=[dict(major=1, names=['cpu'])]),
            outstanding=[], model_actions=False, launch_authorized=False)
        @contextmanager
        def reply(operation, args, **_kwargs):
            self.operations.append(operation)
            if operation == 'issue':
                if fail_issue: raise OSError('injected lost credential issuance')
                # Explicit fake kernel capabilities; no BPF or GPU is opened.
                fds = [os.dup(self.lease), os.dup(self.instance)]
                with device_control.Reply(dict(fake=True), fds) as value: yield value
                return
            identity = dict(target_cgroup_id=os.fstat(args['target_fd']).st_ino,
                allowed_cgroup_id=os.fstat(args['allowed_fd']).st_ino,
                link_id=11, program_id=12, program_tag='0123456789abcdef')
            if operation == 'attach': Path(args['pin']).write_bytes(b'CPU-only fake rule')
            self.assertIn(operation, ('attach', 'adopt'))
            with device_control.Reply(identity, [os.dup(args['target_fd']), os.dup(args['allowed_fd'])]) as value:
                yield value
        with mock.patch.object(domain, 'BPFFS', pins), \
                mock.patch.object(domain.Observation, 'inspect', return_value=observation), \
                mock.patch.object(device_control.Control, 'call', side_effect=reply):
            domain.arm(self.device, self.lease, self.lease_path())
            yield

    def lease_path(self):
        return json.loads((Path(self.fixture['package'])/'manifest.json').read_text())['lock_path']

    def issue(self, selected=None, instance=None):
        return authority.issue(self.host, self.selected if selected is None else selected,
            self.lease, self.instance if instance is None else instance,
            (self.peer.pid, self.uid, self.gid), self.pidfd)

    def test_actual_peer_original_intent_and_private_verifier_issue_once(self):
        self.start_peer()
        with self.fake_devices():
            with self.issue() as result:
                self.assertEqual(len(result.fds), 4)
                self.assertEqual(result.result['request_sha256'], ledger.digest(self.selected))
            with self.assertRaisesRegex(ValueError, 'spent'): self.issue()
        self.assertEqual(self.operations.count('issue'), 1)
        self.assertEqual(domain.status(self.device)['charged_total'], 1)
        self.assertIsNone(self.peer.poll())

    def test_issue_failure_spends_both_original_attempt_and_device_charge(self):
        self.start_peer()
        with self.fake_devices(fail_issue=True):
            with self.assertRaises(OSError): self.issue()
            with self.assertRaisesRegex(ValueError, 'spent'): self.issue()
        self.assertEqual(domain.status(self.device)['charged_total'], 1)
        self.assertIsNone(self.peer.poll())

    def test_stop_intent_and_changed_request_never_reach_device_control(self):
        self.start_peer()
        with self.fake_devices():
            count = len(self.operations)
            changed = deepcopy(self.selected); changed['ticket'] = str(uuid.uuid4())
            with self.assertRaisesRegex(ValueError, 'verification failed'): self.issue(changed)
            wire.send(self.peer_control, dict(operation='stop-intent'))
            with wire.receive(self.peer_control, count=0) as reply: self.assertTrue(reply.result['stopped'])
            with self.assertRaisesRegex(ValueError, 'verification failed'): self.issue()
            self.assertEqual(len(self.operations), count)
        self.assertEqual(domain.status(self.device)['charged_total'], 0)

    def test_replaced_instance_fd_and_peer_start_cannot_issue(self):
        self.start_peer()
        fake = os.open(self.path/'fake-instance', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchown(fake, self.uid, self.gid); self.addCleanup(os.close, fake)
        fcntl.flock(fake, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.fake_devices():
            with self.assertRaisesRegex(ValueError, 'verification failed'): self.issue(instance=fake)
            with mock.patch.object(authority, 'verify_runtime') as verify:
                checked = resource_install.read_public(self.directory)
                verify.return_value = dict(lease_path=self.lease_path(),
                    resource=dict(directory=str(self.directory), publication=checked))
                changed = deepcopy(self.selected); changed['custody']['supervisor']['start_ticks'] += 1
                with self.assertRaisesRegex(ValueError, 'peer identity'): self.issue(changed)
        self.assertNotIn('issue', self.operations)

    def test_disabled_or_retired_host_does_not_kill_existing_peer(self):
        self.start_peer(); host.configure(self.host, enabled=False)
        with self.assertRaisesRegex(ValueError, 'not enabled'): self.issue()
        self.assertIsNone(self.peer.poll()); self.assertEqual(domain.status(self.device)['charged_total'], 0)

    def test_actual_installed_root_channel_rejects_unarmed_without_touching_peer(self):
        self.start_peer()
        process = subprocess.Popen([sys.executable, '-I', '-B',
            str(self.host/'source/tools/serve_recovery/cold_issuer.py'), '--directory', str(self.host), '--serve'],
            env=host.environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        end = time.monotonic()+5
        while True:
            try: status = cold_issuer.control(self.host, 'status'); break
            except (OSError, ValueError):
                if process.poll() is not None: self.fail(process.stderr.read().decode())
                if time.monotonic() >= end: self.fail('owned issuer readiness timeout')
                time.sleep(.02)
        self.assertEqual(status['issuer_pid'], process.pid)
        wire.send(self.peer_control, dict(operation='issuer-request'))
        with wire.receive(self.peer_control, count=0) as reply: self.assertFalse(reply.result['issued'])
        self.assertIsNone(self.peer.poll())
        self.assertFalse(domain.status(self.device)['qualified'])
        self.assertEqual(domain.status(self.device)['charged_total'], 0)
        cold_issuer.control(self.host, 'stop'); self.assertEqual(process.wait(timeout=5), 0)
        self.assertIsNone(self.peer.poll())


class HostIntegrationTests(AuthorityTests):
    def test_stopped_host_entry_selects_detached_issuer_and_retains_zero_starts(self):
        before = self.state_files()
        result = self.enter()
        self.assertEqual(result['state'], 'STOPPED'); self.assertTrue(result['cleanup_confirmed'])
        status = cold_issuer.control(self.host, 'status')
        self.assertEqual(status['admission_state'], 'unarmed')
        self.assertEqual(self.state_files(), before)
        self.assertTrue(cold_host.withdraw(self.host)['issuer_exited'])

    def test_actual_unarmed_status_is_unavailable_to_original_unprivileged_client(self):
        first = cold_host.ensure(self.host); self.assertTrue(first['selected'])
        second = cold_host.ensure(self.host)
        self.assertEqual(first['issuer']['issuer_pid'], second['issuer']['issuer_pid'])
        self.start_peer(); before = self.state_files()
        wire.send(self.peer_control, dict(operation='client-status'))
        with wire.receive(self.peer_control, count=0) as reply: self.assertFalse(reply.result['available'])
        self.assertEqual(before, self.state_files()); self.assertIsNone(self.peer.poll())

    def test_host_removal_withdraws_issuer_and_preserves_original_core(self):
        before = self.state_files(); started = cold_host.ensure(self.host)
        pidfd = os.pidfd_open(started['issuer']['issuer_pid']); self.addCleanup(os.close, pidfd)
        result = host.remove(self.host)
        self.assertTrue(result['removed']); self.assertTrue(select.select([pidfd], [], [], 0)[0])
        self.assertEqual(before, self.state_files())

    def test_lost_shutdown_reply_never_claims_live_issuer_exited(self):
        selected = host.public(self.host)
        process = subprocess.Popen([sys.executable, '-I', '-B', __file__, '--reset-issuer',
            selected['installation_id']], env=host.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 3)[0])
            self.assertEqual(process.stdout.readline().strip(), b'ready')
            with self.assertRaisesRegex(ValueError, 'still owns'): cold_host.withdraw(self.host)
            self.assertIsNone(process.poll())
        finally:
            if process.poll() is None: process.terminate()
            process.wait(timeout=3)

    def test_entry_resource_epoch_renewal_keeps_device_and_replay_history(self):
        original = resource_install.load(self.directory); old = deepcopy(original)
        old['record']['boot_id'] = str(uuid.uuid4())
        resource_install.commit(self.directory, old)
        with fixtures.kernel.Scope(original['record']) as scope:
            self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))
        path = Path(original['record']['root'])
        for name in fixtures.kernel.NAMES: (path/name).rmdir()
        path.rmdir()
        record = self.device/requests.Store.directory_name/'policy.json'; raw = record.read_bytes()
        host.check_resource(host.load(self.host), renew=True)
        current = resource_install.load(self.directory)
        self.assertEqual(current['domain_id'], original['domain_id'])
        self.assertNotEqual(current['record']['root_identity'], original['record']['root_identity'])
        self.assertEqual(current['record']['boot_id'], store.stamp().boot_id)
        self.assertEqual(record.read_bytes(), raw)
        self.assertEqual(domain.configuration(domain.read_record(self.device/'configuration.json')), self.config)

    def test_automatic_requalification_refuses_idle_and_rechecks_original_armed_boot(self):
        self.start_peer()
        with mock.patch.object(domain.Observation, 'inspect') as inspect:
            with self.assertRaisesRegex(ValueError, 'armed original fence'):
                domain.arm(self.device, self.lease, self.lease_path(), renew_boot=True)
            inspect.assert_not_called()
        with self.fake_devices():
            binding = access_store.binding(self.device, self.sid)
            held = os.fstat(self.lease)
            device_store.charge_once(binding, self.fixture['digest'], held.st_dev, held.st_ino)
            previous = device_resource.lifecycle(self.device, self.config)
            domain.write_record(self.device/device_resource.FENCE, dict(previous, boot_id=str(uuid.uuid4())))
            _, pins = domain.names(self.config); (pins/'policy').unlink(); pins.rmdir()
            domain.arm(self.device, self.lease, self.lease_path(), renew_boot=True)
            self.assertEqual(domain.status(self.device)['charged_total'], 1)
            self.assertEqual(device_resource.lifecycle(self.device, self.config)['boot_id'], store.stamp().boot_id)


class NoDeviceHostTests(fixtures.HostTests):
    def test_no_device_association_adds_no_issuer_dependency(self):
        self.initialize_host(); host.configure(self.host, enabled=True)
        with mock.patch.object(cold_host.subprocess, 'run', side_effect=AssertionError('no issuer expected')):
            result = cold_host.ensure(self.host)
        self.assertFalse(result['selected']); self.assertFalse(result['model_actions'])


def fixture_worker(mode, fd):
    with socket.socket(fileno=fd) as channel:
        with wire.receive(channel, count=0) as incoming: value = incoming.result
        if mode == 'orphan':
            pid = os.fork()
            if pid == 0:
                channel.close(); os.setsid(); time.sleep(10); os._exit(0)
        if mode == 'no-reply': time.sleep(10); return
        wire.send(channel, dict(ok=True, result=dict(cpu_fixture=True)), [value['descriptors'][0]]*4)


class ChannelTests(unittest.TestCase):
    def setUp(self):
        base = ROOT/'.q4t-work/serve-cold-channel-20261112/fixtures'; base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'lease'
        self.lease = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(self.close_lease)

    def close_lease(self):
        if self.lease is not None: os.close(self.lease); self.lease = None

    def worker(self, mode, timeout=1):
        result = wire.Worker.start([sys.executable, '-I', '-B', __file__, '--fixture-worker', mode],
                                  dict(version=1), [self.lease], timeout=timeout)
        self.addCleanup(self.finish, result)
        return result

    def finish(self, worker):
        if not worker.tree.entered: return
        worker.failure = worker.failure or 'test_cleanup'
        end = time.monotonic()+5
        while not worker.poll():
            if time.monotonic() >= end: self.fail('own CPU worker cleanup deadline')
            time.sleep(.01)
        worker.close()

    def test_real_worker_returns_fds_only_after_complete_tree_exit(self):
        worker = self.worker('success'); end = time.monotonic()+3
        while not worker.poll():
            self.assertLess(time.monotonic(), end); time.sleep(.01)
        self.assertIsNone(worker.failure); self.assertEqual(len(worker.response.fds), 4)
        self.assertTrue(outer_tree.no_children()); worker.close()

    def test_orphan_and_timeout_hold_original_slot_and_lease_until_reaped(self):
        worker = self.worker('orphan', timeout=2)
        self.close_lease()
        probe = os.open(self.path, os.O_RDWR); self.addCleanup(os.close, probe)
        end = time.monotonic()+1
        while worker.response is None and time.monotonic() < end:
            self.assertFalse(worker.poll()); time.sleep(.01)
        self.assertIsNotNone(worker.response)
        with self.assertRaises(BlockingIOError): fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(ValueError, 'pre-existing children'):
            wire.Worker.start([sys.executable], {}, [probe])
        with self.assertRaisesRegex(ValueError, 'still owns'): worker.close()
        self.finish(worker)
        self.assertEqual(worker.failure, 'test_cleanup')
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_no_reply_times_out_and_reaps_only_own_worker(self):
        worker = self.worker('no-reply', timeout=.1); end = time.monotonic()+3
        while not worker.poll():
            self.assertLess(time.monotonic(), end); time.sleep(.01)
        self.assertEqual(worker.failure, 'worker_timeout'); worker.close()

    def test_message_credentials_reject_socket_forwarded_to_another_process(self):
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(parent.close); self.addCleanup(endpoint.close)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1); parent.settimeout(2)
        expected = wire.credentials(parent)
        pid = os.fork()
        if pid == 0:
            parent.close(); wire.send(endpoint, dict(forwarded=True)); os._exit(0)
        try:
            with self.assertRaisesRegex(ValueError, 'invalid cold channel'):
                wire.receive(parent, count=0, peer=expected)
        finally: os.waitpid(pid, 0)

    def test_wrong_fd_count_closes_received_capabilities(self):
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            before = set(Path('/proc/self/fd').iterdir())
            wire.send(endpoint, dict(cpu=True), [self.lease]*2)
            with self.assertRaises(ValueError): wire.receive(parent, count=1)
            self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)
        finally: parent.close(); endpoint.close()

    def test_closed_peer_is_transport_loss_without_received_capabilities(self):
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        endpoint.close(); parent.settimeout(2)
        try:
            with self.assertRaises(ConnectionResetError): wire.receive(parent, count=0)
        finally: parent.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--prepare':
        fixtures.prepare(Path(sys.argv[2]), cold=True)
    elif len(sys.argv) > 1 and sys.argv[1] == '--peer-entry':
        os.set_inheritable(int(sys.argv[4]), True)
        resource_launch.enter(sys.argv[2], [sys.executable, '-I', '-B', __file__,
            '--peer-runtime', sys.argv[3], sys.argv[4]])
    elif len(sys.argv) > 1 and sys.argv[1] == '--peer-runtime':
        peer_runtime(Path(sys.argv[2]), int(sys.argv[3]))
    elif len(sys.argv) > 1 and sys.argv[1] == '--fixture-worker':
        fixture_worker(sys.argv[2], int(sys.argv[4]))
    elif len(sys.argv) > 1 and sys.argv[1] == '--reset-issuer':
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(wire.address(sys.argv[2])); listener.listen(1)
            print('ready', flush=True)
            connection, _ = listener.accept(); connection.close(); time.sleep(10)
    else: unittest.main(module=None)
