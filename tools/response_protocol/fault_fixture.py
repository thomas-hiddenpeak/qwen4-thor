"""Controlled children for verify_failures.py; never a model/network runner.

Every role requires the surrounding acceptance lease. Crash responses are
synthetic; journal writes, locks, signals and process exits are real.
"""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import acceptance

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/examples'))
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
sys.path.insert(0, str(ROOT / 'tools/verify/startup_delivery'))
import agent_tools_demo as demo
from chat_client import read_json
from deploy import require_guard
from gpu_exclusive import stop_owned
from test_chat_client import MESSAGE, capabilities, whole
from tool_recovery import save_receipt

CRASH_CASES = {
    'submitted': ('round-00-submitted.json', 'REQUEST_OUTCOME_UNKNOWN'),
    'partial-wire': (None, 'REQUEST_OUTCOME_UNKNOWN'),
    'tool-started': ('round-00-tool-000-started.json', 'TOOL_OUTCOME_UNKNOWN'),
    'tool-result': ('round-00-tool-000-result.json', 'READY_TO_REQUEST'),
    'tool-history': ('round-00-history.json', 'READY_TO_REQUEST'),
    'final-response': ('round-01-response.json', 'FINAL_RESPONSE_SAVED'),
}
SIGNAL_CASES = ('SIGTERM', 'SIGINT')


def identity():
    fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=os.getpid(), ppid=os.getppid(), start_ticks=int(fields[19]))


def wait_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        acceptance.require(time.monotonic() < deadline,
                           'fixture handshake timed out: ' + path.name)
        time.sleep(.01)


def command(args, role):
    return [sys.executable, '-B', str(Path(__file__).resolve()),
            '--role', role, '--case', args.case,
            '--directory', str(args.directory),
            '--lease-root', str(args.lease_root)]


def leaf(args):
    save_receipt(args.directory / 'ready-leaf.json', identity())
    wait_file(args.directory / 'never-release-leaf', timeout=40)
    raise AssertionError('leaf must exit by its owner signal')


def guard(args):
    acceptance.install_interrupt_handlers()
    save_receipt(args.directory / 'ready-guard.json', identity())
    child = None
    try:
        child = subprocess.Popen(command(args, 'leaf'), cwd=ROOT,
                                 start_new_session=True)
        wait_file(args.directory / 'ready-leaf.json')
        wait_file(args.directory / 'never-release-guard', timeout=30)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            save_receipt(args.directory / 'cleanup-entered.json', identity())
            # The controller sends the repeated parent signal in this window.
            wait_file(args.directory / 'allow-cleanup.json')
        finally:
            if child is not None:
                stop_owned(child, grace=1)
        save_receipt(args.directory / 'cleanup-finished.json',
                     dict(child_returncode=child.returncode if child else None))


def parent(args):
    acceptance.install_interrupt_handlers()
    real_popen = subprocess.Popen

    class ObservedGuard:
        def __init__(self, child):
            self.child = child
            self.waits = 0

        def __getattr__(self, name):
            return getattr(self.child, name)

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                # execute has stored its Popen handle and entered wait now.
                save_receipt(args.directory / 'ready-parent.json', identity())
            return self.child.wait(timeout=timeout)

    def launch(argv, **kwargs):
        acceptance.require(argv[2] == str(ROOT / 'tools/evalscope/gpu_exclusive.py')
                           and argv[3:5] == ['--output',
                               str(args.directory / 'acceptance/lease')],
                           'unexpected subprocess command in fixture')
        return ObservedGuard(real_popen(command(args, 'guard'), **kwargs))

    # Fixed local plan only: no artifact validation, GPU lease acquisition or
    # model command. execute's supervision/finally code itself is unmodified.
    plan = dict(artifacts={'python': dict(path=sys.executable)})
    with patch.object(acceptance, 'validate_plan', lambda _: None), \
         patch.object(acceptance.subprocess, 'Popen', launch):
        acceptance.execute(plan, args.directory / 'acceptance')
    raise AssertionError('interrupted acceptance unexpectedly returned')


def crash(args):
    target, _ = CRASH_CASES[args.case]
    original_save = demo.save

    def checkpoint():
        save_receipt(args.directory / 'ready-crash.json',
                     dict(identity(), case=args.case))
        wait_file(args.directory / 'never-release-crash', timeout=30)
        raise AssertionError('crash boundary unexpectedly resumed')

    def save(path, value):
        original_save(path, value)
        if path.name == target:
            checkpoint()

    class LocalResponses:
        def __init__(self, _base_url):
            self.calls = 0

        def capabilities(self, **_kwargs):
            return capabilities()

        def complete(self, request, *, sink):
            self.calls += 1
            acceptance.require(self.calls <= 2, 'fixture request budget')
            message, finish = (MESSAGE, 'tool_calls') if self.calls == 1 else (
                dict(role='assistant', content='831907'), 'stop')
            raw = json.dumps(whole(message, finish, request['request_id']),
                             ensure_ascii=False).encode()
            if args.case == 'partial-wire' and self.calls == 1:
                sink(raw[:32])
                # Acknowledge only after the actual demo file is durable.
                sink.__self__.flush()
                os.fsync(sink.__self__.fileno())
                checkpoint()
            sink(raw)
            return read_json(io.BytesIO(raw))

    with patch('urllib.request.OpenerDirector.open',
               side_effect=AssertionError('fixture network forbidden')), \
         patch.object(demo, 'save', save), \
         patch.object(demo, 'ChatClient', LocalResponses):
        demo.run(args.directory / 'journal', 'http://127.0.0.1:1',
                 stream=False, max_rounds=2, max_tool_calls=1, max_tokens=256)
    raise AssertionError('fixture did not stop at the selected crash boundary')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['parent', 'guard', 'leaf', 'crash'],
                        required=True)
    parser.add_argument('--case', choices=[*SIGNAL_CASES, *CRASH_CASES],
                        required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--lease-root', type=Path, required=True)
    args = parser.parse_args()
    args.directory = args.directory.resolve(strict=True)
    args.lease_root = args.lease_root.resolve(strict=True)
    acceptance.require(args.lease_root.is_relative_to(ROOT / '.q4t-work') and
                       args.directory.parent == args.lease_root / 'faults',
                       'fixture must belong to the guarded faults directory')
    acceptance.require((args.role == 'crash') == (args.case in CRASH_CASES),
                       'role/case mismatch')
    require_guard(args.lease_root)
    {'parent': parent, 'guard': guard, 'leaf': leaf,
     'crash': crash}[args.role](args)


if __name__ == '__main__':
    main()
