"""Production outer daemon lifecycle with actual Guardian and CPU role fixtures."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, control, outer_daemon, outer_manager, outer_runtime
from tools.serve_recovery import outer_tree, service, test_outer_runtime


def launcher(path):
    args = json.loads(path.read_text()); mode = args.get('daemon_mode', 'normal')
    original = subprocess.Popen
    def launch(command, **kwargs):
        assert Path(command[3]).name == 'outer_daemon.py'
        # Only substitute the CPU fixture bootstrap. It calls production run,
        # patches the synthetic engine verifier and supplies actual CPU roles.
        command = [command[0], '-I', '-B', __file__, '--daemon-child', str(path), command[-1]]
        process = original(command, **kwargs)
        if mode == 'parent-exit':
            (Path(args['state'])/'daemon-pid').write_text(str(process.pid))
            os._exit(17)
        return process
    with mock.patch.object(bundle.engine, 'verify', return_value=args['base']), \
            mock.patch.object(outer_daemon.subprocess, 'Popen', side_effect=launch):
        value = outer_daemon.start(args['state'], args['package'], args['digest'],
            activate=args['activate'], timeout=1 if mode == 'reply-loss' else 6)
        print(json.dumps(value), flush=True)


def child(path, config):
    args = json.loads(path.read_text())
    with ExitStack() as patches:
        if args.get('daemon_mode') == 'reply-loss':
            patches.enter_context(mock.patch.object(outer_daemon.Reply, 'send', return_value=None))
        test_outer_runtime.worker(path, daemon_config=config)


class DaemonTests(test_outer_runtime.RuntimeTests):
    def config(self, mode='normal', activate=True):
        path = self.state/('daemon-'+mode+'.json')
        path.write_text(json.dumps(dict(state=str(self.state), package=str(self.package),
            digest=self.digest, base=self.fixture.base, mode='simple-gateway',
            activate=activate, daemon_mode=mode)))
        return path

    def begin(self, mode='normal', activate=True):
        path = self.config(mode, activate)
        self.tree = outer_tree.Tree().__enter__()
        read_fd, write_fd = os.pipe()
        try:
            self.tree.launch([sys.executable, '-I', '-B', __file__, '--launcher', str(path)],
                             env=dict(os.environ), stdio=write_fd)
            self.process = self.tree.root
        finally: os.close(write_fd)
        with os.fdopen(read_fd, 'rb') as stream: raw = stream.read(16385)
        code = self.process.wait(timeout=8)
        if mode == 'parent-exit':
            self.assertEqual(code, 17)
            return dict(outer_pid=int((self.state/'daemon-pid').read_text()))
        self.assertEqual(code, 0, raw.decode())
        return json.loads(raw)

    def observed(self, predicate, seconds=6):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            try:
                value = control.request(self.sid, 'status', timeout=.2)
                if predicate(value): return value
            except (OSError, ValueError): pass
            time.sleep(.02)
        self.fail('bounded daemon CPU observation deadline')

    def stop_daemon(self):
        self.assertTrue(control.request(self.sid, 'stop', timeout=4)['cleanup_confirmed'])
        self.tree.drain(term_seconds=.2, kill_seconds=2)
        self.assertEqual(self.core()['desired'], 'STOPPED')
        self.assertFalse(any(self.core()['active'].values())); self.assertIsNone(self.policy()['pending'])

    def test_ready_daemon_survives_launcher_exit_in_its_own_session(self):
        value = self.begin(); self.assertTrue(value['startup_confirmed']); self.assertTrue(value['business_ready'])
        first = self.observed(lambda row: row['business_ready'])
        self.assertEqual(os.getsid(value['outer_pid']), value['outer_pid'])
        self.assertEqual(first['guardian_pid'], value['guardian']['guardian_pid'])
        self.assertEqual(self.policy()['total'], 1); self.stop_daemon()

    def test_lost_startup_reply_times_out_without_stopping_healthy_core(self):
        value = self.begin('reply-loss'); self.assertEqual(value['status'], 'UNKNOWN')
        self.assertFalse(value['business_ready'])
        first = self.observed(lambda row: row['business_ready'])
        self.assertFalse(first['stop_requested']); self.assertEqual(self.core()['desired'], 'RUNNING')
        self.assertEqual(self.policy()['total'], 1); self.stop_daemon()

    def test_launcher_death_before_receipt_does_not_stop_independent_daemon(self):
        value = self.begin('parent-exit')
        first = self.observed(lambda row: row['business_ready'])
        self.assertFalse(first['stop_requested']); self.assertEqual(os.getsid(value['outer_pid']), value['outer_pid'])
        self.assertEqual(self.core()['desired'], 'RUNNING'); self.assertEqual(self.policy()['total'], 1)
        self.stop_daemon()

    def test_second_daemon_cannot_replace_live_outer_or_charge_another_attempt(self):
        first = self.begin(); original = self.core(); policy = self.policy()
        path = self.config('second')
        second = subprocess.run([sys.executable, '-I', '-B', __file__, '--launcher', str(path)],
            capture_output=True, text=True, timeout=8)
        self.assertEqual(second.returncode, 0, second.stderr)
        result = json.loads(second.stdout); self.assertEqual(result['status'], 'FAILED')
        self.assertFalse(result['startup_confirmed'])
        current = self.observed(lambda row: row['business_ready'])
        self.assertEqual(current['guardian_pid'], first['guardian']['guardian_pid'])
        self.assertEqual(self.core(), original); self.assertEqual(self.policy(), policy); self.stop_daemon()

    def test_daemon_resume_keeps_stopped_intent_and_creates_no_roles(self):
        before = self.core(); value = self.begin(activate=False)
        self.assertEqual(value['state'], 'STOPPED'); self.assertTrue(value['startup_confirmed'])
        self.tree.drain(term_seconds=.2, kill_seconds=2)
        self.assertEqual(self.core(), before); self.assertEqual(self.policy()['total'], 0)
        self.assertEqual(list((self.state/'runs').iterdir()), [])

    def test_unified_cli_respects_enable_and_never_downgrades_incomplete_policy(self):
        args = ['run', '--state', str(self.state), '--package', str(self.package),
                '--manifest-sha256', self.digest]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with mock.patch.object(outer_runtime, 'run', return_value=dict(state='STOPPED')) as run:
                self.assertEqual(service.main(args), 0)
                self.assertTrue(run.call_args.kwargs['activate'])
            outer_manager.configure(self.state, self.package, self.digest, enabled=False)
            with mock.patch.object(service, 'launch', return_value=dict(state='STOPPED')) as legacy:
                self.assertEqual(service.main(args), 0); self.assertEqual(legacy.call_count, 1)
            (self.state/outer_manager.INTENT).unlink()
            with mock.patch.object(service, 'launch', side_effect=AssertionError('downgrade forbidden')):
                self.assertEqual(service.main(args), 1)

    def test_inherited_capabilities_validate_all_original_fds_before_duplication(self):
        read_fd, write_fd = os.pipe(); os.close(write_fd)
        try:
            with mock.patch.dict(os.environ, {'Q4T_MANAGED_LEASE_FD': str(read_fd),
                                             'Q4T_RESOURCE_ENTRY_FD': str(write_fd)}, clear=True):
                with self.assertRaises(OSError): outer_daemon.inherited_environment()
                os.fstat(read_fd)
            with mock.patch.dict(os.environ, {'Q4T_MANAGED_LEASE_FD': str(read_fd)}, clear=True):
                env, copies = outer_daemon.inherited_environment()
                try:
                    self.assertNotEqual(int(env['Q4T_MANAGED_LEASE_FD']), read_fd)
                    self.assertEqual(os.fstat(copies[0]).st_ino, os.fstat(read_fd).st_ino)
                finally:
                    for fd in copies: os.close(fd)
        finally: os.close(read_fd)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--launcher': launcher(Path(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == '--daemon-child': child(Path(sys.argv[2]), Path(sys.argv[3]))
    else: unittest.main(module=None)
