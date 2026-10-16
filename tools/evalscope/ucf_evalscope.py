"""Single-request EvalScope UCF diagnostic wrapper; use the EvalScope venv.

Usage: python ucf_evalscope.py --probe ABS --evidence DIR -- perf ...
Requires the shared GPU lease externally. This is not performance acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from ucf_client_boundary import instrument


class CounterWindow:
    def __init__(self, command, evidence, timeout=5):
        self.command = command
        self.evidence = Path(evidence)
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.timeout = timeout
        self.process = None
        self.messages = queue.Queue()
        self.rows = []
        self.started = False
        self.complete = False
        self.error = None
        self.boundary = None
        self.stderr = None

    def save(self):
        data = {'command': self.command, 'rows': self.rows,
                'complete': self.complete, 'error': self.error,
                'boundary': self.boundary, 'performance_acceptance': False}
        (self.evidence / 'counter.json').write_text(json.dumps(data, indent=2)+'\n')

    def receive(self, expected):
        line = self.messages.get(timeout=self.timeout)
        if line is None:
            raise RuntimeError('counter EOF before ' + expected)
        row = json.loads(line)
        self.rows.append(row)
        if row.get('event') != expected:
            raise RuntimeError('unexpected counter response: ' + repr(row))
        return row

    def start(self):
        if self.started:
            raise RuntimeError('only one request/window is permitted')
        self.started = True
        self.stderr = (self.evidence / 'counter.stderr').open('w')
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr,
                                        text=True)
        def reader():
            try:
                for line in self.process.stdout:
                    self.messages.put(line)
            finally:
                self.messages.put(None)
        threading.Thread(target=reader, daemon=True).start()
        self.receive('ready')
        self.process.stdin.write('start\n'); self.process.stdin.flush()
        self.receive('started')
        self.save()

    def first(self, request_start, first_output):
        if not self.started or self.complete:
            raise RuntimeError('invalid first-output lifecycle')
        callback_ns = time.monotonic_ns()
        self.process.stdin.write('stop\n'); self.process.stdin.flush()
        stopped = self.receive('stopped')
        snapshot = self.receive('snapshot')
        rc = self.process.wait(timeout=self.timeout)
        expected = [4294967296, 0, 6, 12]
        events = snapshot.get('events', [])
        if (rc != 0 or not snapshot.get('valid') or
                snapshot.get('enabled_ns', 0) <= 0 or
                snapshot['enabled_ns'] != snapshot.get('running_ns') or
                [x.get('config') for x in events] != expected or
                len({x.get('id') for x in events}) != 4):
            raise RuntimeError('incomplete or multiplexed counter window')
        start = self.rows[1]
        self.boundary = {
            'request_start_ns': round(request_start * 1e9),
            'first_output_ns': round(first_output * 1e9),
            'callback_ns': callback_ns,
            'stop_begin_ns': stopped['begin_ns'],
            'stop_end_ns': stopped['end_ns'],
            'pre_request_count_ns': round(request_start * 1e9) - start['end_ns'],
            'first_to_stop_ns': stopped['end_ns'] - round(first_output * 1e9),
            'scope': 'HTTP first output, may include early decode before receipt',
        }
        if (self.boundary['pre_request_count_ns'] < 0 or
                self.boundary['first_to_stop_ns'] < 0):
            raise RuntimeError('counter/client clock ordering mismatch')
        self.complete = True
        self.save()

    def close(self, error=None):
        if error:
            self.error = str(error)
            self.complete = False
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=self.timeout)
            self.process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
        self.save()


def install(window):
    from evalscope.perf.plugin.api.default_api import DefaultApiPlugin
    original = DefaultApiPlugin.process_request
    patched = instrument(original, window.start, window.first)
    requests = 0

    async def observed(self, *args, **kwargs):
        nonlocal requests
        requests += 1
        if requests != 1:
            window.close('more than one request')
            raise RuntimeError('UCF diagnostic requires exactly one request')
        try:
            result = await patched(self, *args, **kwargs)
            if not result.success or not window.complete:
                raise RuntimeError('request or counter incomplete: ' + str(result.error))
            return result
        except BaseException as error:
            window.close(error)
            raise
    DefaultApiPlugin.process_request = observed
    return original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('client_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if time.get_clock_info('perf_counter').implementation != time.get_clock_info('monotonic').implementation:
        raise RuntimeError('client and collector clocks are not comparable')
    probe = args.probe.resolve(strict=True)
    window = CounterWindow(['sudo', '-n', str(probe)], args.evidence)
    (window.evidence / 'probe-sha256.txt').write_text(hashlib.sha256(probe.read_bytes()).hexdigest()+'\n')
    install(window)
    client_args = args.client_args
    if client_args[:1] == ['--']:
        client_args = client_args[1:]
    sys.argv = ['evalscope', *client_args]
    try:
        from evalscope.cli.cli import run_cmd
        rc = run_cmd()
        if rc not in (None, 0) or not window.complete:
            raise RuntimeError('client/counter did not complete')
    except BaseException as error:
        window.close(error)
        raise
    else:
        window.close()


if __name__ == '__main__':
    main()
