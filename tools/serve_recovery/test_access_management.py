"""Private CLI/package and real managed ingress CPU checks, no model loading."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_manager as manager, access_policy as policy, access_store
from tools.serve_recovery import bundle, gateway_config, ledger, service, store
from tools.serve_recovery.test_access_ingress import ALPHA, BETA, NEW
from tools.serve_recovery.test_gateway_nginx import Stack
from tools.serve_recovery import test_native_bundle

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_bytes(ledger.canonical(value));path.chmod(0o600)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.receipt = self.fixture.prepare()
        self.package = Path(self.receipt['package']);self.digest = self.receipt['manifest_sha256']
        self.manifest = bundle.verify(self.package, self.digest)
        self.state = self.fixture.directory / 'access-state'
        service.initialize(self.package, self.digest, self.state)
        self.desc = service.descriptor(self.state, self.package, self.digest)

    def cli(self, operation, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = service.main([operation, '--state', str(self.state), *map(str, arguments)],
                                package=self.package, digest=self.digest)
        raw = output.getvalue() + error.getvalue()
        for secret in (ALPHA, BETA, NEW):self.assertNotIn(secret, raw)
        return code, json.loads(raw)

    def keys(self, keys=None):
        path = self.fixture.directory / 'desired-keys.json'
        save(path, dict(version=1, clients=[dict(id='alpha', api_keys=[NEW] if keys is None else keys),
                                           dict(id='beta', api_keys=[BETA])]))
        return path

    def test_cli_propose_apply_query_replay_and_stale_revision(self):
        before = (self.state / 'ledger.json').read_bytes()
        proposal = self.fixture.directory / 'proposal.json';request = str(uuid.uuid4())
        code, value = self.cli('access-propose', '--keys', self.keys(), '--proposal', proposal,
                               '--request-id', request)
        self.assertEqual(code, 0);self.assertFalse(value['committed'])
        self.assertNotIn(NEW.encode(), proposal.read_bytes())
        code, receipt = self.cli('access-apply', '--proposal', proposal, '--wait-seconds', 0)
        self.assertEqual(code, 0);self.assertTrue(receipt['durable'])
        self.assertFalse(receipt['live_applied']);self.assertFalse(receipt['replayed'])
        self.assertTrue(self.cli('access-apply', '--proposal', proposal, '--wait-seconds', 0)[1]['replayed'])
        code, status = self.cli('access-status', '--request-id', request)
        self.assertEqual(code, 0);self.assertEqual(status['receipt']['id'], request)
        self.assertEqual(status['durable']['revision'], 1)
        changed = manager.private_json(proposal);changed['request_id'] = str(uuid.uuid4());save(proposal, changed)
        code, error = self.cli('access-apply', '--proposal', proposal, '--wait-seconds', 0)
        self.assertEqual(code, 1);self.assertEqual(error['code'], 'credential_revision_conflict')
        self.assertEqual((self.state / 'ledger.json').read_bytes(), before)

    def test_whole_journal_loss_cannot_rebootstrap_and_launch_stays_deferred(self):
        shutil.rmtree(self.state / 'client-access')
        with self.assertRaises(OSError):manager.initialize(self.state, self.desc, self.manifest)
        self.assertFalse((self.state / 'client-access').exists())
        self.assertEqual(self.cli('access-status')[0], 1)
        with self.assertRaisesRegex(ValueError, 'retain initialized'):
            manager.migrate(self.state, self.desc, self.manifest)
        fd = os.open(self.fixture.directory / 'owned-cpu-lock', os.O_CREAT | os.O_RDWR, 0o600)
        process = mock.Mock(returncode=0);process.poll.return_value = 0
        with mock.patch.object(service, 'acquire_lease', return_value=fd), \
             mock.patch.object(service.subprocess, 'Popen', return_value=process):
            result = service.launch(self.package, self.digest, self.state)
        cfg = manager.private_json(Path(result['run']) / 'gateway.json')
        self.assertEqual(cfg['version'], 3)
        self.assertEqual(cfg['access'], access_store.binding(self.state, self.desc['service_id']))
        gateway_config.validate(cfg)
        cfg['access']['state_identity'][0] = True
        with self.assertRaises(ValueError):gateway_config.validate(cfg)

    def test_old_migration_is_stopped_exclusive_and_keeps_core_ledger(self):
        target = self.fixture.directory / 'old-state';target.mkdir(mode=0o700)
        identity = str(uuid.uuid4());desc = dict(self.desc, service_id=identity)
        with store.Store(target, identity, initialize=True):pass
        old = dict(self.manifest, version=6)
        lock = os.open(target / 'guardian.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):manager.migrate(target, desc, old)
            self.assertFalse((target / access_store.MARKER).exists())
        finally:os.close(lock)
        with store.Store(target, identity) as core:
            running = copy.deepcopy(core.state);running['desired'] = 'RUNNING';core.commit(running)
        with self.assertRaisesRegex(ValueError, 'STOPPED'):manager.migrate(target, desc, old)
        with store.Store(target, identity) as core:
            stopped = copy.deepcopy(core.state);stopped['desired'] = 'STOPPED';core.commit(stopped)
        before = (target / 'ledger.json').read_bytes()
        self.assertTrue(manager.migrate(target, desc, old)['initialized'])
        self.assertFalse(manager.migrate(target, desc, old)['initialized'])
        self.assertEqual((target / 'ledger.json').read_bytes(), before)
        shutil.rmtree(target / 'client-access')
        with self.assertRaises(OSError):manager.migrate(target, desc, old)

    def test_proposal_identity_private_inputs_and_pending_membership_reject(self):
        keys = self.keys();proposal = self.fixture.directory / 'proposal.json'
        keys.chmod(0o644)
        with self.assertRaises(ValueError):manager.propose(self.state, self.desc, keys, proposal)
        keys.chmod(0o600)
        link = self.fixture.directory / 'hardlink';os.link(keys, link)
        with self.assertRaises(ValueError):manager.propose(self.state, self.desc, keys, proposal)
        link.unlink();link.symlink_to(keys)
        with self.assertRaises(OSError):manager.propose(self.state, self.desc, link, proposal)
        manager.propose(self.state, self.desc, keys, proposal)
        value = manager.private_json(proposal);value['service_id'] = str(uuid.uuid4());save(proposal, value)
        with self.assertRaisesRegex(ValueError, 'identity'):
            manager.apply(self.state, self.desc, self.manifest, proposal, wait_seconds=0)
        new = manager.private_json(keys);new['clients'].append(dict(id='new_client', api_keys=['Z'*40]));save(keys, new)
        with self.assertRaisesRegex(ValueError, 'coordinated'):
            manager.propose(self.state, self.desc, keys, self.fixture.directory / 'new-proposal')
        self.assertEqual(manager.status(self.state, self.desc, self.manifest)['durable']['revision'], 0)

    def test_binding_marker_interruption_and_state_swap_fail_closed(self):
        target = self.fixture.directory / 'interrupted';target.mkdir(mode=0o700)
        binding = access_store.binding(target, str(uuid.uuid4()))
        value = policy.from_keys([dict(id='alpha', api_keys=[ALPHA])])
        with mock.patch.object(access_store, 'Store', side_effect=OSError('CPU bootstrap fault')):
            with self.assertRaises(OSError):access_store.initialize_bound(binding, value)
        self.assertTrue((target / access_store.MARKER).exists())
        with self.assertRaises(OSError):access_store.initialize_bound(binding, value)
        self.assertFalse((target / 'client-access').exists())
        swapped = dict(binding, state_identity=[binding['state_identity'][0], binding['state_identity'][1]+1])
        with self.assertRaises(ValueError):access_store.check_binding(swapped)
        target = self.fixture.directory / 'parent-sync';target.mkdir(mode=0o700)
        binding = access_store.binding(target, str(uuid.uuid4()))
        sync = os.fsync;calls = 0
        def fail_parent_commit(fd):
            nonlocal calls
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino] == binding['state_identity']:
                calls += 1
                if calls == 2:raise OSError('CPU parent directory sync fault')
            return sync(fd)
        with mock.patch.object(access_store.os, 'fsync', side_effect=fail_parent_commit):
            with self.assertRaises(OSError):access_store.initialize_bound(binding, value)
        before = (target / 'client-access/policy.json').read_bytes()
        # The ordinary CLI/reader recovery path must sync the parent too;
        # it cannot depend on repeating an initialization-only command.
        with mock.patch.object(access_store, 'sync_binding', side_effect=OSError('CPU sync unknown')):
            with self.assertRaises(OSError):access_store.open_bound(binding)
        with access_store.open_bound(binding) as restored:
            self.assertEqual(restored.head['state']['policy'], value)
        self.assertEqual((target / 'client-access/policy.json').read_bytes(), before)


class ManagedIngressTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/serve-access-20261109/managed-fixtures';base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.state = self.root / 'state';self.state.mkdir(mode=0o700)
        self.id = str(uuid.uuid4());self.binding = access_store.binding(self.state, self.id)
        initial = policy.from_keys([dict(id='alpha', api_keys=[ALPHA]), dict(id='beta', api_keys=[BETA])])
        access_store.initialize_bound(self.binding, initial)
        self.binary = Path(os.environ.get('Q4T_ACCESS_INGRESS',
            ROOT / '.q4t-work/serve-access-20261109/host/q4t-ingress'))
        self.package = self.root / 'package';(self.package / 'engine').mkdir(parents=True)
        self.desc = dict(service_id=self.id, package=str(self.package))
        self.manifest = dict(version=7, ingress_mode='native',
            files={'bin/q4t-ingress': hashlib.sha256(self.binary.read_bytes()).hexdigest()})

    def start(self):
        self.stack = Stack(self.root, native_ingress=self.binary, access=self.binding)
        self.addCleanup(self.stack.close)
        save(self.package / 'engine/client-policy.json', self.stack.value['backend_policy'])
        self.stack.wait(lambda: any(e['event'] == 'ready' for e in self.stack.events))

    def ready(self):
        self.stack.wait(lambda: self.stack.bound == self.stack.generation)
        self.assertEqual(self.stack.request()[0], 200)

    def change(self, keys):
        path = self.root / str(uuid.uuid4());proposal = self.root / str(uuid.uuid4())
        save(path, dict(version=1, clients=[dict(id='alpha', api_keys=keys), dict(id='beta', api_keys=[BETA])]))
        manager.propose(self.state, self.desc, path, proposal)
        return manager.apply(self.state, self.desc, self.manifest, proposal, wait_seconds=4)

    def test_managed_rotation_live_receipt_and_restart_preserve_revocation(self):
        self.start();self.ready()
        original = manager.live(self.desc, self.manifest)
        result = self.change([ALPHA, NEW])
        self.assertTrue(result['live_applied']);self.assertEqual(result['live']['entrypoint_pid'], original['entrypoint_pid'])
        self.assertEqual(self.stack.request(key=NEW)[0], 200)
        self.assertTrue(self.change([NEW])['live_applied'])
        self.assertEqual(self.stack.request()[0], 401)
        self.assertEqual(self.stack.request(dict(prompt='CPU auth'), key=NEW)[0], 200)
        self.assertEqual(self.stack.forwarded[-1]['client'], 'alpha')
        self.stack.restart_gateway();self.stack.wait(lambda: self.stack.bound == 1)
        self.assertEqual(self.stack.request()[0], 401)
        self.assertEqual(self.stack.request(key=NEW)[0], 200)
        observed = manager.status(self.state, self.desc, self.manifest)
        self.assertTrue(observed['live_applied']);self.assertEqual(observed['live']['revision'], 2)
        self.assertEqual(observed['live']['gateway_generation'], 2)
        self.assertNotEqual(observed['live']['entrypoint_pid'], original['entrypoint_pid'])
        with self.assertRaises(ValueError):manager.live(self.desc,
            dict(self.manifest, files={'bin/q4t-ingress': '0'*64}))
        self.assertEqual(self.stack.generation, 1)

    def test_journal_loss_keeps_native_and_gateway_heartbeat_then_restart_503(self):
        self.start();self.ready()
        pid = manager.live(self.desc, self.manifest)['entrypoint_pid'];gateway_pid = self.stack.process.pid
        (self.state / 'client-access/policy.json').unlink()
        before, events = time.monotonic(), len(self.stack.events)
        self.stack.wait(lambda: time.monotonic() - before > 1.4 and len(self.stack.events) >= events + 3)
        self.assertEqual(self.stack.request(dict(prompt='CPU survives'))[0], 200)
        self.assertEqual(manager.live(self.desc, self.manifest)['entrypoint_pid'], pid)
        self.assertEqual(self.stack.process.pid, gateway_pid)
        status = manager.status(self.state, self.desc, self.manifest)
        self.assertIsNone(status['durable']);self.assertTrue(status['live']['configured'])
        self.assertFalse(status['live_applied'])
        self.stack.restart_gateway()
        self.stack.wait(lambda: len([e for e in self.stack.events if e['event'] == 'ready']) == 2)
        self.assertEqual(self.stack.request()[0], 503)
        self.assertEqual(self.stack.bound, 0)
        self.assertEqual(self.stack.generation, 1)

    def test_first_start_without_binding_never_reloads_sealed_package_keys(self):
        (self.state / access_store.MARKER).unlink()
        self.start()
        before = time.monotonic()
        self.stack.wait(lambda: time.monotonic() - before > 1.3)
        self.assertEqual(self.stack.request()[0], 503)
        self.assertEqual(self.stack.bound, 0)
        self.assertEqual(len([e for e in self.stack.events if e['event'] == 'ready']), 1)
        self.assertGreaterEqual(len([e for e in self.stack.events if e['event'] == 'heartbeat']), 3)
        self.assertFalse(manager.live(self.desc, self.manifest)['configured'])


if __name__ == '__main__':unittest.main()
