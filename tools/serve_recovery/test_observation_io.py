"""Targeted CPU fault tests for bounded ordinary I/O, no GPU/NVML queries."""
import fcntl
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import controller, observation_io as io, policy


class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now


class WriterTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/observation-io-tests'))
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.directory = Path(self.temp.name)
        self.fd = os.open(self.directory/'private-cpu-lease', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.clock, self.stats = Clock(), io.Observations()
        self.writer = None

    def tearDown(self):
        if self.writer is not None:
            self.writer.stop()
            self.wait(lambda: self.writer.reaped)
        os.close(self.fd)
        self.temp.cleanup()

    def wait(self, predicate, timeout=3):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            if self.writer is not None:
                self.writer.tick()
            if predicate():
                return
            time.sleep(0.005)
        self.fail('bounded CPU observation condition not reached')

    def start(self):
        self.writer = io.Writer(self.directory, self.fd, self.stats, clock=self.clock)
        self.wait(lambda: self.writer.state == 'AVAILABLE')

    def test_real_writer_preserves_bytes_and_self_check_is_observation_copy(self):
        self.start()
        raw = b'prefix\x00\xff\n'
        self.assertTrue(self.writer.submit(io.LOG, raw))
        self.assertTrue(self.writer.save_self_check(dict(ok=True, response='bounded')))
        self.wait(lambda: self.stats.acknowledged == 2)
        self.assertEqual((self.directory/'model.log').read_bytes(), raw)
        self.assertEqual(json.loads((self.directory/'self-check.json').read_text()),
                         dict(ok=True, response='bounded'))
        self.assertEqual(self.stats.snapshot(self.writer)['failed_operations'], 0)

    def test_blocked_file_open_cannot_backpressure_cpu_business_stdout(self):
        # A real blocking open in the production worker, without manufacturing
        # kernel D-state or requiring a broken storage device.
        os.mkfifo(self.directory/'model.log', 0o600)
        self.start()
        child = subprocess.Popen([sys.executable, '-B', '-c',
            'import os; data=b"x"*(2*1024**2); '
            '[(os.write(1,data[i:i+4096])) for i in range(0,len(data),4096)]'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        capture = io.Capture(self.writer, child.stdout)
        try:
            self.assertEqual(child.wait(timeout=3), 0)
            self.wait(lambda: self.writer.pending is not None)
            snapshot = self.stats.snapshot(self.writer)
            self.assertGreater(snapshot['dropped_bytes'], 0)
            self.assertLessEqual(snapshot['queued_records'], io.MAX_RECORDS)
            self.assertLessEqual(snapshot['queued_bytes'], io.MAX_BYTES)
            self.clock.now += io.ACK_SECONDS+1
            self.writer.tick()
            self.wait(lambda: self.writer.reaped)
            self.assertEqual(self.writer.state, 'DISABLED')
            self.assertTrue(self.stats.unavailable)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            self.assertTrue(capture.stop())

    def test_queue_and_oversized_record_drop_instead_of_waiting(self):
        self.start()
        data = b'x'*io.MAX_RECORD
        for _ in range(io.MAX_BYTES//len(data)):
            self.assertTrue(self.writer.submit(io.LOG, data))
        self.assertFalse(self.writer.submit(io.LOG, b'overflow'))
        self.assertFalse(self.writer.submit(io.LOG, b'x'*(io.MAX_RECORD+1)))
        self.assertEqual(self.stats.snapshot(self.writer)['queued_bytes'], io.MAX_BYTES)
        self.assertEqual(self.stats.failed, 2)

    def test_record_count_bound_also_covers_zero_length_records(self):
        self.start()
        for _ in range(io.MAX_RECORDS):
            self.assertTrue(self.writer.submit(io.LOG, b''))
        self.assertFalse(self.writer.submit(io.LOG, b''))
        self.assertEqual(len(self.writer.queue), io.MAX_RECORDS)

    def test_returned_disk_error_backoff_reuses_same_worker_and_keeps_history(self):
        (self.directory/'model.log').mkdir()
        self.start()
        pid = self.writer.process.pid
        self.assertTrue(self.writer.submit(io.LOG, b'first'))
        self.wait(lambda: self.writer.state == 'BACKOFF')
        for _ in range(100):
            self.assertFalse(self.writer.submit(io.LOG, b'skipped'))
            self.writer.tick()
        failed = self.stats.failed
        (self.directory/'model.log').rmdir()
        self.clock.now += io.RETRY_SECONDS
        self.assertTrue(self.writer.submit(io.LOG, b'recovered'))
        self.wait(lambda: self.stats.acknowledged == 1)
        self.assertEqual(self.writer.process.pid, pid)
        self.assertEqual(self.stats.failed, failed)
        self.assertFalse(self.stats.unavailable)
        self.assertEqual((self.directory/'model.log').read_bytes(), b'recovered')

    def test_stalled_snapshot_worker_is_subject_to_same_deadline(self):
        self.start()
        signal.pidfd_send_signal(self.writer.pidfd, signal.SIGSTOP)
        self.assertTrue(self.writer.save_self_check(dict(ok=True)))
        self.writer.tick()
        self.assertIsNotNone(self.writer.pending)
        self.clock.now += io.ACK_SECONDS+1
        self.writer.tick()
        self.wait(lambda: self.writer.reaped)
        self.assertFalse((self.directory/'self-check.json').exists())
        self.assertGreater(self.stats.dropped, 0)

    def test_enospc_and_erofs_acknowledgements_never_escape_to_caller(self):
        original = subprocess.Popen
        for code in (errno.ENOSPC, errno.EROFS):
            with self.subTest(errno=code):
                def spawn(command, **kwargs):
                    command = list(command)
                    index = command.index(str(Path(io.__file__).resolve()))
                    command[index:index+1] = [str(Path(__file__).resolve()),
                        '--writer-errno', str(code)]
                    return original(command, **kwargs)
                with mock.patch.object(io.subprocess, 'Popen', side_effect=spawn):
                    self.start()
                self.assertTrue(self.writer.submit(io.LOG, b'cannot persist'))
                self.wait(lambda: self.writer.state == 'BACKOFF')
                self.assertIsNone(self.writer.process.returncode)
                self.writer.stop()
                self.wait(lambda: self.writer.reaped)

    def test_ack_observed_after_deadline_is_not_claimed_as_timely_success(self):
        self.start()
        self.writer.submit(io.LOG, b'late acknowledgement')
        self.writer.tick()
        self.clock.now += io.ACK_SECONDS+1
        self.writer.tick()
        self.assertEqual(self.writer.state, 'DISABLED')
        self.assertEqual(self.stats.acknowledged, 0)

    def test_unconfirmed_reap_keeps_identity_and_cannot_spawn_replacement(self):
        self.start()
        process, pidfd = self.writer.process, self.writer.pidfd
        signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        self.writer.submit(io.LOG, b'pending')
        self.writer.tick()
        with mock.patch.object(process, 'poll', return_value=None), \
             mock.patch.object(io.subprocess, 'Popen') as spawn, \
             mock.patch.object(io.signal, 'pidfd_send_signal', wraps=signal.pidfd_send_signal) as send:
            self.clock.now += 3
            self.writer.tick()
            for _ in range(100):
                self.clock.now += 600
                self.writer.tick()
                self.assertFalse(self.writer.submit(io.LOG, b'dropped'))
                self.assertFalse(self.writer.reaped)
            self.assertIs(self.writer.process, process)
            self.assertEqual(self.writer.pidfd, pidfd)
            self.assertTrue(self.stats.snapshot(self.writer)['worker_unreaped'])
            spawn.assert_not_called()
            self.assertEqual(send.call_count, 1)
        self.wait(lambda: self.writer.reaped)
        self.assertIsNone(self.writer.pidfd)

    def test_socket_creation_failure_leaves_disabled_sink_without_throwing(self):
        with mock.patch.object(io.socket, 'socketpair', side_effect=OSError('fd limit')):
            self.writer = io.Writer(self.directory, self.fd, self.stats, clock=self.clock)
        self.assertTrue(self.writer.reaped)
        self.assertFalse(self.writer.submit(io.LOG, b'continue draining'))
        self.assertEqual(self.writer.state, 'DISABLED')

    def test_pidfd_failure_retains_owned_process_until_real_reap(self):
        with mock.patch.object(io.os, 'pidfd_open', side_effect=OSError('pin failed')):
            self.writer = io.Writer(self.directory, self.fd, self.stats, clock=self.clock)
        self.assertIsNotNone(self.writer.process)
        self.wait(lambda: self.writer.reaped)
        self.assertEqual(self.writer.state, 'DISABLED')

    def test_reap_os_error_keeps_owned_slot_and_does_not_escape_to_capture(self):
        self.start()
        with mock.patch.object(self.writer.process, 'poll', side_effect=OSError('wait failed')):
            self.writer.tick()
            self.assertEqual(self.writer.state, 'DISABLED')
            self.assertFalse(self.writer.reaped)
            self.assertFalse(self.writer.submit(io.LOG, b'drain continues'))
        self.wait(lambda: self.writer.reaped)

    def test_sequence_mismatch_disables_instead_of_claiming_write_success(self):
        self.start()
        parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.setblocking(False)
        self.writer.sock.close()
        self.writer.sock = parent
        try:
            peer.send(io.ACK.pack(7, 0))
            self.writer.tick()
            self.assertEqual(self.writer.state, 'DISABLED')
            self.assertEqual(self.stats.acknowledged, 0)
        finally:
            peer.close()

    def test_repeated_stop_never_blocks_or_releases_unconfirmed_slot(self):
        self.start()
        process = self.writer.process
        with mock.patch.object(process, 'poll', return_value=None):
            for _ in range(10):
                self.writer.stop()
                self.assertIs(self.writer.process, process)
                self.assertFalse(self.writer.reaped)
        self.wait(lambda: self.writer.reaped)

    def test_worker_holds_inherited_lease_until_actual_exit(self):
        self.start()
        pid = self.writer.process.pid
        inherited = Path(f'/proc/{pid}/fdinfo/{self.fd}').read_text()
        self.assertIn('FLOCK', inherited)
        self.writer.stop()
        self.wait(lambda: self.writer.reaped)
        self.assertFalse(Path(f'/proc/{pid}').exists())


class ControllerIsolationTests(unittest.TestCase):
    def actor(self):
        actor = object.__new__(controller.Controller)
        actor.monitor = mock.Mock(fatal=False, visibility='AVAILABLE')
        actor.observations = io.Observations()
        actor.observations.unavailable = True
        actor.ownership_unknown = actor.stopping = actor.frozen = False
        actor.logger = None
        actor.phase, actor.reason, actor.heartbeat_at = 'READY', 'serving', 0
        actor.channel = mock.Mock()
        return actor

    def test_lost_observations_do_not_turn_into_core_fault_or_model_action(self):
        actor = self.actor()
        self.assertFalse(actor.tick())
        actor.channel.health.assert_called_once_with('READY', 'UNKNOWN',
            'auxiliary_io_unavailable', auxiliary_io=actor.observations.snapshot())
        actor.channel.begin.assert_not_called()
        self.assertEqual(actor.phase, 'READY')

    def test_true_device_failure_still_wins_over_ordinary_io_failure(self):
        actor = self.actor()
        actor.monitor.fatal = True
        self.assertTrue(actor.tick())
        self.assertEqual(actor.observation(), 'DEVICE_FAILURE')
        self.assertEqual(controller.health_fault(dict(status='unhealthy', gpu_healthy=False), 503),
                         policy.FaultKind.CUDA_CONTEXT)


if __name__ == '__main__':
    if '--writer-errno' in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--writer-errno', type=int, required=True)
        parser.add_argument('--worker-fd', type=int, required=True)
        parser.add_argument('--directory', type=Path, required=True)
        args = parser.parse_args()
        with mock.patch.object(io.supervisor.RotatingLog, 'write',
                               side_effect=OSError(args.writer_errno, 'CPU injection')):
            io.worker(args.worker_fd, args.directory)
    else:
        unittest.main()
