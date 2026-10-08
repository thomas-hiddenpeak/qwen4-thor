"""Five serial HTTP requests against the link-wrapped sequential fault server.

This is a test executable's logical Status-failure recovery evidence. It is not
production-binary HTTP, a CUDA fatal/OOM test or performance evidence.
"""
import argparse
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time

from acceptance_mode import startup_evidence
import mtp_failure_recovery_contract as fault
import request_cancellation_contract as recovery

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_log(path):
    recovery.require(path.stat().st_size <= 16 * 1024 * 1024,
                     'server log exceeded fixed 16 MiB limit')
    return path.read_text()


def request(port, out, label, path, body=None, timeout=120):
    end = time.monotonic() + timeout
    connection = HTTPConnection('127.0.0.1', port, timeout=min(10, timeout))
    sock = response = timer = None
    expired = threading.Event()
    received = bytearray()
    status, headers, failure = None, [], None
    if body is not None:
        save(out / (label + '-request.json'), body)
    try:
        connection.connect()
        sock = connection.sock

        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = end - time.monotonic()
        recovery.require(remaining > 0, 'HTTP total deadline exceeded')
        sock.settimeout(remaining)
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request('GET' if body is None else 'POST', path,
                           None if body is None else json.dumps(body),
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        status, headers = response.status, response.getheaders()
        recovery.read_body(response, received, expired=expired.is_set)
        result = {'status': status, 'headers': headers,
                  'body': received.decode('utf-8')}
        save(out / (label + '-response.json'), result)
        return result
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        connection.close()
        if sock is not None:
            sock.close()
        (out / (label + '-body.bin')).write_bytes(received)
        save(out / (label + '-transport.json'), {
            'status': status, 'headers': headers, 'failure': failure,
            'received_bytes': len(received),
            'encoding': 'HTTP-decoded body; chunk framing removed'})


def snapshot(port, out, label):
    end = time.monotonic() + 120
    attempt = 0
    while True:
        remaining = end - time.monotonic()
        recovery.require(remaining > 0, 'request cleanup deadline exceeded')
        tag = f'{label}-{attempt:04d}'
        health = request(port, out, tag + '-health', '/healthz',
                         timeout=min(10, remaining))
        recovery.require(health['status'] == 200, 'health endpoint failed')
        fields = json.loads(health['body'])
        recovery.require(fields.get('gpu_healthy') is True and
                         fields.get('seq_slots_total') == 1,
                         'unhealthy GPU or incorrect capacity')
        remaining = end - time.monotonic()
        recovery.require(remaining > 0, 'request cleanup deadline exceeded')
        metrics = request(port, out, tag + '-metrics', '/metrics',
                          timeout=min(10, remaining))
        recovery.require(metrics['status'] == 200, 'metrics endpoint failed')
        gauges = {}
        for name in ('q4t_active_chats', 'q4t_request_body_bytes',
                     'q4t_seq_slots_free', 'q4t_gpu_healthy'):
            found = re.findall(r'^' + name + r' ([0-9]+)$', metrics['body'],
                               re.MULTILINE)
            recovery.require(len(found) == 1, 'missing/duplicate cleanup gauge')
            gauges[name] = int(found[0])
        if (fields.get('seq_slots_free') == 1 and
                gauges == {'q4t_active_chats': 0, 'q4t_request_body_bytes': 0,
                           'q4t_seq_slots_free': 1, 'q4t_gpu_healthy': 1}):
            values = fault.counters(metrics['body'])
            save(out / (label + '-snapshot.json'), {
                'health': fields, 'gauges': gauges,
                'counters': dict(zip(fault.COUNTERS, values)), 'attempt': attempt})
            return values
        attempt += 1
        time.sleep(min(.05, max(0, end - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--quality-run', type=Path, required=True)
    parser.add_argument('--performance-run', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--startup-timeout', type=int, default=180)
    args = parser.parse_args()
    if args.startup_timeout <= 0 or not 1 <= args.port <= 65535:
        parser.error('startup timeout and port must be positive and valid')
    out = args.output.resolve()
    recovery.require(any(out.is_relative_to(ROOT / directory)
                         for directory in ('build', '.q4t-work')),
                     'output must be below this worktree build/.q4t-work')
    out.mkdir(parents=True, exist_ok=False)
    paths = {
        'quality_inputs': args.quality_run / 'inputs/requests.jsonl',
        'quality_manifest': args.quality_run / 'inputs/manifest.json',
        'performance_inputs': args.performance_run / 'inputs/context-1024.jsonl',
        'performance_metadata': args.performance_run / 'results.json',
        'server_command': args.quality_run / 'server-command.json'}
    fixtures, metadata = recovery.select_fixtures(
        [json.loads(line) for line in paths['quality_inputs'].read_text().splitlines()],
        json.loads(paths['quality_manifest'].read_text()),
        [json.loads(line) for line in paths['performance_inputs'].read_text().splitlines()],
        json.loads(paths['performance_metadata'].read_text()))
    command = json.loads(paths['server_command'].read_text())['argv']
    recovery.require(len(command) > 1 and command[1] == 'serve',
                     'source must be a serve command')
    for flag, value in (('--max-seq', '1'), ('--max-len', '208896'),
                        ('--max-prefill', '8192')):
        recovery.require(command.count(flag) == 1 and
                         command[command.index(flag) + 1] == value,
                         'source capacity differs from frozen configuration')
    recovery.require(command.count('--mtp') + command.count('--no-mtp') == 1,
                     'ambiguous source MTP mode')
    if '--no-mtp' in command:
        command[command.index('--no-mtp')] = '--mtp'
    recovery.require(command.count('--mtp-verifier') <= 1 and
                     not any(arg.startswith('--mtp-verifier=') for arg in command),
                     'ambiguous source verifier')
    if '--mtp-verifier' in command:
        index = command.index('--mtp-verifier')
        recovery.require(index + 1 < len(command), 'missing source verifier')
        del command[index:index + 2]
    recovery.require(command.count('--port') == 1 and
                     command.index('--port') + 1 < len(command),
                     'source must contain one port')
    command[command.index('--port') + 1] = str(args.port)
    command[0] = str(args.binary.resolve())
    command += ['--mtp-verifier', 'sequential']
    for name in ('run_mtp_failure_recovery.py', 'mtp_failure_recovery_contract.py',
                 'request_cancellation_contract.py', 'acceptance_mode.py',
                 'response_identity.py'):
        shutil.copy2(Path(__file__).with_name(name), out / name)
    shutil.copy2(ROOT / 'tests/mtp_sequential_fault_server.cpp', out)
    source_files = [out / name for name in (
        'run_mtp_failure_recovery.py', 'mtp_failure_recovery_contract.py',
        'request_cancellation_contract.py', 'acceptance_mode.py',
        'response_identity.py', 'mtp_sequential_fault_server.cpp')]
    save(out / 'input-bindings.json', {
        'oracle': 'fresh control in this process and sequential mode',
        'historical_output_reused': False, 'selected': metadata['decode'],
        'files': {key: {'path': str(path.resolve()),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                  for key, path in paths.items()}})
    save(out / 'server-command.json', {'argv': command})
    save(out / 'protocol.json', {
        'schema_version': 1, 'variant': 'link-wrapped test server',
        'production_binary_http': False, 'generation_requests': 5,
        'plan': fault.PLAN, 'fixture': metadata['decode'], 'max_tokens': 256,
        'seed': 20260920, 'temperature': 0, 'stream': True,
        'request_timeout_seconds': 120, 'cleanup_timeout_seconds': 120,
        'startup_timeout_seconds': args.startup_timeout,
        'body_limit_bytes': 4 * 1024 * 1024,
        'expected_counter_delta': dict(zip(fault.COUNTERS, (5, 3, 2, 0))),
        'binary': str(args.binary.resolve()),
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in source_files},
        'scope': 'logical real-forward Status failure, not CUDA fatal/OOM; '
                 'same-process observable HTTP recovery, not full state proof'})
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    records, failure, validation = [], None, None
    server = None
    log_path = out / 'server.log'
    try:
        with log_path.open('w') as log:
            server = subprocess.Popen(command, cwd=ROOT, env=environment,
                                      stdout=log, stderr=subprocess.STDOUT)
            save(out / 'server-process.json', {'pid': server.pid})
            deadline = time.monotonic() + args.startup_timeout
            while True:
                recovery.require(server.poll() is None, 'server exited at startup')
                trace = read_log(log_path)
                if 'serving on port' in trace:
                    break
                recovery.require(time.monotonic() < deadline, 'startup timeout')
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
            fault.require_variant(trace)  # Reject normal q4t before any generation.
            startup = startup_evidence(trace, True, 'sequential')
            save(out / 'startup-mode.json', startup)
            recovery.require(startup['passed'], 'wrong startup mode/capacity')
            before = previous = snapshot(args.port, out, 'before')
            control = None
            for label, kind in fault.PLAN:
                recovery.require(server.poll() is None, 'server exited between requests')
                body = {'model': 'qwen3.8-flash-next', 'prompt': fixtures['decode'],
                        'max_tokens': 256, 'temperature': 0, 'seed': 20260920,
                        'stream': True, 'stream_options': {'include_usage': True},
                        'request_id': label, 'cancel_token': secrets.token_hex(32)}
                response = request(args.port, out, label,
                                   '/v1/chat/completions', body)
                if kind == 'success':
                    result = recovery.completed_result(response, label)
                    if control is None:
                        control = result
                    else:
                        recovery.require_recovery(result, control)
                else:
                    result = fault.failed_result(response, label)
                save(out / (label + '-result.json'), result)
                current = snapshot(args.port, out, label + '-after')
                fault.require_counter_delta(previous, current, kind == 'success')
                records.append({'request_id': label, 'kind': kind, 'passed': True})
                previous = current
            recovery.require(tuple(b - a for a, b in zip(before, previous)) ==
                             (5, 3, 2, 0), 'wrong total request counters')
            validation = fault.validate_log(read_log(log_path))
            save(out / 'request-evidence.json', validation)
    except BaseException as error:
        failure = repr(error)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
                failure = failure or 'server required forced termination'
        if server is not None and server.returncode != 0:
            failure = failure or f'server exit {server.returncode}'
        if failure is None:
            try:
                validation = fault.validate_log(read_log(log_path))
                save(out / 'request-evidence.json', validation)
            except Exception as error:
                failure = repr(error)
        save(out / 'summary.json', {
            'records': records, 'failure': failure,
            'server_exit': None if server is None else server.returncode,
            'server_pid': None if server is None else server.pid,
            'variant': 'link-wrapped test server', 'production_binary_http': False,
            'passed': failure is None and len(records) == 5 and validation is not None})
    if failure is not None:
        raise RuntimeError(failure)
    print(json.dumps({'passed': True, 'requests': 5, 'output': str(out)}))


if __name__ == '__main__':
    main()
