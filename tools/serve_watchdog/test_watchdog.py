"""Adversarial receiver/state contracts; no network or GPU work."""
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.serve_watchdog import core as c, watchdog as w
from tools.serve_watchdog.storage import Directory, encoded, sha

BOOT = '11111111-2222-3333-4444-555555555555'
OTHER_BOOT = '22222222-2222-3333-4444-555555555555'
DIGEST = 'e'*64


def clock(seconds=100, unix=1800000000, boot=BOOT):
    return dict(boot_id=boot, boottime=seconds, unix_seconds=unix)


def payload(status='firing', start='2026-11-04T00:00:00Z'):
    return dict(version='4', receiver='q4t-webhook', status=status,
        truncatedAlerts=0, alerts=[dict(status=status,
            labels=dict(alertname='Q4TNotifyHeartbeat', component='q4t-monitor',
                deployment='thor-test', severity='information'),
            startsAt=start, endsAt='0001-01-01T00:00:00Z' if
                status == 'firing' else '2026-11-04T00:05:00Z',
            fingerprint='1234567890abcdef')])


def alert(**kwargs):
    return c.heartbeat(encoded(payload(**kwargs)), 'thor-test')


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.state = c.new_state(DIGEST, clock())

    def test_never_received_expires_at_exact_boundary(self):
        self.assertEqual(c.status(self.state, clock(279.999))['state'], 'WAITING')
        self.assertEqual(c.status(self.state, clock(280))['state'], 'MISSING')

    def test_arrival_and_byte_identical_repeats_refresh_deadline(self):
        a = alert()
        state, _ = c.receive(self.state, a, clock(150))
        self.assertEqual(c.status(state, clock(329.999))['state'], 'PRESENT')
        self.assertEqual(c.status(state, clock(330))['state'], 'MISSING')
        newer, _ = c.receive(state, a, clock(320))
        self.assertEqual(c.status(newer, clock(400))['state'], 'PRESENT')
        self.assertEqual(newer['receipt']['payload_sha256'],
                         state['receipt']['payload_sha256'])
        self.assertEqual(newer['accepted'], 2)

    def test_wall_clock_changes_do_not_extend_or_expire_liveness(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        self.assertEqual(c.status(state, clock(200, unix=10))['state'], 'PRESENT')
        self.assertEqual(c.status(state, clock(200, unix=2**40))['state'], 'PRESENT')
        self.assertEqual(c.status(state, clock(330, unix=10))['state'], 'MISSING')

    def test_receiver_reboot_unknown_until_new_authenticated_delivery(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        self.assertEqual(c.status(state, clock(2, boot=OTHER_BOOT))['state'], 'UNKNOWN')
        state, _ = c.receive(state, alert(), clock(3, boot=OTHER_BOOT))
        self.assertEqual(c.status(state, clock(4, boot=OTHER_BOOT))['state'], 'PRESENT')

    def test_regressed_boottime_unknown_and_receive_refused(self):
        with self.assertRaisesRegex(c.ContractError, 'clock_regressed'):
            c.receive(self.state, alert(), clock(99))
        state, _ = c.receive(self.state, alert(), clock(150))
        self.assertEqual(c.status(state, clock(149))['reason'], 'clock_regressed')
        with self.assertRaisesRegex(c.ContractError, 'clock_regressed'):
            c.receive(state, alert(), clock(149))

    def test_resolution_immediate_and_same_incident_reactivation(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        state, _ = c.receive(state, alert(status='resolved'), clock(151))
        self.assertEqual(c.status(state, clock(151))['state'], 'RESOLVED')
        state, _ = c.receive(state, alert(), clock(152))
        self.assertEqual(c.status(state, clock(152))['state'], 'PRESENT')

    def test_old_incident_does_not_refresh_or_resolve_current(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        for status in ('firing', 'resolved'):
            result, disposition = c.receive(state, alert(status=status,
                start='2026-11-03T23:59:59.999999999Z'), clock(350))
            self.assertEqual(disposition, 'ignored_stale_incident')
            self.assertEqual(result['receipt'], state['receipt'])
            self.assertEqual(c.status(result, clock(350))['state'], 'MISSING')

    def test_outage_recorded_even_without_periodic_observer(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        state, _ = c.receive(state, alert(), clock(400))
        self.assertEqual([e['state'] for e in state['history']],
                         ['WAITING', 'PRESENT', 'MISSING', 'PRESENT'])

    def test_transitions_bounded_while_counts_preserve_drops(self):
        state = self.state
        for i in range(100):
            state, _ = c.receive(state, alert(), clock(150+i*200))
        c.validate(state, DIGEST)
        self.assertEqual(len(state['history']), 64)
        self.assertEqual(state['accepted'], 100)
        self.assertGreater(state['transitions'], 64)
        self.assertLess(len(encoded(state)), c.MAXIMUM)
        view = c.status(state, clock(20150))
        self.assertEqual(view['history_dropped'], state['transitions']-64)

    def test_duplicate_json_nonfinite_and_huge_input_rejected(self):
        for raw in (b'{"version":"4","version":"4"}', b'{"x":NaN}',
                    b'['*10000, b' '*65537, b'\xff', b'1e999'):
            with self.subTest(raw=raw[:50]):
                with self.assertRaises(c.ContractError):
                    c.heartbeat(raw, 'thor-test')

    def test_wrong_envelope_and_ordinary_alerts_rejected(self):
        for field, value in [('version', 4), ('receiver', 'untrusted'),
                             ('truncatedAlerts', True), ('truncatedAlerts', 1),
                             ('status', 'bad'), ('alerts', [])]:
            body = payload(); body[field] = value
            with self.subTest(field=field), self.assertRaises(c.ContractError):
                c.heartbeat(encoded(body), 'thor-test')
        for field, value in [('deployment', 'other'), ('alertname', 'Q4TTargetDown'),
                             ('severity', 'warning'), ('new', 'extra')]:
            body = payload(); body['alerts'][0]['labels'][field] = value
            with self.subTest(label=field), self.assertRaises(c.ContractError):
                c.heartbeat(encoded(body), 'thor-test')

    def test_mixed_batch_and_invalid_times_or_fingerprint_rejected(self):
        body = payload(); body['alerts'] *= 2
        with self.assertRaises(c.ContractError):
            c.heartbeat(encoded(body), 'thor-test')
        for field, value in [('startsAt', None), ('startsAt', '2026-02-31T00:00:00Z'),
                ('endsAt', '2020-01-01T00:00:00Z'), ('fingerprint', '../a'),
                ('status', 'resolved')]:
            body = payload(); body['alerts'][0][field] = value
            with self.subTest(field=field), self.assertRaises(c.ContractError):
                c.heartbeat(encoded(body), 'thor-test')

    def test_timestamp_timezone_and_nanoseconds_preserved(self):
        a = c.timestamp('2026-11-04T08:00:00.000000001+08:00')
        b = c.timestamp('2026-11-04T00:00:00Z')
        self.assertEqual(a-b, 1)

    def test_corrupt_or_foreign_state_never_treated_healthy(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        mutations = [('policy_sha256', 'f'*64), ('accepted', True),
            ('history', []), ('version', True), ('created', {}),
            ('receipt', None), ('ignored', -1)]
        for field, value in mutations:
            bad = dict(state, **{field:value})
            with self.subTest(field=field), self.assertRaises(c.ContractError):
                c.validate(bad, DIGEST)
        for value in (float('inf'), -1, True, 2**9999):
            with self.assertRaises(c.ContractError):
                c.clock(clock(value))

    def test_metrics_are_bounded_and_no_credential_or_payload(self):
        state, _ = c.receive(self.state, alert(), clock(150))
        text = c.metrics(c.status(state, clock(330)), 'thor-test')
        self.assertIn('state="MISSING"} 1', text)
        self.assertNotIn('1234567890abcdef', text)
        self.assertLess(len(text), 4096)
        with self.assertRaises(c.ContractError):
            c.metrics(c.status(state, clock(330)), 'bad"label')


class StorageTests(unittest.TestCase):
    def setUp(self):
        parent = w.ROOT/'build/serve-watchdog-tests'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.root.chmod(0o700)
        self.token = secrets.token_urlsafe(32)
        self.key = self.root/'token-private'
        self.key.write_text(self.token); self.key.chmod(0o600)
        self.state = self.root/'state'
        self.receipt = w.initialize(self.state, 'thor-test', self.key)
        self.digest = self.receipt['policy_sha256']
        self.raw, self.auth = encoded(payload()), 'Bearer '+self.token
        self.entry = self.state/'source/tools/serve_watchdog/watchdog.py'

    def argv(self, action):
        return [str(Path(sys.executable).resolve()), '-I', '-B', str(self.entry),
                action, '--state', str(self.state),
                '--policy-sha256', self.digest]

    def ledger(self):
        return (self.state/'ledger.json').read_bytes()

    def test_wrong_auth_or_payload_cannot_refresh_and_no_secret_in_result(self):
        before = self.ledger()
        for auth in ('', self.auth+'x', 'Bearer ü'):
            with self.assertRaisesRegex(c.ContractError, 'unauthorized'):
                w.ingest(self.state, self.digest, self.raw, auth)
        body = payload(); body['alerts'][0]['labels']['alertname'] = 'Q4TTargetDown'
        with self.assertRaises(c.ContractError):
            w.ingest(self.state, self.digest, encoded(body), self.auth)
        self.assertEqual(self.ledger(), before)
        result = w.ingest(self.state, self.digest, self.raw, self.auth)
        self.assertTrue(result['durable'])
        self.assertTrue(result['status']['healthy'])
        self.assertNotIn(self.token, json.dumps(result))
        self.assertNotIn(self.token, self.ledger().decode())

    def test_existing_state_and_public_or_linked_secret_rejected(self):
        before = self.ledger()
        with self.assertRaises(FileExistsError):
            w.initialize(self.state, 'thor-test', self.key)
        self.key.chmod(0o644)
        with self.assertRaises(c.ContractError):
            w.initialize(self.root/'other', 'thor-test', self.key)
        self.key.chmod(0o600)
        link = self.root/'key-link'; link.symlink_to(self.key)
        with self.assertRaises(c.ContractError):
            w.initialize(self.root/'other', 'thor-test', link)
        self.assertFalse((self.root/'other').exists())
        self.assertEqual(self.ledger(), before)

    def test_hardlinks_symlinks_and_nonprivate_state_rejected(self):
        ledger = self.state/'ledger.json'; original = self.ledger()
        os.link(ledger, self.root/'hardlink')
        with self.assertRaises(c.ContractError):
            w.inspect(self.state, self.digest)
        (self.root/'hardlink').unlink()
        ledger.rename(self.root/'saved-ledger')
        ledger.symlink_to(self.root/'saved-ledger')
        with self.assertRaises(OSError):
            w.inspect(self.state, self.digest)
        self.assertEqual((self.root/'saved-ledger').read_bytes(), original)
        self.state.chmod(0o755)
        with self.assertRaises(c.ContractError):
            w.inspect(self.state, self.digest)

    def test_durable_write_failure_keeps_original_and_pending_file(self):
        before = self.ledger()
        with mock.patch('tools.serve_watchdog.storage.os.fsync',
                        side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                w.ingest(self.state, self.digest, self.raw, self.auth)
        pending = self.state/'ledger.json.new'
        self.assertTrue(pending.exists())
        self.assertEqual(self.ledger(), before)
        saved = pending.read_bytes()
        with self.assertRaises(FileExistsError):
            w.ingest(self.state, self.digest, self.raw, self.auth)
        self.assertEqual(pending.read_bytes(), saved)
        self.assertFalse(w.inspect(self.state, self.digest)['healthy'])

    def test_source_policy_credential_or_state_tamper_is_unknown(self):
        variants = [('ledger.json', b'{}'), ('policy.json', b'{}'),
                    ('token-private', b'wrong')]
        for name, bad in variants:
            path = self.state/name; old = path.read_bytes()
            path.write_bytes(bad)
            with self.subTest(name=name), self.assertRaises(c.ContractError):
                w.inspect(self.state, self.digest)
            path.write_bytes(old)
        path = self.state/'source/tools/serve_watchdog/core.py'
        path.chmod(0o600); path.write_bytes(b'changed')
        with self.assertRaisesRegex(c.ContractError, 'pinned_source'):
            w.inspect(self.state, self.digest)

    def test_repeated_and_concurrent_ingestion_cannot_lose_counts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: w.ingest(self.state, self.digest,
                self.raw, self.auth), range(16)))
        self.assertTrue(all(r['durable'] for r in rows))
        self.assertEqual(w.inspect(self.state, self.digest)['accepted'], 16)

    def test_actual_child_ingest_with_private_fd_and_isolated_entry(self):
        fd = os.open(self.key, os.O_RDONLY)
        # Pass the header through a pipe, not argv/environment.
        os.close(fd)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, self.auth.encode()); os.close(write_fd)
        try:
            result = subprocess.run([*self.argv('ingest'),
                '--authorization-fd', str(read_fd)], input=self.raw,
                capture_output=True, pass_fds=(read_fd,), timeout=10)
        finally:
            os.close(read_fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['durable'])
        self.assertNotIn(self.token.encode(), result.stdout + result.stderr)

    def test_claim_content_and_other_process_exclusion(self):
        with Directory(self.state) as directory, directory.claim():
            result = subprocess.run(self.argv('watch'), capture_output=True,
                                    text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['error'], 'state_busy')
        (self.state/'state.lock').write_text('unexpected')
        with self.assertRaisesRegex(c.ContractError, 'claim_content'):
            w.inspect(self.state, self.digest, persist=True)

    def test_status_is_readonly_even_without_claim_files(self):
        before = {p.name:p.read_bytes() for p in self.state.iterdir()
                  if p.is_file()}
        self.assertEqual(w.inspect(self.state, self.digest)['state'], 'WAITING')
        self.assertEqual(before, {p.name:p.read_bytes() for p in
                                 self.state.iterdir() if p.is_file()})

    def test_readonly_status_does_not_reset_expiry_and_watcher_restart(self):
        state = c.parse(self.ledger())
        state['created']['boottime'] -= 181
        (self.state/'ledger.json').write_bytes(encoded(state))
        before = self.ledger()
        self.assertEqual(w.inspect(self.state, self.digest)['state'], 'MISSING')
        self.assertEqual(self.ledger(), before)
        for _ in range(2):
            child = subprocess.Popen(self.argv('watch'), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            try:
                until = time.monotonic()+5
                while not (self.state/'metrics.prom').exists():
                    self.assertLess(time.monotonic(), until)
                    self.assertIsNone(child.poll())
                    time.sleep(.02)
                # Probe the persistent watch lock; a previous output file
                # alone is not proof that this process acquired the claim.
                with Directory(self.state) as directory:
                    while True:
                        try:
                            with directory.claim('watch.lock', 0): pass
                        except c.ContractError:
                            break
                        self.assertLess(time.monotonic(), until)
                        time.sleep(.02)
                other = subprocess.run(self.argv('watch'), capture_output=True,
                                       text=True, timeout=5)
                self.assertEqual(other.returncode, 1)
            finally:
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
            self.assertEqual(w.inspect(self.state, self.digest)['state'], 'MISSING')
        with Directory(self.state) as directory, directory.claim('watch.lock', 0):
            pass

    def test_fixed_outputs_and_unit_no_automatic_restart(self):
        w.inspect(self.state, self.digest, persist=True)
        before = sorted(p.name for p in self.state.iterdir())
        for _ in range(4): w.inspect(self.state, self.digest, persist=True)
        self.assertEqual(before, sorted(p.name for p in self.state.iterdir()))
        text = w.unit(self.state, self.digest)
        unit = self.root/'watchdog.service'; unit.write_text(text)
        check = subprocess.run(['systemd-analyze', '--user', 'verify', unit],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn('Restart=no', text)
        self.assertNotIn('[Install]', text)
        self.assertNotIn(self.token, text)


if __name__ == '__main__':
    unittest.main()
