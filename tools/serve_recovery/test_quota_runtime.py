"""Limits reader, actual native publication and immutable boot discovery."""
import copy
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_gateway import client_policy as cp
from tools.serve_recovery import access_io, access_policy, access_store, binding, journal
from tools.serve_recovery import quota_control as control, quota_policy as policy, quota_store
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_quota_admin import Server
from tools.serve_service import supervisor

ROOT = Path(__file__).resolve().parents[2]


class QuotaRuntimeTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/serve-access-20261109/runtime-fixtures';base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        self.state = Path(temp.name);self.service = str(uuid.uuid4())
        self.binding = access_store.binding(self.state, self.service)
        self.initial = policy.from_base(config(self.state)['backend_policy'])
        quota_store.initialize_bound(self.binding, self.initial)
        access_store.initialize_bound(self.binding, access_policy.from_keys([
            dict(id='alpha', api_keys=['A'*40]), dict(id='beta', api_keys=['B'*40])]))

    def reader(self, kind):
        value = access_io.Reader(self.state, self.service, self.binding['state_identity'], kind=kind)
        def close():
            deadline = time.monotonic()+3
            while not value.close() and time.monotonic()<deadline:time.sleep(.01)
            self.assertIsNone(value.process);self.assertIsNone(value.claim)
        self.addCleanup(close)
        return value

    def server(self, **kwargs):
        value = Server(policy.runtime(self.initial, 'a'*64), **kwargs)
        self.addCleanup(value.close)
        return value

    def until(self, step, predicate, seconds=4):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            step()
            if predicate():return
            time.sleep(.005)
        self.fail('bounded CPU policy condition not reached')

    def test_independent_readers_feed_actual_model_policy_and_file_failure_preserves_it(self):
        keys, limits = self.reader('access'), self.reader('limits')
        server = self.server();channel = control.Channel(server.address);self.addCleanup(channel.close)
        start = journal.process_start(server.process.pid)
        def step():
            keys.step();limits.step()
            if limits.available:channel.desired(limits.value, 'a'*64)
            channel.step(server.process.pid, start)
        self.until(step, lambda: keys.available and channel.status()['live_applied'])
        self.assertNotEqual(keys.process.pid, limits.process.pid)
        self.assertNotEqual(keys.claim.getsockname(), limits.claim.getsockname())
        clients = copy.deepcopy(self.initial['clients']);clients[0]['requests_per_minute'] = 1
        changed = policy.desired(self.initial, clients)
        self.assertTrue(server.command('reserve', id='alpha')['ok']);server.command('settle')
        with quota_store.open_bound(self.binding) as writer:writer.transact(str(uuid.uuid4()), 0, changed)
        limits.next_read = 0
        self.until(step, lambda: channel.status()['live_applied'] and channel.status()['desired_revision'] == 1)
        self.assertFalse(server.command('reserve', id='alpha')['ok'])
        path = self.state/'client-limits/policy.json';path.rename(path.with_suffix('.lost'))
        limits.next_read = 0
        self.until(step, lambda: not limits.available)
        self.assertTrue(keys.available);self.assertEqual(keys.started, 1);self.assertEqual(limits.started, 1)
        self.assertEqual(server.status()['revision'], 1)
        self.assertFalse(server.command('reserve', id='alpha')['ok'])

    def test_limits_claim_and_stopped_worker_are_separate_and_reap_before_replacement(self):
        keys, limits, competing = self.reader('access'), self.reader('limits'), self.reader('limits')
        self.until(lambda:(keys.step(), limits.step()), lambda: keys.available and limits.available)
        competing.step();self.assertEqual(competing.started, 0)
        process = limits.process;os.kill(process.pid, signal.SIGSTOP);limits.next_read = 0
        self.until(limits.step, lambda: limits.stopping)
        self.assertEqual(limits.error, 'reader_timeout');self.assertFalse(limits.available)
        self.until(limits.step, lambda: limits.process is None)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(limits.reaped, 1);limits.next_start = 0
        self.until(limits.step, lambda: limits.available)
        self.assertEqual(limits.started, 2);self.assertIsNone(keys.process.poll())

    def test_inflight_publication_is_frozen_then_latest_revision_applies_and_owner_is_pinned(self):
        server = self.server();clock = [time.monotonic()]
        channel = control.Channel(server.address, clock=lambda: clock[0]);self.addCleanup(channel.close)
        first = policy.desired(self.initial, self.initial['clients'])
        second = policy.desired(first, first['clients'])
        start = journal.process_start(server.process.pid)
        channel.desired(first, 'a'*64)
        self.until(lambda: channel.step(server.process.pid, start), lambda: channel.pending is not None)
        frozen = channel.pending['raw'];channel.desired(second, 'a'*64)
        self.assertEqual(channel.pending['raw'], frozen)
        def step():
            clock[0] = time.monotonic();channel.step(server.process.pid, start)
        self.until(step, lambda: channel.status()['live_applied'])
        self.assertEqual(server.status()['revision'], 2)
        self.assertEqual(channel.status()['desired_revision'], 2)
        clock[0] += 4
        self.assertFalse(channel.status()['live_applied']);self.assertIsNone(channel.status()['observed'])
        channel.step(server.process.pid, start+1)
        self.assertIsNone(channel.status()['observed']);self.assertFalse(channel.status()['live_applied'])
        self.assertEqual(server.status()['revision'], 2)

    def test_factory_uses_parent_identity_without_any_progress_channel(self):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('Q4T_PROGRESS_', 'Q4T_POLICY_'))}
        env.update(Q4T_SERVICE_ID=self.service, Q4T_MODEL_GENERATION='4',
                   Q4T_POLICY_PARENT_PID=str(os.getpid()),
                   Q4T_POLICY_PARENT_START=str(journal.process_start(os.getpid())))
        server = self.server(mode='environment', address=control.address(self.service, 4), env=env)
        self.assertTrue(server.initial['publisher_alive'])
        first = policy.desired(self.initial, self.initial['clients'])
        self.assertTrue(server.publish(policy.runtime(first, 'a'*64), 0)['ok'])
        for change in ({'Q4T_POLICY_PARENT_START':'1'}, {'Q4T_SERVICE_ID':'invalid'}):
            refused = self.server(mode='environment', env=dict(env, **change))
            self.assertIsNone(refused.initial)
            self.assertTrue(refused.command('reserve', id='alpha')['ok'])
        server.command('stop')
        self.assertTrue(server.command('reserve', id='alpha')['ok'])

    def test_startup_binding_stays_strict_and_does_not_accept_current_revision(self):
        raw = binding.policy_bytes(self.initial, 'a'*64);expected = cp.binding(raw)
        observed = dict(profile='q4t-gateway-clients-v1', credential_values_redacted=True, **expected)
        cp.observed_matches(observed, expected)
        self.assertEqual(expected['snapshot'], 'startup')
        for changes in ({'revision': 1}, {'revision':False}, {'revision':0.0},
                        {'snapshot':'current'}, {'sha256':'f'*64},
                        {'content_sha256':'f'*64}, {'mutable':False}):
            with self.assertRaises(ValueError):cp.observed_matches(dict(observed, **changes), expected)
        legacy = cp.binding(binding.policy_bytes(config(self.state)['backend_policy'], 'b'*64))
        cp.observed_matches(dict(profile='q4t-gateway-clients-v1', credential_values_redacted=True,
            mutable=False, **legacy), legacy)

    def test_supervisor_projection_preserves_startup_binding_without_metrics_dependency(self):
        expected = cp.binding(binding.policy_bytes(self.initial, 'a'*64))
        observed = dict(profile='q4t-gateway-clients-v1', credential_values_redacted=True, **expected)
        owner = dict(executable_sha256='b'*64, listener_scope='loopback')
        def get(_port, path, **_kwargs):
            self.assertNotEqual(path, '/metrics')
            value = (dict(observability_version=4, client_policy=observed)
                if path == '/v1/config' else dict(ready=True, live=True, status='ready'))
            return dict(http_status=200, body=json.dumps(value))
        with mock.patch.object(supervisor.identity, 'process', return_value=owner), \
             mock.patch.object(supervisor.transport, 'get', side_effect=get), \
             mock.patch.object(supervisor.contracts, 'configuration',
                               return_value=dict(protocol_sha256='c'*64)):
            result = supervisor.probe(42, 18181, include_metrics=False)
        cp.observed_matches(result['client_policy'], expected)
        self.assertIsNone(result['metrics'])


if __name__ == '__main__':unittest.main()
