"""Virtual-clock and actual-disk contracts for persistent restart authority."""
from copy import deepcopy
import errno
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import ledger as l
from tools.serve_recovery import store

BOOT = '12345678-1234-5678-1234-567812345678'
NEXT_BOOT = '22345678-1234-5678-1234-567812345678'
SERVICE = '32345678-1234-5678-1234-567812345678'
PROOF = {field: True for field in l.PROOFS}


class Clock:
    def __init__(self):
        self.boot = BOOT
        self.now = l.SECOND

    def __call__(self):
        return l.Stamp(self.boot, self.now)

    def advance(self, seconds):
        self.now += seconds*l.SECOND


def request(current, operation, /, **args):
    return dict(id=str(uuid.uuid4()), expected_revision=current['revision'],
                operation=operation, args=args)


class Machine:
    def __init__(self):
        self.clock = Clock()
        self.state = l.new_state(SERVICE, self.clock())

    def send(self, operation, **args):
        self.state, receipt = l.transition(self.state,
            request(self.state, operation, **args), self.clock())
        return receipt['result']

    def start(self, role):
        result = self.send('authorize', role=role, proof=PROOF)
        if not result['allowed']:
            return result
        ticket = result['grant']['ticket']
        self.send('spawned', role=role, ticket=ticket,
                  owner=dict(pid=123, start_ticks=456, boot_id=self.clock.boot))
        return result

    def retire(self, role):
        return self.send('retired', role=role, verified=True,
                          ticket=self.state['active'][role]['ticket'])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.m.send('desired', state='RUNNING')
        self.m.start('controller')

    def fault(self, label='same', unknown=False):
        return self.m.send('fault',
            generation=self.m.state['generations']['model'],
            fingerprint=l.digest(label), unknown=unknown)

    def replace_model(self, delay=1):
        if self.m.state['active']['model'] is not None:
            self.m.retire('model')
        self.m.clock.advance(delay)
        return self.m.start('model')

    def test_initial_start_is_single_and_replacement_is_charged_before_spawn(self):
        result = self.m.send('authorize', role='model', proof=PROOF)
        self.assertTrue(result['allowed'])
        self.assertEqual(self.m.state['attempts']['model'], [])
        self.assertEqual(self.m.send('authorize', role='model', proof=PROOF)
                         ['reason'], 'unreconciled_grant')
        self.m.retire('model')
        self.m.send('authorize', role='model', proof=PROOF)
        self.assertEqual(len(self.m.state['attempts']['model']), 1)
        self.assertEqual(self.m.state['active']['model']['phase'], 'authorized')

    def test_three_replacements_per_30_minutes_then_cooldown(self):
        self.m.start('model')
        for _ in range(3):
            self.assertTrue(self.replace_model()['allowed'])
        result = self.replace_model()
        self.assertEqual(result['reason'], 'budget_exhausted')
        deadline = result['retry_at_logical_ns']
        for _ in range(10):
            self.m.clock.advance(1)
            result = self.m.send('authorize', role='model', proof=PROOF)
            self.assertEqual(result['retry_at_logical_ns'], deadline)
        self.m.clock.advance(1800)
        self.assertTrue(self.m.start('model')['allowed'])

    def test_six_replacements_per_day_survive_short_window_expiry(self):
        self.m.start('model')
        for _ in range(2):
            for _ in range(3):
                self.assertTrue(self.replace_model()['allowed'])
            self.m.clock.advance(31*60)
        self.assertEqual(self.replace_model()['reason'], 'budget_exhausted')
        self.m.clock.advance(24*60*60)
        self.assertTrue(self.m.start('model')['allowed'])

    def test_controller_budget_independent_and_model_count_survives(self):
        self.m.start('model')
        self.replace_model()
        self.m.retire('model')
        for _ in range(3):
            self.m.retire('controller')
            self.assertTrue(self.m.start('controller')['allowed'])
        self.m.retire('controller')
        self.assertEqual(self.m.start('controller')['reason'], 'budget_exhausted')
        self.assertEqual(len(self.m.state['attempts']['model']), 1)
        self.assertEqual(self.m.state['generations']['model'], 2)

    def test_controller_cannot_start_over_an_unreaped_model(self):
        self.m.start('model')
        with self.assertRaisesRegex(l.LedgerError, 'subtree'):
            self.m.retire('controller')
        self.assertIsNotNone(self.m.state['active']['controller'])

    def test_stop_then_start_does_not_reset_model_budget(self):
        self.m.start('model')
        self.replace_model()
        self.m.send('desired', state='STOPPED')
        self.m.retire('model')
        result = self.m.start('model')
        self.assertEqual(result['reason'], 'operator_intent')
        self.m.send('desired', state='RUNNING')
        self.m.start('model')
        self.assertEqual(len(self.m.state['attempts']['model']), 2)

    def test_stop_after_grant_blocks_ready_and_does_not_refund_ticket(self):
        result = self.m.send('authorize', role='model', proof=PROOF)
        self.m.send('desired', state='STOPPED')
        # A registration acknowledges a child already spawned before STOP.
        # It is not a second permission to launch from a stale grant.
        self.m.send('spawned', role='model', ticket=result['grant']['ticket'],
            owner=dict(pid=123, start_ticks=456, boot_id=BOOT))
        with self.assertRaisesRegex(l.LedgerError, 'admission'):
            self.m.send('ready', role='model', verified=True,
                         ticket=result['grant']['ticket'])
        self.m.retire('model')
        self.assertEqual(self.m.state['generations']['model'], 1)

    def test_same_fault_in_two_generations_latches_quarantine(self):
        self.m.start('model')
        self.fault()
        self.assertTrue(self.replace_model(delay=5)['allowed'])
        self.assertEqual(self.fault()['quarantine'], 'repeated_model_fault')
        self.m.retire('model')
        self.m.clock.advance(24*60*60)
        self.assertEqual(self.m.start('model')['reason'], 'quarantined')
        self.m.send('desired', state='STOPPED')
        self.m.send('desired', state='RUNNING')
        self.assertEqual(self.m.start('model')['reason'], 'quarantined')

    def test_different_fault_fingerprints_still_limit_unknown_exit_to_one_probe(self):
        self.m.start('model')
        self.fault('first', unknown=True)
        self.replace_model(delay=5)
        self.assertEqual(self.fault('different', unknown=True)['quarantine'],
                         'unknown_exit_repeated')

    def test_duplicate_fault_does_not_consume_second_generation(self):
        self.m.start('model')
        self.fault()
        self.assertEqual(self.fault()['reason'], 'duplicate_fault')
        self.assertEqual(len(self.m.state['failures']), 1)
        self.assertIsNone(self.m.state['quarantine'])

    def test_stale_fault_does_not_affect_current_generation(self):
        self.m.start('model')
        self.replace_model()
        result = self.m.send('fault', generation=1, fingerprint=l.digest('old'),
                             unknown=True)
        self.assertEqual(result['reason'], 'stale_generation')
        self.assertEqual(self.m.state['failures'], [])

    def test_fault_backoff_is_five_twenty_sixty_seconds(self):
        self.m.start('model')
        for index, seconds in enumerate((5, 20, 60)):
            self.fault(str(index))
            self.m.retire('model')
            self.m.clock.advance(seconds-1)
            self.assertEqual(self.m.start('model')['reason'], 'cooldown')
            self.m.clock.advance(1)
            self.assertTrue(self.m.start('model')['allowed'])

    def test_boot_change_keeps_remaining_windows_and_does_not_assume_downtime(self):
        self.m.start('model')
        for _ in range(3):
            self.replace_model()
        self.m.retire('model')
        self.m.clock.boot = NEXT_BOOT
        self.m.clock.now = l.SECOND  # A reboot, possibly after a long outage.
        self.m.retire('controller')
        self.m.start('controller')
        self.assertEqual(self.m.start('model')['reason'], 'budget_exhausted')
        self.assertEqual(len(self.m.state['attempts']['model']), 3)

    def test_clock_rollback_freezes_without_modifying_state(self):
        previous = deepcopy(self.m.state)
        self.m.clock.now -= 1
        with self.assertRaisesRegex(l.LedgerError, 'backwards'):
            self.m.start('model')
        self.assertEqual(self.m.state, previous)

    def test_wall_clock_is_not_budget_input(self):
        self.m.start('model')
        for _ in range(3):
            self.replace_model()
        self.m.retire('model')
        with mock.patch('time.time', return_value=10**18):
            self.assertEqual(self.m.start('model')['reason'], 'budget_exhausted')

    def test_stability_closes_incident_without_clearing_budget(self):
        self.m.start('model')
        self.fault()
        self.replace_model(delay=5)
        ticket = self.m.state['active']['model']['ticket']
        self.m.send('ready', role='model', ticket=ticket, verified=True)
        with self.assertRaisesRegex(l.LedgerError, 'stability'):
            self.m.send('stable', role='model', ticket=ticket,
                         successful_request=True)
        self.m.clock.advance(600)
        with self.assertRaisesRegex(l.LedgerError, 'stability'):
            self.m.send('stable', role='model', ticket=ticket,
                         successful_request=False)
        self.m.send('stable', role='model', ticket=ticket,
                     successful_request=True)
        self.assertEqual(self.m.state['failures'], [])
        self.assertEqual(len(self.m.state['attempts']['model']), 1)

    def test_same_request_has_one_receipt_and_older_replay_fails(self):
        req = request(self.m.state, 'authorize', role='model', proof=PROOF)
        current, first = l.transition(self.m.state, req, self.m.clock())
        replayed, second = l.transition(current, req, self.m.clock())
        self.assertEqual(current, replayed)
        self.assertEqual(first, second)
        self.m.state = current
        self.m.send('desired', state='STOPPED')
        with self.assertRaisesRegex(l.LedgerError, 'stale ledger'):
            l.transition(self.m.state, req, self.m.clock())

    def test_request_id_reuse_with_changed_payload_is_rejected(self):
        req = request(self.m.state, 'authorize', role='model', proof=PROOF)
        state, _ = l.transition(self.m.state, req, self.m.clock())
        req['args']['role'] = 'gateway'
        with self.assertRaisesRegex(l.LedgerError, 'id reused'):
            l.transition(state, req, self.m.clock())

    def test_false_safety_proof_never_spends_or_grants(self):
        for field in PROOF:
            result = self.m.send('authorize', role='model',
                                 proof=dict(PROOF, **{field: False}))
            self.assertEqual(result['reason'], 'preflight_incomplete')
        self.assertEqual(self.m.state['generations']['model'], 0)

    def test_wrong_boot_or_twice_consumed_ticket_is_rejected(self):
        result = self.m.send('authorize', role='model', proof=PROOF)
        args = dict(role='model', ticket=result['grant']['ticket'],
                    owner=dict(pid=123, start_ticks=456, boot_id=NEXT_BOOT))
        with self.assertRaisesRegex(l.LedgerError, 'boot mismatch'):
            self.m.send('spawned', **args)
        args['owner']['boot_id'] = BOOT
        self.m.send('spawned', **args)
        with self.assertRaisesRegex(l.LedgerError, 'already consumed'):
            self.m.send('spawned', **args)

    def test_quarantine_needs_explicit_stopped_empty_acknowledgement(self):
        self.m.send('quarantine', reason='device_failure')
        with self.assertRaisesRegex(l.LedgerError, 'stopped empty'):
            self.m.send('acknowledge', reason='device_failure')
        self.m.send('desired', state='STOPPED')
        self.m.retire('controller')
        with self.assertRaisesRegex(l.LedgerError, 'reason changed'):
            self.m.send('acknowledge', reason='cleanup_failed')
        self.m.send('acknowledge', reason='device_failure')
        self.assertEqual(self.m.state['desired'], 'STOPPED')
        self.assertEqual(self.m.state['generations']['controller'], 1)

    def test_invalid_schema_boolean_generation_and_unknown_field_are_rejected(self):
        for field, value in (('revision', True), ('policy', 'future'),
                             ('version', 2)):
            state = deepcopy(self.m.state)
            state[field] = value
            with self.assertRaises(l.LedgerError):
                l.validate(state, SERVICE)
        state = deepcopy(self.m.state)
        state['extra'] = 0
        with self.assertRaises(l.LedgerError):
            l.validate(state, SERVICE)


class StoreTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                    ROOT/'build/recovery-ledger-tests'))
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.directory = Path(self.temp.name)
        self.clock = Clock()
        self.writer = store.Store(self.directory, SERVICE, initialize=True,
                                  clock=self.clock)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def send(self, operation, **args):
        return self.writer.transact(request(self.writer.state, operation,
                                             **args))

    def reopen(self):
        self.writer.close()
        self.writer = store.Store(self.directory, SERVICE, clock=self.clock)

    def test_single_writer_and_service_identity_are_enforced(self):
        with self.assertRaises(BlockingIOError):
            store.Store(self.directory, SERVICE, clock=self.clock)
        self.writer.close()
        with self.assertRaisesRegex(l.LedgerError, 'identity mismatch'):
            store.Store(self.directory, str(uuid.uuid4()), clock=self.clock)

    def test_second_initialization_never_truncates_history(self):
        self.send('desired', state='RUNNING')
        before = (self.directory/store.STATE).read_bytes()
        self.writer.close()
        with self.assertRaises(FileExistsError):
            store.Store(self.directory, SERVICE, initialize=True,
                        clock=self.clock)
        self.assertEqual((self.directory/store.STATE).read_bytes(), before)

    def test_missing_ledger_is_not_a_new_service(self):
        self.writer.close()
        (self.directory/store.STATE).unlink()
        with self.assertRaises(FileNotFoundError):
            store.Store(self.directory, SERVICE, clock=self.clock)
        with self.assertRaises(FileExistsError):
            store.Store(self.directory, SERVICE, initialize=True,
                        clock=self.clock)

    def test_corrupt_ledger_and_checksum_mismatch_freeze(self):
        self.writer.close()
        path = self.directory/store.STATE
        for content in (b'{', b'{"sha256":"0","state":{}}',
                        b'{"sha256":"0","sha256":"1","state":{}}'):
            path.write_bytes(content)
            with self.assertRaises(l.LedgerError):
                store.Store(self.directory, SERVICE, clock=self.clock)

    def test_private_mode_symlink_and_hardlink_are_required(self):
        self.writer.close()
        path = self.directory/store.STATE
        path.chmod(0o644)
        with self.assertRaisesRegex(l.LedgerError, 'private regular'):
            store.Store(self.directory, SERVICE, clock=self.clock)
        path.chmod(0o600)
        copy = self.directory/'hardlink'
        copy.hardlink_to(path)
        with self.assertRaisesRegex(l.LedgerError, 'private regular'):
            store.Store(self.directory, SERVICE, clock=self.clock)
        copy.unlink()
        path.rename(copy)
        path.symlink_to(copy)
        with self.assertRaises(OSError):
            store.Store(self.directory, SERVICE, clock=self.clock)

    def test_commit_before_ack_survives_reopen_with_occupied_ticket(self):
        self.send('desired', state='RUNNING')
        receipt = self.send('authorize', role='controller', proof=PROOF)
        self.reopen()  # Simulate lost response/old Guardian before consumption.
        self.assertEqual(self.writer.state['active']['controller']['ticket'],
                         receipt['result']['grant']['ticket'])
        self.assertEqual(self.send('authorize', role='controller', proof=PROOF)
                         ['result']['reason'], 'unreconciled_grant')

    def test_complete_pending_transaction_is_charged_on_reopen(self):
        self.send('desired', state='RUNNING')
        with mock.patch.object(store.os, 'replace', side_effect=
                               OSError(errno.EIO, 'synthetic rename failure')):
            with self.assertRaises(OSError):
                self.send('authorize', role='controller', proof=PROOF)
        self.assertTrue(self.writer.poisoned)
        self.assertTrue((self.directory/store.PENDING).exists())
        self.reopen()
        self.assertEqual(self.writer.state['generations']['controller'], 1)
        self.assertEqual(self.send('authorize', role='controller', proof=PROOF)
                         ['result']['reason'], 'unreconciled_grant')

    def test_partial_pending_never_falls_back_to_old_free_budget(self):
        with mock.patch.object(store.os, 'write', side_effect=
                               OSError(errno.ENOSPC, 'synthetic full disk')):
            with self.assertRaises(OSError):
                self.send('desired', state='RUNNING')
        with self.assertRaisesRegex(l.LedgerError, 'frozen'):
            self.send('desired', state='RUNNING')
        self.writer.close()
        with self.assertRaisesRegex(l.LedgerError, 'invalid bounded JSON'):
            store.Store(self.directory, SERVICE, clock=self.clock)

    def test_post_rename_fsync_failure_never_returns_or_refunds_grant(self):
        self.send('desired', state='RUNNING')
        real_sync = store.os.fsync
        def fail_directory(fd):
            if fd == self.writer.dir_fd:
                raise OSError(errno.EIO, 'synthetic directory failure')
            return real_sync(fd)
        with mock.patch.object(store.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.send('authorize', role='controller', proof=PROOF)
        self.reopen()
        self.assertIsNotNone(self.writer.state['active']['controller'])
        self.assertEqual(self.writer.state['generations']['controller'], 1)

    def test_durable_stop_is_preserved_on_reopen(self):
        self.send('desired', state='RUNNING')
        self.send('desired', state='STOPPED')
        self.reopen()
        self.assertEqual(self.send('authorize', role='controller', proof=PROOF)
                         ['result']['reason'], 'operator_intent')

    def test_short_writes_are_completed_before_commit(self):
        real_write = store.os.write
        with mock.patch.object(store.os, 'write',
                               side_effect=lambda fd, data:
                               real_write(fd, data[:7])):
            self.send('desired', state='RUNNING')
        self.reopen()
        self.assertEqual(self.writer.state['desired'], 'RUNNING')

    def test_real_writer_crash_before_rename_keeps_spent_authorization(self):
        self.send('desired', state='RUNNING')
        self.writer.close()
        child = os.fork()
        if child == 0:
            try:
                with store.Store(self.directory, SERVICE,
                                 clock=self.clock) as writer:
                    with mock.patch.object(store.os, 'replace',
                                           side_effect=lambda *_a, **_k:
                                           os._exit(77)):
                        writer.transact(request(writer.state, 'authorize',
                                                role='controller', proof=PROOF))
            finally:
                os._exit(99)
        deadline = time.monotonic()+3
        reaped = False
        try:
            while time.monotonic() < deadline:
                pid, status = os.waitpid(child, os.WNOHANG)
                if pid:
                    reaped = True
                    self.assertEqual(os.waitstatus_to_exitcode(status), 77)
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, 'CPU journal child did not exit')
        finally:
            if not reaped:
                import signal
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
        self.reopen()
        self.assertEqual(self.writer.state['generations']['controller'], 1)
        self.assertEqual(self.send('authorize', role='controller', proof=PROOF)
                         ['result']['reason'], 'unreconciled_grant')


if __name__ == '__main__':
    unittest.main()
