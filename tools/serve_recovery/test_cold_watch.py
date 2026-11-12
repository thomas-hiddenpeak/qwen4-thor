"""Owned CPU-only cold issuer crashes, bounded recovery and retained custody."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, cold_custody as custody, cold_host, cold_issuer
from tools.serve_recovery import cold_watch, cold_watch_policy as policy, device_domain as domain
from tools.serve_recovery import ledger, store
from tools.serve_recovery import test_cold_issuer as fixtures


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(service_id=str(uuid.uuid4()), cpu=True)
        self.boot = str(uuid.uuid4()); self.seed = policy.initial(self.config, ledger.Stamp(self.boot, 1))

    def stamp(self, seconds, boot=None): return ledger.Stamp(boot or self.boot, 1+int(seconds*ledger.SECOND))

    def test_rate_limit_cooldown_and_normal_expiry_without_model_actions(self):
        value = self.seed
        for second in (0, 5, 10):
            value = policy.charge(value, self.stamp(second), str(uuid.uuid4()))
            decision = policy.decision(value, self.stamp(second+1))
            self.assertFalse(decision['allowed']); self.assertFalse(decision['model_actions'])
        self.assertEqual(policy.decision(value, self.stamp(15))['reason'], 'auxiliary_start_limit')
        self.assertFalse(policy.decision(value, self.stamp(899))['allowed'])
        fourth = policy.charge(value, self.stamp(900), str(uuid.uuid4()))
        self.assertEqual(fourth['total'], 4); self.assertEqual(len(fourth['starts']), 3)

    def test_new_boot_and_backward_clock_never_refund_history(self):
        value = policy.charge(self.seed, self.stamp(10), str(uuid.uuid4()))
        reboot = str(uuid.uuid4())
        self.assertFalse(policy.decision(value, self.stamp(100000, reboot))['allowed'])
        with self.assertRaisesRegex(ValueError, 'regressed'): policy.decision(value, self.stamp(9))
        changed = policy.charge(value, self.stamp(15), str(uuid.uuid4()))
        with self.assertRaises(ValueError): policy.transition(value, dict(changed, total=0))


class WatchTests(fixtures.AuthorityTests):
    def wait_status(self, predicate, timeout=8):
        end = time.monotonic()+timeout; last = None
        while time.monotonic() < end:
            try:
                last = cold_watch.control(self.host, 'status')
                if predicate(last): return last
            except (OSError, ValueError): pass
            time.sleep(.025)
        self.fail('owned CPU watcher deadline: '+repr(last))

    def issuer(self, different=None):
        end = time.monotonic()+8
        while time.monotonic() < end:
            try:
                result = cold_issuer.control(self.host, 'status')
                if result['issuer_pid'] != different: return result
            except (OSError, ValueError): pass
            time.sleep(.025)
        self.fail('owned CPU issuer readiness deadline')

    def kill_owned(self, pid):
        fd = os.pidfd_open(pid)
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            self.assertTrue(select.select([fd], [], [], 3)[0])
        finally: os.close(fd)

    def history(self):
        binding = access_store.binding(self.device, self.sid)
        with access_store.open_bound(binding, policy.Store) as writer:
            return deepcopy(writer.head['state']['policy'])

    def test_actual_issuer_sigkill_recovers_and_preserves_live_peer_core_and_device(self):
        self.start_peer(); before = self.state_files(); replay = self.device/'cold-outer-requests/policy.json'
        previous_replay = replay.read_bytes(); start = cold_host.ensure(self.host)
        first = start['issuer']['issuer_pid']; self.kill_owned(first)
        second = self.issuer(first); watch = self.wait_status(lambda v: v['charged_starts'] == 2)
        self.assertNotEqual(second['issuer_pid'], first)
        self.assertEqual(watch['watcher_pid'], start['watcher']['watcher_pid'])
        self.assertEqual(before, self.state_files()); self.assertEqual(previous_replay, replay.read_bytes())
        self.assertIsNone(self.peer.poll()); self.assertEqual(domain.status(self.device)['charged_total'], 0)

    def test_three_actual_crashes_enter_durable_limit_and_ensure_cannot_reset(self):
        cold_host.ensure(self.host)
        for count in range(1, 4):
            pid = self.issuer()['issuer_pid']; self.kill_owned(pid)
            if count < 3: self.issuer(pid)
        status = self.wait_status(lambda v: v['reason'] == 'auxiliary_start_limit')
        self.assertEqual(status['charged_starts'], 3); before = self.history()
        repeated = cold_host.ensure(self.host)
        self.assertFalse(repeated['available']); self.assertEqual(before, self.history())
        self.assertIsNone(repeated['watcher']['issuer_pid'])

    def test_watcher_loss_keeps_issuer_and_new_watcher_waits_for_original_ofd(self):
        started = cold_host.ensure(self.host); issuer = started['issuer']['issuer_pid']
        self.kill_owned(started['watcher']['watcher_pid'])
        replacement = cold_host.ensure(self.host)
        self.assertEqual(replacement['issuer']['issuer_pid'], issuer)
        status = self.wait_status(lambda v: v['reason'] == 'existing_auxiliary_custody')
        self.assertIsNone(status['issuer_pid']); self.assertEqual(self.history()['total'], 1)
        self.kill_owned(issuer)
        self.assertNotEqual(self.issuer(issuer)['issuer_pid'], issuer)
        self.assertEqual(self.history()['total'], 2)

    def test_native_watch_stop_cleans_auxiliary_tree_and_never_restarts(self):
        started = cold_host.ensure(self.host); ids = [started['watcher']['watcher_pid'], started['issuer']['issuer_pid']]
        fds = [os.pidfd_open(pid) for pid in ids]
        try:
            self.assertTrue(cold_host.withdraw(self.host)['issuer_exited'])
            self.assertEqual(set(select.select(fds, [], [], 0)[0]), set(fds))
            fd = custody.acquire(self.device, self.config); os.close(fd)
            with self.assertRaises(ConnectionRefusedError): cold_watch.control(self.host, 'status')
            with self.assertRaises(ConnectionRefusedError): cold_issuer.control(self.host, 'status')
            self.assertEqual(self.history()['total'], 1)
        finally:
            for fd in fds: os.close(fd)

    def test_lost_published_history_never_reinitializes_or_spawns(self):
        marker = self.device/policy.Store.marker_name; backup = marker.read_bytes(); marker.unlink()
        try:
            result = cold_host.ensure(self.host)
            self.assertFalse(result['available']); self.assertIsNone(result['watcher']['issuer_pid'])
            self.assertEqual(result['watcher']['reason'], 'auxiliary_history_unavailable')
            with self.assertRaises(FileNotFoundError): domain.resume_initialize(self.device)
        finally: marker.write_bytes(backup); marker.chmod(0o600)

    def test_failed_budget_commit_cannot_dispatch_or_refund(self):
        before = self.history()
        with mock.patch.object(policy.Store, 'transact', side_effect=OSError('injected durable write failure')):
            with self.assertRaises(OSError): policy.attempt(self.device, self.config)
        self.assertEqual(self.history(), before)
        result = policy.attempt(self.device, self.config)
        self.assertTrue(result['allowed']); self.assertEqual(result['total'], 1)
        self.assertFalse(policy.attempt(self.device, self.config)['allowed'])

    def test_new_boot_checkpoint_allows_elapsed_cooldown_without_restart_refund(self):
        first = policy.attempt(self.device, self.config); self.assertEqual(first['total'], 1)
        before = self.history(); reboot = str(uuid.uuid4()); now = ledger.Stamp(reboot, 100)
        with mock.patch.object(policy.store, 'stamp', return_value=now):
            result = policy.attempt(self.device, self.config)
        self.assertFalse(result['allowed']); changed = self.history()
        self.assertEqual(changed['starts'], before['starts']); self.assertEqual(changed['total'], before['total'])
        self.assertEqual(changed['clock']['boot_id'], reboot)
        self.assertEqual(changed['clock']['logical_ns'], before['clock']['logical_ns'])
        with mock.patch.object(policy.store, 'stamp', return_value=ledger.Stamp(reboot, 100+policy.COOLDOWN)):
            result = policy.attempt(self.device, self.config)
        self.assertTrue(result['allowed']); self.assertEqual(result['total'], 2)
        self.assertEqual(self.history()['starts'][0], before['starts'][0])

    def test_actual_auxiliary_child_holds_custody_after_parent_fd_close(self):
        fd = custody.acquire(self.device, self.config)
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c',
            'import os,sys,time;os.fstat(int(sys.argv[1]));print("held",flush=True);time.sleep(8)', str(fd)],
            pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 2)[0]); self.assertEqual(process.stdout.readline(), b'held\n')
            os.close(fd); fd = -1
            with self.assertRaises(BlockingIOError): custody.acquire(self.device, self.config)
        finally:
            if fd >= 0: os.close(fd)
            process.terminate(); process.wait(timeout=3)
        held = custody.acquire(self.device, self.config); os.close(held)


if __name__ == '__main__': unittest.main(module=None)
