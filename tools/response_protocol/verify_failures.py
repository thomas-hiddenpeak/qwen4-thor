"""Bounded real-process fault scenarios, only under the acceptance GPU lease.

This driver executes no model or network workload. It is development-only until
explicitly run in an allowed validation window. Never run on import.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import acceptance
from fault_fixture import CRASH_CASES, SIGNAL_CASES
from deploy import require_guard
from gpu_exclusive import owned_processes, processes
from tool_recovery import inspect_recovery, save_receipt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/release'))
from release import child_subreaper

CASE_SECONDS = 30


def snapshot(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.iterdir()}


class Scenario:
    def __init__(self, directory, lease_root, case, deadline):
        self.directory, self.lease_root = directory, lease_root
        self.case = case
        self.deadline = min(deadline, time.monotonic() + CASE_SECONDS)
        self.child = None
        self.history, self.fds = {}, {}
        self.fixture_pids = set()
        self.signals = []
        self.forced_cleanup = False

    def capture(self):
        if self.child is None:
            return
        records = processes()
        for pid in owned_processes(records, self.child.pid, self.history):
            if pid in self.fds:
                continue
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                acceptance.require(int(fields[19]) == self.history[pid]['start_ticks'],
                                   'owned identity changed during capture')
            except FileNotFoundError:
                os.close(fd)
                continue
            except BaseException:
                os.close(fd)
                raise
            self.fds[pid] = fd

    def wait_marker(self, name):
        path = self.directory / name
        while True:
            self.capture()
            if path.exists():
                return json.loads(path.read_text())
            acceptance.require(self.child.poll() is None,
                               'fixture exited before ' + name)
            acceptance.require(time.monotonic() < self.deadline,
                               'scenario deadline waiting for ' + name)
            time.sleep(.01)

    def checked_identity(self, value, parent_pid):
        pid = value['pid']
        record = self.history.get(pid, {})
        acceptance.require(pid in self.fds and
            record.get('start_ticks') == value.get('start_ticks') and
            value.get('ppid') == parent_pid, 'unowned fixture identity')
        self.fixture_pids.add(pid)
        return pid

    def send(self, pid, signum):
        acceptance.require(pid in self.fds, 'signal requires owned pidfd')
        signal.pidfd_send_signal(self.fds[pid], signum)
        self.signals.append(dict(pid=pid, signal=signal.Signals(signum).name))

    def wait_exit(self):
        remaining = self.deadline - time.monotonic()
        acceptance.require(remaining > 0, 'scenario exit deadline')
        return self.child.wait(timeout=remaining)

    def exited(self, pid):
        return bool(select.select([self.fds[pid]], [], [], 0)[0])

    def recover(self):
        journal = self.directory / 'journal'
        before = snapshot(journal)
        result = inspect_recovery(journal)
        acceptance.require(snapshot(journal) == before, 'recovery mutated journal')
        save_receipt(self.directory / 'journal-sha256.json', before)
        save_receipt(self.directory / 'recovery.json', result)
        acceptance.require(result['state'] == CRASH_CASES[self.case][1] and
                           result['automatic_retry'] is False,
                           'unexpected crash recovery state')
        start = json.loads((journal / 'run-started.json').read_text())
        history = result['messages']
        known = self.case in ('tool-result', 'tool-history', 'final-response')
        acceptance.require(result['completed_tool_calls'] == int(known) and
            result['attempted_tool_calls'] ==
                int(known or self.case == 'tool-started') and
            result['requests'] == (2 if self.case == 'final-response' else 1),
            'recovery counts lost durable execution boundaries')
        if known:
            response = json.loads((journal / 'round-00-response.json').read_text())
            tool_result = json.loads(
                (journal / 'round-00-tool-000-result.json').read_text())
            expected = start['initial_messages'] + [response['message'],
                dict(role='tool', **tool_result)]
            if self.case == 'final-response':
                final = json.loads((journal / 'round-01-response.json').read_text())
                expected.append(final['message'])
            acceptance.require(history == expected, 'reconstructed history changed')
        else:
            acceptance.require(history == start['initial_messages'],
                               'incomplete operation committed history')
        if self.case == 'partial-wire':
            acceptance.require((journal / 'round-00-wire.bin').stat().st_size == 32,
                               'partial wire was not retained')
        acceptance.require('failed.json' not in before and
                           'completed.json' not in before and
                           'round-02-request.json' not in before,
                           'crash fixture continued after boundary')

    def exercise(self):
        if self.case in CRASH_CASES:
            ready = self.wait_marker('ready-crash.json')
            pid = self.checked_identity(ready, os.getpid())
            acceptance.require(pid == self.child.pid and ready['case'] == self.case,
                               'wrong crash boundary')
            self.send(pid, signal.SIGKILL)
            acceptance.require(self.wait_exit() == -signal.SIGKILL,
                               'fixture did not exit from injected SIGKILL')
            self.recover()
            return
        parent = self.checked_identity(self.wait_marker('ready-parent.json'),
                                       os.getpid())
        guard = self.checked_identity(self.wait_marker('ready-guard.json'), parent)
        leaf = self.checked_identity(self.wait_marker('ready-leaf.json'), guard)
        acceptance.require(parent == self.child.pid, 'wrong acceptance parent')
        signum = getattr(signal, self.case)
        self.send(parent, signum)
        self.wait_marker('cleanup-entered.json')
        self.send(parent, signum)
        save_receipt(self.directory / 'allow-cleanup.json', dict(allowed=True))
        acceptance.require(self.wait_exit() != 0, 'interrupted parent succeeded')
        done = json.loads((self.directory / 'cleanup-finished.json').read_text())
        acceptance.require(type(done['child_returncode']) is int,
                           'fixture leaf was not waited')
        result = json.loads((self.directory / 'acceptance/result.json').read_text())
        acceptance.require(result['status'] == 'FAIL' and
            self.case in result['error'] and result['deployed'] is False,
            'interrupted stage did not retain failure')
        acceptance.require(self.exited(guard) and self.exited(leaf),
                           'acceptance left owned children alive')

    def cleanup(self):
        try:
            capture_error = None
            try:
                self.capture()
            except BaseException as error:
                capture_error = error
            # This is fallback only, and must never turn a failed contract PASS.
            for pid in self.fds:
                if not self.exited(pid):
                    self.forced_cleanup = True
                    try:
                        self.send(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if self.child is not None:
                if self.child.poll() is None and self.child.pid not in self.fds:
                    self.forced_cleanup = True
                    self.child.kill()  # Direct owned Popen handle only.
                self.child.wait(timeout=5)
            deadline = time.monotonic() + 5
            for pid in self.fds:
                while not self.exited(pid):
                    acceptance.require(time.monotonic() < deadline,
                                       'owned process did not drain')
                    time.sleep(.01)
                try:
                    # The subreaper adopts only this driver's orphaned children.
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                    acceptance.require(reaped == pid, 'owned orphan not reaped')
                except ChildProcessError:
                    pass  # Already reaped by its original parent.
            if capture_error is not None:
                raise capture_error
        finally:
            for fd in self.fds.values():
                os.close(fd)

    def run(self):
        self.directory.mkdir(mode=0o700)
        role = 'crash' if self.case in CRASH_CASES else 'parent'
        command = [sys.executable, '-B', str(ROOT /
            'tools/response_protocol/fault_fixture.py'), '--role', role,
            '--case', self.case, '--directory', str(self.directory),
            '--lease-root', str(self.lease_root)]
        save_receipt(self.directory / 'command.json', dict(argv=command))
        status, error = 'FAIL', None
        try:
            with (self.directory / 'fixture.log').open('x') as log:
                self.child = subprocess.Popen(command, cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.capture()
                self.exercise()
            expected = 1 if self.case in CRASH_CASES else 3
            acceptance.require(len(self.fixture_pids) == expected,
                               'unexpected fixture process inventory')
            status = 'PASS'
        except BaseException as exception:
            status = 'FAIL'
            error = repr(exception)
            raise
        finally:
            try:
                self.cleanup()
                acceptance.require(not self.forced_cleanup,
                                   'fixture required fallback cleanup')
            except BaseException as cleanup_error:
                status = 'ENV_ABORT'
                error = str(error) + '; cleanup: ' + repr(cleanup_error)
                raise
            finally:
                save_receipt(self.directory / 'result.json', dict(
                    status=status, error=error, signals=self.signals,
                    identities=self.history, forced_cleanup=self.forced_cleanup,
                    fixture_pids=sorted(self.fixture_pids),
                    child_returncode=self.child.returncode if self.child else None,
                    network_requests=0, model_starts=0))


def main():
    acceptance.install_interrupt_handlers()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    acceptance.require(output.name == 'faults' and
                       output.is_relative_to(ROOT / '.q4t-work'),
                       'use STAGE/faults below .q4t-work')
    require_guard(output.parent)
    output.mkdir(mode=0o700)
    started = time.monotonic()
    deadline = started + acceptance.LIMITS['fault_seconds']
    status, error, completed, observed = 'FAIL', None, [], 0
    try:
        with child_subreaper():
            for name in (*SIGNAL_CASES, *CRASH_CASES):
                require_guard(output.parent)
                acceptance.require(time.monotonic() < deadline,
                                   'fault stage deadline')
                scenario = Scenario(output / name, output.parent, name, deadline)
                try:
                    scenario.run()
                finally:
                    observed += len(scenario.fixture_pids)
                completed.append(name)
                acceptance.require(time.monotonic() < deadline,
                                   'fault stage deadline after scenario')
        acceptance.require(len(completed) == acceptance.LIMITS['fault_cases'] and
                           observed == acceptance.LIMITS['fault_process_starts'],
                           'fault coverage incomplete')
        status = 'PASS'
    except BaseException as exception:
        error = repr(exception)
        status = 'ENV_ABORT' if isinstance(exception, OSError) else 'FAIL'
        try:
            receipts = [json.loads((p / 'result.json').read_text())
                        for p in output.iterdir() if p.is_dir()]
            if any(r.get('status') == 'ENV_ABORT' for r in receipts):
                status = 'ENV_ABORT'
        except (OSError, ValueError):
            # Missing/torn scenario cleanup evidence cannot establish safety.
            status = 'ENV_ABORT'
        raise
    finally:
        save_receipt(output / 'result.json', dict(status=status, error=error,
            completed=completed, observed_fixture_processes=observed,
            elapsed_seconds=time.monotonic() - started,
            network_requests=0, model_starts=0,
            scope='Real process interruption with synthetic model responses; '
                  'not power-loss or model-quality evidence.'))


if __name__ == '__main__':
    main()
