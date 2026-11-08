"""CPU fault evidence, actual native wire and recovery integration contracts."""
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import struct
import unittest
from unittest import mock

from tools.serve_recovery import controller, observation_io, policy
from tools.serve_recovery import progress as p

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = p.Identity(123, 456, 789, 3, 7,
    '00000000-0000-4000-8000-000000000001',
    '00000000-0000-4000-8000-000000000002')


def row(**changes):
    return replace(p.Work(1, 100*p.NS, 6, p.NS, 4, p.NS, 6, 1,
                          p.NS, 0, 0, p.NS), **changes)


def sample(sequence=1, seconds=131, rows=None, **changes):
    rows = (row(),) if rows is None else rows
    return replace(p.Snapshot(sequence, seconds*p.NS, True, IDENTITY,
                             len(rows), 0, 1, 0, rows), **changes)


def packet(value):
    identity = value.identity
    return p.HEADER.pack(b'Q4TPR001' if value.wire_version == 1 else b'Q4TPR002',
        value.sequence, value.observed_ns,
        int(value.complete), identity.pid, identity.start_ticks,
        identity.pid_namespace, identity.controller_generation,
        identity.model_generation, value.active, value.untracked,
        len(value.rows), value.successes, value.steps,
        identity.boot_id.encode(), identity.service_id.encode()) + b''.join(
            p.ROW.pack(*vars(work).values()) for work in value.rows)


class ProgressTests(unittest.TestCase):
    def reader(self):
        receiving, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        value = p.Reader(receiving, IDENTITY)
        self.addCleanup(value.close)
        self.addCleanup(peer.close)
        return value, peer

    def send(self, reader, peer, value, now=None):
        peer.send(packet(value))
        return reader.poll(value.observed_ns if now is None else now)

    def test_actual_cpp_wire_not_a_python_roundtrip(self):
        data = (ROOT/'docs/evidence/serve-progress-native-20261109/'
                'integration-cpu-01/work/native-progress.bin').read_bytes()
        self.assertEqual(len(data), 280)
        value = p.decode(data)
        self.assertEqual((value.sequence, value.observed_ns, value.complete),
                         (9, 5, True))
        self.assertEqual((value.successes, value.steps, value.active), (2, 7, 1))
        self.assertEqual(value.identity.controller_generation, 3)
        self.assertEqual(value.identity.model_generation, 7)
        self.assertEqual(value.identity.service_id,
                         '00000000-0000-4000-8000-000000000001')
        self.assertEqual((value.rows[0].work, value.rows[0].completed,
                          value.rows[0].last_completed_ns), (6, 1, 4))

    def test_wire_bounds_enums_clocks_duplicates_and_completeness(self):
        raw = packet(sample())
        cases = [raw[:183], raw[:-1], raw+b'x', b'badmagic'+raw[8:]]
        for index, value in ((0, 0), (1, 0), (2, 2), (3, 0), (10, 1025)):
            changed = bytearray(raw)
            struct.pack_into('<Q', changed, 8+8*index, value)
            cases.append(bytes(changed))
        for value in (sample(rows=(row(), row())), sample(untracked=1),
                      sample(active=2), sample(rows=(row(stage=10),)),
                      sample(rows=(row(wait=10),)), sample(rows=(row(work=8),)),
                      sample(rows=(row(updated_ns=132*p.NS),)),
                      sample(rows=(row(completed=1),)),
                      sample(rows=(row(last_completed_ns=p.NS),)),
                      sample(rows=(row(work_id=0),)),
                      sample(rows=(row(work_since_ns=0),))):
            cases.append(packet(value))
        for raw in cases:
            with self.subTest(raw=raw[:24]), self.assertRaises(ValueError):
                p.decode(raw)
        value = p.decode(packet(sample(complete=False, active=3, untracked=1)))
        self.assertFalse(value.complete)

    def test_actual_cpp_v2_and_old_v1_frames_are_distinct_and_valid(self):
        path = os.environ.get('Q4T_PROGRESS_V2_FRAME', str(ROOT /
            'docs/evidence/serve-progress-stages-20261109/'
            'host-cpu-01/work/native-progress.bin'))
        data = Path(path).read_bytes()
        value = p.decode(data)
        self.assertEqual(value.wire_version, 2)
        self.assertEqual((value.successes, value.steps), (2, 7))
        self.assertEqual(value.rows[0].completed, 1)
        loop = row(deadline_ns=0, stage=10, wait=0, work=8)
        value = sample(rows=(loop,), wire_version=2)
        self.assertEqual(p.decode(packet(value)), value)
        with self.assertRaises(ValueError):
            p.decode(packet(replace(value, wire_version=1)))

    def test_native_service_loop_requires_its_own_actual_completion(self):
        detector = p.Detector()
        loop = row(deadline_ns=0, stage=10, wait=0, work=8)
        for sequence, seconds in ((1, 60), (2, 61), (3, 62)):
            value = sample(sequence, seconds, (loop,), wire_version=2)
            self.assertIsNone(detector.assess(value, value.observed_ns))
        value = sample(4, 63, (loop,), wire_version=2)
        found = detector.assess(value, value.observed_ns)
        self.assertEqual(found.work, 8)
        # A completed accept-loop iteration genuinely clears its old work.
        loop = replace(loop, work_id=2, work_since_ns=64*p.NS, completed=1,
                       last_completed_ns=64*p.NS, updated_ns=64*p.NS)
        value = sample(5, 64, (loop,), wire_version=2)
        self.assertIsNone(detector.assess(value, value.observed_ns))

    def test_actual_media_work_respects_deadline_and_completed_substages(self):
        detector = p.Detector()
        media = row(stage=13, work=3)
        first = sample(1, 301, (media,), wire_version=2)
        self.assertIsNone(detector.assess(first, first.observed_ns))
        media = replace(media, stage=14, stage_since_ns=302*p.NS, completed=1,
                        last_completed_ns=302*p.NS, updated_ns=302*p.NS)
        next_stage = sample(2, 303, (media,), wire_version=2)
        self.assertIsNone(detector.assess(next_stage, next_stage.observed_ns))
        self.assertFalse(detector.pending)
        stalled = sample(3, 603, (media,), wire_version=2)
        self.assertIsNone(detector.assess(stalled, stalled.observed_ns))
        confirmed = sample(4, 605, (media,), wire_version=2)
        self.assertEqual(detector.assess(confirmed, confirmed.observed_ns).work, 3)

    def test_every_identity_field_is_required_and_mismatch_is_unknown(self):
        for name, value in vars(IDENTITY).items():
            altered = (value+1 if type(value) is int else
                       'ffffffff-ffff-4fff-8fff-ffffffffffff')
            with self.subTest(name=name):
                reader, peer = self.reader()
                bad = sample(identity=replace(IDENTITY, **{name: altered}))
                self.assertIsNone(self.send(reader, peer, bad))
                self.assertEqual(reader.reason, 'invalid_progress')
                self.assertFalse(reader.stable(1000*p.NS))

    def test_replay_future_stale_counter_and_per_work_regression(self):
        bad_cases = (sample(), sample(2, seconds=130),
            sample(2, seconds=132, successes=0),
            sample(2, seconds=132, rows=(row(work_id=0, work=0, work_since_ns=0),)),
            sample(2, seconds=132, rows=(row(deadline_ns=99*p.NS),)),
            sample(2, seconds=132, rows=(row(work_since_ns=2*p.NS,
                updated_ns=2*p.NS),)))
        for bad in bad_cases:
            reader, peer = self.reader()
            self.assertIsNotNone(self.send(reader, peer, sample()))
            self.assertIsNone(self.send(reader, peer, bad))
        for now in (130*p.NS, 134*p.NS):
            reader, peer = self.reader()
            self.assertIsNone(self.send(reader, peer, sample(), now))
        reader, peer = self.reader()
        self.assertIsNotNone(self.send(reader, peer, sample()))
        self.assertIsNone(reader.snapshot(134*p.NS))
        self.assertEqual(reader.reason, 'stale_progress')

    def test_drain_bound_corruption_and_disconnect_never_create_idle(self):
        reader, peer = self.reader()
        for sequence in range(1, 10):
            peer.send(packet(sample(sequence, observed_ns=131*p.NS+sequence)))
        self.assertIsNone(reader.poll(132*p.NS))
        self.assertEqual(reader.sequence, 8)
        self.assertEqual(reader.reason, 'progress_queue_backlog')
        self.assertIsNotNone(reader.poll(132*p.NS))
        self.assertEqual(reader.sequence, 9)
        peer.send(b'corrupt')
        self.assertIsNone(reader.poll(132*p.NS))
        peer.send(b'x'*(p.MAX_PACKET+1))
        self.assertIsNone(reader.poll(132*p.NS))
        peer.close()
        self.assertIsNone(reader.poll(132*p.NS))
        self.assertEqual(reader.reason, 'progress_disconnected')

    def test_idle_waiting_and_non_execution_are_not_stalls(self):
        detector = p.Detector()
        self.assertIsNone(detector.assess(sample(rows=()), 131*p.NS))
        variants = [row(wait=wait) for wait in (1, 2, 3, 5, 6, 7, 8, 9)]
        variants += [row(work=work) for work in (0, 1, 2, 4)]
        for sequence, work in enumerate(variants, 2):
            value = sample(sequence, seconds=1000, rows=(work,))
            self.assertIsNone(detector.assess(value, value.observed_ns))
        self.assertFalse(detector.pending)

    def test_deadline_stage_cleanup_and_two_fresh_samples_all_required(self):
        for work, starts in ((5, 301), (6, 130), (7, 130)):
            with self.subTest(work=work):
                detector = p.Detector()
                first = sample(1, seconds=starts-1, rows=(row(work=work),))
                self.assertIsNone(detector.assess(first, first.observed_ns))
                self.assertFalse(detector.pending)
                first = sample(2, seconds=starts, rows=(row(work=work),))
                self.assertIsNone(detector.assess(first, first.observed_ns))
                self.assertIsNone(detector.assess(first, first.observed_ns+p.NS))
                second = sample(3, seconds=starts+1, rows=(row(work=work),))
                self.assertIsNone(detector.assess(second, second.observed_ns))
                third = sample(4, seconds=starts+2, rows=(row(work=work),))
                found = detector.assess(third, third.observed_ns)
                self.assertEqual((found.ticket, found.work_id, found.work), (1, 1, work))
                self.assertIsNone(detector.assess(third, third.observed_ns+3*p.NS))

    def test_another_requests_completed_work_cannot_hide_old_stall(self):
        detector = p.Detector()
        for sequence, seconds in ((1, 131), (2, 132), (3, 133)):
            busy = row(ticket=2, completed=sequence, last_completed_ns=seconds*p.NS,
                       updated_ns=seconds*p.NS)
            value = sample(sequence, seconds, (row(), busy), steps=sequence)
            found = detector.assess(value, value.observed_ns)
        self.assertEqual(found.ticket, 1)

    def test_completion_wait_change_and_unknown_break_confirmation(self):
        for replacement in ('completed', 'wait', 'unknown', 'work'):
            with self.subTest(replacement=replacement):
                detector = p.Detector()
                detector.assess(sample(), 131*p.NS)
                work = row()
                if replacement == 'completed':
                    work = row(completed=1, last_completed_ns=132*p.NS,
                               updated_ns=132*p.NS)
                elif replacement == 'wait':
                    work = row(wait=0, wait_since_ns=132*p.NS, updated_ns=132*p.NS)
                elif replacement == 'work':
                    work = row(work_id=2, work_since_ns=132*p.NS, updated_ns=132*p.NS)
                value = None if replacement == 'unknown' else sample(2, 132, (work,))
                self.assertIsNone(detector.assess(value, 132*p.NS))
                self.assertIsNone(detector.assess(sample(3, 133, (work,)), 133*p.NS))

    def test_stability_requires_post_ready_business_and_fresh_evidence(self):
        reader, peer = self.reader()
        reader.mark_ready(10*p.NS)
        # Delayed self-check snapshot is not an external successful request.
        self.send(reader, peer, sample(1, 9, rows=()), 10*p.NS)
        self.assertIsNone(reader.success_baseline)
        self.send(reader, peer, sample(2, 11, rows=()))
        self.assertEqual(reader.success_baseline, 1)
        self.send(reader, peer, sample(3, 611, rows=()))
        self.assertFalse(reader.stable(611*p.NS))
        self.send(reader, peer, sample(4, 612, rows=(), successes=2))
        self.assertTrue(reader.stable(612*p.NS))
        self.assertFalse(reader.stable(615*p.NS))
        self.send(reader, peer, sample(5, 616, rows=(), successes=2,
            identity=replace(IDENTITY, model_generation=8)))
        self.assertFalse(reader.stable(616*p.NS))


class ControllerProgressTests(unittest.TestCase):
    def actor(self):
        actor = object.__new__(controller.Controller)
        actor.monitor = mock.Mock(fatal=False, visibility='AVAILABLE')
        actor.observations = observation_io.Observations()
        actor.ownership_unknown = actor.stopping = actor.frozen = False
        actor.logger, actor.progress = None, None
        actor.progress_detector, actor.progress_fault = p.Detector(), None
        actor.ready, actor.stable_reported = True, False
        actor.phase, actor.reason, actor.heartbeat_at = 'READY', 'serving', 0
        actor.channel, actor.watchdog = mock.Mock(), mock.Mock()
        actor.watchdog.tripped.is_set.return_value = False
        actor.model = mock.Mock()
        actor.model.poll.return_value = None
        actor.protect = mock.Mock(return_value=False)
        actor.last_core_check, actor.stable_at = 0, 0
        actor.grant = dict(generation=7, ticket='owned')
        actor.settings = dict(operations_port=12345)
        actor.transaction = mock.Mock(return_value=dict(ok=True, result={}))
        return actor

    def core(self):
        first = mock.patch.object(controller.core_http, 'request', return_value={
            'http_status': 200, 'body': json.dumps(dict(status='ok', gpu_healthy=True))})
        second = mock.patch.object(controller.transport, 'get', return_value={
            'http_status': 200, 'body': json.dumps(dict(ready=True))})
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)
        first.start()
        second.start()

    def test_elapsed_time_and_collector_errors_cannot_close_incident(self):
        self.core()
        actor = self.actor()
        self.assertIsNone(actor.supervise())
        actor.transaction.assert_not_called()
        reader, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        actor.progress = p.Reader(reader, IDENTITY)
        self.addCleanup(actor.progress.close)
        self.addCleanup(peer.close)
        with mock.patch.object(actor.progress, 'poll', side_effect=RuntimeError('collector bug')):
            self.assertFalse(actor.tick())
        self.assertEqual(actor.progress.reason, 'progress_collector_failed')
        actor.transaction.assert_not_called()
        actor.model.terminate.assert_not_called()

    def test_stable_transaction_requires_actual_reader_success(self):
        self.core()
        actor = self.actor()
        receive, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        actor.progress = p.Reader(receive, IDENTITY)
        self.addCleanup(actor.progress.close)
        self.addCleanup(peer.close)
        actor.progress.mark_ready(10*p.NS)
        actor.progress.accept(packet(sample(1, 11, rows=())), 11*p.NS)
        with mock.patch.object(controller.time, 'monotonic', return_value=611), \
             mock.patch.object(p.time, 'monotonic_ns', return_value=611*p.NS):
            peer.send(packet(sample(2, 611, rows=())))
            self.assertIsNone(actor.supervise())
            actor.transaction.assert_not_called()
        with mock.patch.object(controller.time, 'monotonic', return_value=614), \
             mock.patch.object(p.time, 'monotonic_ns', return_value=614*p.NS):
            peer.send(packet(sample(3, 614, rows=(), successes=2)))
            self.assertIsNone(actor.supervise())
        actor.transaction.assert_called_once_with('stable',
            dict(role='model', ticket='owned', successful_request=True))

    def test_confirmed_native_work_uses_recovery_but_still_requires_probe(self):
        actor = self.actor()
        receive, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        actor.progress = p.Reader(receive, IDENTITY)
        self.addCleanup(actor.progress.close)
        self.addCleanup(peer.close)
        with mock.patch.object(p.time, 'monotonic_ns', return_value=131*p.NS):
            peer.send(packet(sample()))
            actor.tick()
        with mock.patch.object(p.time, 'monotonic_ns', return_value=133*p.NS):
            peer.send(packet(sample(2, 133)))
            with mock.patch.object(controller.core_http, 'request') as request:
                self.assertEqual(actor.supervise(), policy.FaultKind.EXECUTION_DEADLINE)
                request.assert_not_called()
        actor.base, actor.manifest = dict(files={'q4t': 'a'*64}), dict(model_assets={})
        actor.record_fault(policy.FaultKind.EXECUTION_DEADLINE)
        self.assertTrue(actor.need_cuda_probe)
        self.assertEqual(actor.transaction.call_args.args[0], 'fault')
        self.assertFalse(actor.transaction.call_args.args[1]['unknown'])
        actor.model.terminate.assert_not_called()  # Lifecycle retires separately.


if __name__ == '__main__':
    unittest.main()
