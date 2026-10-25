"""Pure CPU mocks for one owned interactive session; never starts Nsight."""
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'interactive_under_test', Path(__file__).with_name('interactive.py'))
interactive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(interactive)


class Clock:
    def __init__(self):
        self.value = 10.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Child:
    def __init__(self, pid=60001, code=0):
        self.pid, self.returncode = pid, code
        self.terminated = self.killed = False
        self.waits = []
        self.timeout_on_first_wait = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.timeout_on_first_wait and len(self.waits) == 1:
            raise subprocess.TimeoutExpired('mock', timeout)
        if self.returncode is None:
            self.returncode = -9 if self.killed else -15
        return self.returncode


class InteractiveContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.owner = dict(pid=os.getpid(), start_ticks=100, ppid=1,
                          pgid=os.getpgrp(), state='S')
        self.identities = {self.owner['pid']: self.owner}
        self.subreaper = 0
        self.clock = Clock()
        self.children = []
        self.calls = []
        self.create_report = True
        def identity(pid):
            if pid not in self.identities:
                raise ProcessLookupError(pid)
            return self.identities[pid].copy()
        def popen(command, **kwargs):
            self.calls.append((command, kwargs))
            child = self.children.pop(0) if self.children else Child(60001 + len(self.calls))
            if command[1] == 'stop' and child.returncode == 0 and self.create_report:
                (self.output / 'trace.nsys-rep').write_bytes(b'mock report, not a trace')
            return child
        self.identity = identity
        self.patch('proc_identity', side_effect=identity)
        self.patch('_get_subreaper', side_effect=lambda: self.subreaper)
        self.patch('_set_subreaper', side_effect=lambda value: setattr(self, 'subreaper', value))
        self.patch('secrets.token_hex', return_value='a' * 16)
        self.patch('time.monotonic', side_effect=self.clock.now)
        self.patch('time.sleep', side_effect=self.clock.sleep)
        self.popen = self.patch('subprocess.Popen', side_effect=popen)
        self.waitpid = self.patch('os.waitpid', return_value=(0, 0))

    def patch(self, name, **kwargs):
        # Modules loaded by spec are deliberately not registered globally.
        target = interactive
        bits = name.split('.')
        for part in bits[:-1]:
            target = getattr(target, part)
        handle = patch.object(target, bits[-1], **kwargs)
        self.addCleanup(handle.stop)
        return handle.start()

    def session(self, **kwargs):
        return interactive.Session('/test-only/nsys', self.output,
            interactive.make_name('fixed-case'), cwd=self.output,
            env={'PATH': '/bin'}, deadline=100, **kwargs)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def started(self):
        session = self.session()
        session.start()
        return session

    def launched(self):
        session = self.started()
        session.launch(['/test-only/target', '--arg'], io.StringIO())
        return session

    def child_identity(self, pid, *, ppid=None, ticks=200, state='S'):
        row = dict(pid=pid, ppid=self.owner['pid'] if ppid is None else ppid,
                   start_ticks=ticks, pgid=self.owner['pgid'], state=state)
        self.identities[pid] = row
        return row

    def test_exact_option_ownership_and_lifecycle(self):
        session = self.started()
        launch = session.launch(['/test-only/target'], io.StringIO(),
            {'Q4T_EXEC_DIAGNOSTIC': '1', 'NVLOG_CONFIG_FILE': '/tmp/owned-config'})
        session.stop()
        session.shutdown()
        session.close()
        commands = [row[0] for row in self.calls]
        self.assertEqual([c[1] for c in commands], ['start', 'launch', 'stop', 'shutdown'])
        self.assertIn('--session-new=' + session.name, commands[0])
        for command in commands[1:]:
            self.assertIn('--session=' + session.name, command)
        self.assertIn('--flush-on-cudaprofilerstop=true', commands[0])
        self.assertIn('--sample=none', commands[0])
        self.assertIn('--cpuctxsw=none', commands[0])
        self.assertIn('--output=' + str(self.output / 'trace'), commands[0])
        self.assertIn('--trace=cuda,nvtx', commands[1])
        self.assertIn('--wait=all', commands[1])
        self.assertIn('--env-var=NVLOG_CONFIG_FILE=/tmp/owned-config,Q4T_EXEC_DIAGNOSTIC=1', commands[1])
        self.assertIn('--keep=0', commands[2])
        self.assertIn('--kill=none', commands[3])
        self.assertFalse(any('--cuda-trace-scope' in item or item == 'profile'
                             for cmd in commands for item in cmd))
        self.assertIs(launch, session.launch_process)
        self.assertEqual(self.subreaper, 0)
        self.assertNotIn('target_returncode', self.read('interactive-stop-exit.json'))
        self.assertGreater(self.read('interactive-stop-exit.json')['report']['bytes'], 0)

    def test_names_paths_environment_and_fixed_budgets(self):
        for bad in ['foreign', f'q4t-suffix-{self.owner["pid"]}-case',
                    interactive.make_name('fixed-case').replace('-100-', '-101-')]:
            with self.subTest(name=bad), self.assertRaises(ValueError):
                interactive.Session('/nsys', self.output, bad,
                    cwd=self.output, env={}, deadline=100)
        for value in ['relative/nsys', '/tmp/%p/nsys', '/bad\nnsys']:
            with self.subTest(path=value), self.assertRaises(ValueError):
                interactive.Session(value, self.output, interactive.make_name('fixed-case'),
                    cwd=self.output, env={}, deadline=100)
        session = self.started()
        for environment in [{'A': 'comma,b'}, {'A': 'line\nb'}, {'bad-name': '1'}]:
            with self.subTest(env=environment), self.assertRaises(ValueError):
                session.launch(['/target'], io.StringIO(), environment)
        with self.assertRaises(ValueError):
            session._control('stop', ['/nsys', 'stop'], 46)
        with self.assertRaises(ValueError):
            session.reap_adopted(timeout=6)
        self.assertEqual(len(self.calls), 1)

    def test_ordering_and_no_retry(self):
        session = self.session()
        for action in (lambda: session.launch(['/target'], io.StringIO()), session.stop, session.shutdown):
            with self.assertRaises(RuntimeError):
                action()
        session.start()
        with self.assertRaises(RuntimeError):
            session.start()
        session.launch(['/target'], io.StringIO())
        with self.assertRaises(RuntimeError):
            session.launch(['/target'], io.StringIO())
        session.stop()
        with self.assertRaises(RuntimeError):
            session.stop()
        session.shutdown()
        for action in (session.stop, session.shutdown,
                       lambda: session.launch(['/target'], io.StringIO())):
            with self.assertRaises(RuntimeError):
                action()

    def test_subreaper_failure_never_starts_session(self):
        session = self.session()
        with patch.object(interactive, '_set_subreaper', side_effect=OSError(errno.EPERM, 'denied')):
            with self.assertRaises(OSError):
                session.start()
        self.assertFalse(session.can_shutdown)
        self.assertFalse(session.start_attempted)
        self.popen.assert_not_called()
        self.assertIn('denied', self.read('interactive-owner.json')['failure'])
        session.close()

    def test_subreaper_verification_and_preexisting_state_restore(self):
        session = self.session()
        with patch.object(interactive, '_set_subreaper'):
            with self.assertRaises(RuntimeError):
                session.start()
        self.popen.assert_not_called()
        self.assertEqual(self.read('interactive-owner.json')['verified_subreaper'], 0)
        session.close()
        self.assertEqual(self.subreaper, 0)

    def test_failed_start_is_recorded_and_shutdown_is_once(self):
        self.children.append(Child(code=1))
        session = self.session()
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertTrue(session.start_attempted)
        self.assertTrue(session.can_shutdown)
        self.assertFalse(session.start_succeeded)
        self.assertEqual(self.read('interactive-start-exit.json')['returncode'], 1)
        with self.assertRaises(RuntimeError):
            session.launch(['/target'], io.StringIO())
        session.shutdown()
        self.assertFalse(session.can_shutdown)
        with self.assertRaises(RuntimeError):
            session.shutdown()

    def test_control_timeout_preserves_failure_and_cleans_only_its_popen(self):
        child = Child(code=None)
        child.timeout_on_first_wait = True
        self.children.append(child)
        session = self.session()
        with self.assertRaises(TimeoutError):
            session.start(timeout=.1)
        result = self.read('interactive-start-exit.json')
        self.assertTrue(result['forced_control_cleanup'])
        self.assertTrue(child.terminated and child.killed)
        self.assertEqual(child.waits, [5, 5])
        self.assertIn('deadline', result['failure'])
        self.assertEqual(result['returncode'], -9)
        self.waitpid.assert_not_called()

    def test_monitor_failure_preserved_without_starting_control(self):
        def failed():
            raise RuntimeError('monitor failed')
        session = self.session(check=failed)
        with self.assertRaisesRegex(RuntimeError, 'monitor failed'):
            session.start()
        self.popen.assert_not_called()
        self.assertFalse(session.can_shutdown)
        self.assertIn('monitor failed', self.read('interactive-start-exit.json')['failure'])

    def test_zero_control_exit_after_deadline_is_not_success(self):
        child = Child()
        def late():
            self.clock.value = 50
            return 0
        child.poll = late
        self.children.append(child)
        session = self.session()
        with self.assertRaises(TimeoutError):
            session.start(timeout=1)
        result = self.read('interactive-start-exit.json')
        self.assertEqual(result['returncode'], 0)
        self.assertIsNotNone(result['failure'])
        self.assertFalse(session.start_succeeded)

    def test_proves_stable_owner_even_when_launch_is_not_ancestor(self):
        session = self.launched()
        self.child_identity(50001, ticks=150)
        self.child_identity(50002, ppid=50001)
        self.child_identity(50003, ppid=50002, ticks=250)
        proof = session.prove_descendant(50003)
        self.assertEqual([r['pid'] for r in proof['chain']], [50003, 50002, 50001])
        self.assertNotIn(session.launch_process.pid, [r['pid'] for r in proof['chain']])
        self.assertEqual(proof['owner'], self.owner)

    def test_unproved_recycled_or_looped_ancestry_never_admitted(self):
        session = self.launched()
        for parent, ticks in [(1, 200), (50001, 200), (self.owner['pid'], 50)]:
            with self.subTest(parent=parent, ticks=ticks):
                self.child_identity(50001, ppid=parent, ticks=ticks)
                with self.assertRaises(RuntimeError):
                    session.prove_descendant(50001)
                self.assertFalse(session._known_owned)
        self.child_identity(50001)
        calls = 0
        def recycled(pid):
            nonlocal calls
            row = self.identity(pid)
            if pid == 50001:
                calls += 1
                if calls == 2:
                    row['start_ticks'] += 1
            return row
        with patch.object(interactive, 'proc_identity', side_effect=recycled):
            with self.assertRaises(RuntimeError):
                session.prove_descendant(50001)
        self.assertFalse(session._known_owned)

    def test_reap_only_known_terminal_adopted_not_popen_or_other_parent(self):
        session = self.launched()
        self.child_identity(50001, ticks=150)
        self.child_identity(50002, ppid=50001)
        self.child_identity(session.launch_process.pid, ticks=150)
        session.prove_descendant(50002)
        session.prove_descendant(session.launch_process.pid)
        self.identities[50001]['state'] = 'Z'
        self.identities[50002]['state'] = 'Z'
        self.waitpid.return_value = (50001, 0)
        result = session.reap_adopted(timeout=0)
        self.waitpid.assert_called_once_with(50001, os.WNOHANG)
        self.assertEqual(result['reaped'][0]['returncode'], 0)
        self.assertEqual([r['pid'] for r in result['not_children']], [50002])
        self.assertFalse(result['complete'])

    def test_reap_identity_change_refuses_wait(self):
        session = self.launched()
        self.child_identity(50001)
        session.prove_descendant(50001)
        self.identities[50001].update(start_ticks=201, state='Z')
        with self.assertRaisesRegex(RuntimeError, 'reused'):
            session.reap_adopted(timeout=0)
        self.waitpid.assert_not_called()

    def test_reap_wait_is_bounded_and_preserves_live_remainder(self):
        session = self.launched()
        self.child_identity(50001)
        session.prove_descendant(50001)
        result = session.reap_adopted(timeout=.1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['remaining'][0]['pid'], 50001)
        self.assertLessEqual(result['finished_monotonic'] - result['started_monotonic'], .100001)
        self.waitpid.assert_not_called()

    def test_failed_stop_is_not_repaired_by_successful_shutdown(self):
        session = self.launched()
        self.children.append(Child(code=1))
        with self.assertRaises(RuntimeError):
            session.stop()
        session.shutdown()
        session.close()
        closed = self.read('interactive-close.json')
        self.assertFalse(closed['stop_succeeded'])
        self.assertTrue(closed['shutdown_succeeded'])
        self.assertEqual(self.read('interactive-stop-exit.json')['returncode'], 1)
        with tempfile.TemporaryDirectory() as directory:
            other = interactive.Session('/nsys', Path(directory),
                interactive.make_name('missing-report'), cwd=self.output,
                env={}, deadline=100)
            other.start()
            other.launch(['/target'], io.StringIO())
            self.create_report = False
            with self.assertRaises(FileNotFoundError):
                other.stop()
            result = json.loads((Path(directory) / 'interactive-stop-exit.json').read_text())
            self.assertEqual(result['returncode'], 0)
            self.assertIsNotNone(result['failure'])
            self.assertFalse(other.stop_succeeded)

    def test_close_preserves_existing_subreaper_and_records_restore_failure(self):
        self.subreaper = 1
        session = self.started()
        session.close()
        self.assertEqual(self.subreaper, 1)
        with self.assertRaises(RuntimeError):
            session.close()
        self.assertEqual(self.read('interactive-close.json')['original_subreaper'], 1)
        with tempfile.TemporaryDirectory() as directory:
            other = interactive.Session('/nsys', Path(directory),
                interactive.make_name('restore-failure'), cwd=self.output,
                env={}, deadline=100)
            other.start()
            with patch.object(interactive, '_set_subreaper', side_effect=OSError('restore failed')):
                with self.assertRaisesRegex(OSError, 'restore failed'):
                    other.close()
            record = json.loads((Path(directory) / 'interactive-close.json').read_text())
            self.assertIn('restore failed', record['failure'])


if __name__ == '__main__':
    unittest.main()
