"""Actual HTTP cancellation contracts; no performance inference from this test."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import struct
import threading
import time

from acceptance_mode import startup_evidence
from response_identity import response_identity

ROOT = Path(__file__).resolve().parents[2]


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--quality-run', type=Path)
    p.add_argument('--performance-run', type=Path)
    p.add_argument('--scope', choices=['full', 'decode-recovery',
                                      'same-mode-recovery'], default='full')
    p.add_argument('--reference-run', type=Path,
                   help='Completed ordinary-decode run with fixed 1K performance evidence')
    p.add_argument('--mtp', action='store_true')
    p.add_argument('--mtp-verifier', choices=['t4', 'sequential'])
    p.add_argument('--port', type=int)
    p.add_argument('--startup-timeout', type=int, default=180)
    args = p.parse_args()
    narrow = args.scope == 'decode-recovery'
    same_mode = args.scope == 'same-mode-recovery'
    if args.mtp_verifier is not None and not (args.mtp and same_mode):
        p.error('--mtp-verifier requires --mtp --scope same-mode-recovery')
    verifier = args.mtp_verifier or 't4'
    if same_mode:
        import request_cancellation_contract as recovery_contract
    if args.startup_timeout <= 0:
        p.error('--startup-timeout must be positive')
    if narrow and (not args.mtp or args.reference_run is None):
        p.error('decode-recovery requires --mtp and --reference-run')
    if not narrow and (args.quality_run is None or args.performance_run is None):
        p.error('full/same-mode scope requires --quality-run and --performance-run')
    if same_mode and (not args.mtp or args.reference_run is not None):
        p.error('same-mode-recovery requires --mtp and forbids --reference-run')
    if not narrow and not same_mode and (args.mtp or args.reference_run is not None):
        p.error('--mtp and --reference-run belong to decode-recovery scope')
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    reference = None
    if narrow:
        prior = args.reference_run.resolve()
        paths = {
            'results': prior / 'results.json',
            'exit': prior / 'exit.json',
            'server_command': prior / 'server-command.json',
            'client_command': prior / 'context-1024/command.json',
            'responses': prior / 'context-1024/responses.json',
            'input': prior / 'inputs/context-1024.jsonl',
            'server_log': prior / 'server.log',
        }
        exit_record = json.loads(paths['exit'].read_text())
        assert exit_record['server'] == 0 and exit_record['http_output_checks_passed'], 'reference run not accepted'
        assert not exit_record.get('failure'), 'reference run retained a failure'
        rows = json.loads(paths['results'].read_text())
        selected = [row for row in rows if row['length'] == 1024]
        assert len(selected) == 1, 'reference must contain one 1K group'
        reference = selected[0]
        assert reference['deterministic'] and len(reference['outputs']) >= 3
        assert len(set(reference['outputs'])) == 1
        cmd = json.loads(paths['server_command'].read_text())['argv']
        assert '--no-mtp' in cmd and '--mtp' not in cmd, 'reference is not ordinary decode'
        assert 'MTP disabled; plain decode' in paths['server_log'].read_text()
        for flag, value in [('--max-seq', '1'), ('--max-len', '208896'), ('--max-prefill', '8192')]:
            assert cmd[cmd.index(flag) + 1] == value, 'reference capacity mismatch'
        reference_client = json.loads(paths['client_command'].read_text())
        for flag, value in [('--temperature', '0'), ('--seed', '20260920'), ('--max-tokens', '256')]:
            assert reference_client[reference_client.index(flag) + 1] == value
        fixture_rows = [json.loads(line) for line in paths['input'].read_text().splitlines()]
        assert fixture_rows and len({row['prompt'] for row in fixture_rows}) == 1
        fixture = fixture_rows[0]['prompt']
        prompt_hash = hashlib.sha256(fixture.encode()).hexdigest()
        assert prompt_hash == reference['prompt_sha256']
        reference_responses = json.loads(paths['responses'].read_text())
        assert len(reference_responses) == len(reference['outputs'])
        for row, output_hash in zip(reference_responses, reference['outputs']):
            assert row['success'] and row['actual_input'] == 1024 and row['actual_output'] == 256
            assert row['finish'] == ['length'] and row['request_stream'] is True
            assert row['prompt_sha256'] == prompt_hash
            assert hashlib.sha256(row['text'].encode()).hexdigest() == output_hash
        fixtures, expected = {'decode': fixture}, {}
        cmd[cmd.index('--no-mtp')] = '--mtp'
        cmd[0] = str(args.binary.resolve())
        save(out / 'reference.json', {
            'run': str(prior), 'group': reference,
            'files_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for name, path in paths.items()},
        })
        cases = ['single-stream MTP decode explicit cancellation',
                 'same-input recovery equals frozen ordinary-decode reference']
    elif same_mode:
        paths = {
            'quality_inputs': args.quality_run / 'inputs/requests.jsonl',
            'quality_manifest': args.quality_run / 'inputs/manifest.json',
            'performance_inputs': args.performance_run / 'inputs/context-1024.jsonl',
            'performance_metadata': args.performance_run / 'results.json',
            'server_command': args.quality_run / 'server-command.json',
        }
        fixtures, fixture_metadata = recovery_contract.select_fixtures(
            [json.loads(line) for line in paths['quality_inputs'].read_text().splitlines()],
            json.loads(paths['quality_manifest'].read_text()),
            [json.loads(line) for line in paths['performance_inputs'].read_text().splitlines()],
            json.loads(paths['performance_metadata'].read_text()))
        cmd = json.loads(paths['server_command'].read_text())['argv']
        assert cmd[1] == 'serve', 'expected serve command'
        for flag, value in [('--max-seq', '1'), ('--max-len', '208896'),
                            ('--max-prefill', '8192')]:
            assert cmd.count(flag) == 1 and cmd[cmd.index(flag) + 1] == value, 'fixture capacity mismatch'
        assert cmd.count('--no-mtp') + cmd.count('--mtp') == 1, 'ambiguous source mode'
        if '--no-mtp' in cmd:
            cmd[cmd.index('--no-mtp')] = '--mtp'
        # A source run supplies fixtures/capacity, never the verifier choice.
        assert cmd.count('--mtp-verifier') <= 1, 'ambiguous source verifier'
        if '--mtp-verifier' in cmd:
            index = cmd.index('--mtp-verifier')
            assert index + 1 < len(cmd), 'missing source verifier value'
            del cmd[index:index + 2]
        if args.mtp_verifier is not None:
            cmd += ['--mtp-verifier', verifier]
        cmd[0] = str(args.binary.resolve())
        save(out / 'input-bindings.json', {
            'oracle': 'fresh control in this process and mode; no historical output',
            'selected': fixture_metadata,
            'files': {name: {'path': str(path.resolve()),
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                      for name, path in paths.items()},
        })
        for name in ['request_cancellation_contract.py', 'response_identity.py',
                     'acceptance_mode.py']:
            shutil.copy2(Path(__file__).with_name(name), out / name)
        cases = recovery_contract.recovery_plan()
    else:
        prior = args.quality_run.resolve()
        rows = json.loads((prior / 'results.json').read_text())
        lines = [json.loads(x) for x in (prior / 'inputs/requests.jsonl').read_text().splitlines()]
        fixtures = {}
        expected = {}
        for label, length in [('short', 1024), ('long', 45056)]:
            row = next(x for x in rows if x['actual_input'] == length)
            fixtures[label] = next(x['prompt'] for x in lines if hashlib.sha256(x['prompt'].encode()).hexdigest() == row['prompt_sha256'])
            expected[label] = row['text']
        fixtures['decode'] = json.loads((args.performance_run / 'inputs/context-1024.jsonl').read_text().splitlines()[0])['prompt']
        cmd = json.loads((prior / 'server-command.json').read_text())['argv']
        cmd[0] = str(args.binary.resolve())
        for flag, value in [('--max-seq', '2'), ('--max-len', '65536')]:
            cmd[cmd.index(flag) + 1] = value
        cases = ['queued explicit/FIN/RST/deadline', 'duplicate/wrong key',
                 'prefill explicit/FIN/RST', 'surviving request',
                 'decode explicit/deadline', 'ID reuse', 'shutdown with stalled reader']
    if args.port is not None:
        cmd[cmd.index('--port') + 1] = str(args.port)
    port = int(cmd[cmd.index('--port') + 1])
    save(out / 'plan.json', {
        'command': cmd, 'cases': cases, 'scope': args.scope,
        'startup_timeout_seconds': args.startup_timeout,
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        **({'generation_requests': 9, 'decode_paths': 8,
            'expected_counter_delta': [9, 4, 5], 'deadline_ms': 3000,
            'prefill_cancel_delay_ms': 500, 'verifier': verifier,
            'expected_path': ('mtp_sequential_b1' if verifier == 'sequential'
                              else 'mtp_multi_b1'),
            'http_total_timeout_seconds': 120, 'http_control_timeout_seconds': 10,
            'http_response_byte_limit': 4 * 1024 * 1024,
            'limits': 'HTTP same-mode recovery, not full internal state equality; FIN not exercised'}
           if same_mode else {}),
    })
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff'], cwd=ROOT))
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    keys = {}
    records = []

    def payload(label, fixture, timeout=None):
        key = secrets.token_hex(32)
        keys[label] = key
        d = dict(model='qwen3.8-flash-next', prompt=fixtures[fixture], stream=True,
                 max_tokens=256 if fixture == 'decode' or same_mode else 32, temperature=0,
                 stream_options={'include_usage': True}, request_id=label, cancel_token=key)
        if narrow or same_mode:
            d['seed'] = 20260920
        if timeout is not None:
            d['request_timeout_ms'] = timeout
        return d

    def http(path, body=None, first=None, label=None):
        c = HTTPConnection('127.0.0.1', port, timeout=300)
        if label:
            save(out / (label + '-request.json'), body)
        raw, status = bytearray(), None
        try:
            c.request('GET' if body is None else 'POST', path,
                      None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            r = c.getresponse()
            status = r.status
            while True:
                line = r.readline()
                if not line:
                    break
                raw.extend(line)
                if first and line.startswith(b'data: {'):
                    e = json.loads(line[6:])
                    if any(x.get('delta', {}).get('content') for x in e.get('choices', [])):
                        first.set()
            result = {'status': r.status, 'body': raw.decode()}
            if label:
                save(out / (label + '-response.json'), result)
            return result
        except BaseException as error:
            if (narrow or same_mode) and label:
                save(out / (label + '-partial-response.json'), {
                    'status': status, 'body': raw.decode(errors='replace'),
                    'failure': repr(error),
                })
            raise
        finally:
            c.close()

    def health():
        r = http('/healthz')
        assert r['status'] == 200
        return json.loads(r['body'])

    def wait_free(n, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            h = health()
            if h['seq_slots_free'] == n:
                return h
            time.sleep(.02)
        raise RuntimeError(f'expected {n} free slots')

    def counter(name):
        text = http('/metrics')['body']
        return int(next(x.split()[1] for x in text.splitlines() if x.startswith(name + ' ')))

    def cancel(label, key=None, tag=None):
        r = http('/v1/requests/cancel', {'request_id': label, 'cancel_token': key or keys[label]})
        save(out / ((tag or label + '-cancel') + '.json'), r)
        return r['status']

    def text(r):
        assert r['status'] == 200 and r['body'].strip().endswith('data: [DONE]')
        events = [json.loads(x[6:]) for x in r['body'].splitlines() if x.startswith('data: {')]
        assert not any('error' in e for e in events)
        return ''.join(c.get('delta', {}).get('content', '') for e in events for c in e.get('choices', []))

    def fin_request(label, fixture, active=False, reset=False):
        body = payload(label, fixture)
        save(out / (label + '-request.json'), body)
        before_total = counter('q4t_requests_total')
        before_abort = counter('q4t_requests_aborted_total')
        raw = json.dumps(body).encode()
        sock = socket.create_connection(('127.0.0.1', port), timeout=30)
        sock.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % len(raw)).encode() + raw)
        if active:
            wait_free(1)
            time.sleep(.5)
        if reset:
            for _ in range(100):
                if counter('q4t_requests_total') > before_total:
                    break
                time.sleep(.02)
            else:
                raise RuntimeError('RST request did not reach handler')
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()
            if active:
                wait_free(2)
            for _ in range(100):
                if counter('q4t_requests_aborted_total') == before_abort + 1:
                    break
                time.sleep(.02)
            else:
                raise RuntimeError('RST was not counted as one cancellation')
            save(out / (label + '-reset.json'), {'reset_sent': True, 'aborted_increment': 1})
            return
        sock.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            data = sock.recv(65536)
            if not data:
                break
            response.extend(data)
        sock.close()
        (out / (label + '-raw.http')).write_bytes(response)
        assert b' 409 ' in response and b'data:' not in response

    narrow_ids = []

    def decode_recovery(pool):
        def events(response, label):
            assert response['status'] == 200
            data = [line[6:] for line in response['body'].splitlines()
                    if line.startswith('data: ')]
            assert data and data[-1] == '[DONE]' and data.count('[DONE]') == 1
            parsed = [json.loads(line) for line in data[:-1]]
            identity = response_identity(parsed)
            save(out / (label + '-identity.json'), identity)
            assert identity['response_id_valid'] and identity['response_id'] == label
            narrow_ids.append(identity['response_id'])
            return parsed

        def snapshot(label):
            h = wait_free(1)
            save(out / (label + '-health.json'), h)
            assert h['gpu_healthy'] and h['seq_slots_total'] == 1
            response = http('/metrics')
            save(out / (label + '-metrics.json'), response)
            assert response['status'] == 200
            values = {}
            for name in ['q4t_requests_total', 'q4t_requests_aborted_total',
                         'q4t_requests_success_total']:
                values[name] = int(next(line.split()[1]
                    for line in response['body'].splitlines()
                    if line.startswith(name + ' ')))
            return values

        before = snapshot('before')
        first = threading.Event()
        body = payload('mtp-decode-cancel', 'decode')
        task = pool.submit(http, '/v1/chat/completions', body, first,
                           'mtp-decode-cancel')
        assert first.wait(120), 'no content before cancellation deadline'
        assert not task.done(), 'generation finished before cancellation'
        save(out / 'cancel-active-health.json', health())
        assert cancel('mtp-decode-cancel') == 202
        cancelled = events(task.result(timeout=60), 'mtp-decode-cancel')
        errors = [event['error'] for event in cancelled if 'error' in event]
        assert len(errors) == 1 and errors[0].get('message') == 'request cancelled'
        assert not any(event.get('usage') for event in cancelled)
        assert not any(choice.get('finish_reason') for event in cancelled
                       for choice in event.get('choices', []))
        after_cancel = snapshot('after-cancel')
        assert after_cancel['q4t_requests_total'] == before['q4t_requests_total'] + 1
        assert after_cancel['q4t_requests_aborted_total'] == before['q4t_requests_aborted_total'] + 1
        assert after_cancel['q4t_requests_success_total'] == before['q4t_requests_success_total']
        records.append({'case': 'mtp-decode-cancel', 'passed': True})

        recovery_body = payload('mtp-decode-recovery', 'decode')
        assert {key: value for key, value in body.items()
                if key not in ['request_id', 'cancel_token']} == {
                    key: value for key, value in recovery_body.items()
                    if key not in ['request_id', 'cancel_token']}
        response = http('/v1/chat/completions', recovery_body,
                        label='mtp-decode-recovery')
        recovered = events(response, 'mtp-decode-recovery')
        assert not any('error' in event for event in recovered)
        choices = [choice for event in recovered for choice in event.get('choices', [])]
        finishes = [choice['finish_reason'] for choice in choices if choice.get('finish_reason')]
        usage = [event['usage'] for event in recovered if event.get('usage')]
        output = ''.join(choice.get('delta', {}).get('content', '') for choice in choices)
        output_hash = hashlib.sha256(output.encode()).hexdigest()
        save(out / 'recovery-result.json', {
            'output_sha256': output_hash, 'finish': finishes, 'usage': usage,
            'reference_output_sha256': reference['outputs'][0],
        })
        assert output_hash == reference['outputs'][0], 'recovery differs from ordinary decode'
        assert finishes == ['length'] and len(usage) == 1
        assert usage[0]['prompt_tokens'] == 1024 and usage[0]['completion_tokens'] == 256
        assert usage[0]['total_tokens'] == 1280
        after = snapshot('after-recovery')
        assert after['q4t_requests_total'] == before['q4t_requests_total'] + 2
        assert after['q4t_requests_aborted_total'] == after_cancel['q4t_requests_aborted_total']
        assert after['q4t_requests_success_total'] == before['q4t_requests_success_total'] + 1
        records.append({'case': 'mtp-decode-recovery', 'passed': True,
                        'reference_output_sha256': reference['outputs'][0]})

    def check_narrow_paths():
        # Cancellation has no usage: do not invent a token count from chunks.
        # Both actual response IDs instead bind directly to terminal path logs.
        rows = []
        for line in (out / 'server.log').read_text().splitlines():
            prefix = '[q4t][decode_path] '
            if not line.startswith(prefix):
                continue
            pairs = [field.split('=', 1) for field in line[len(prefix):].split()]
            assert all(len(pair) == 2 for pair in pairs), 'malformed terminal path'
            fields = dict(pairs)
            assert len(fields) == len(pairs), 'duplicate terminal fields'
            rows.append(fields)
        save(out / 'request-modes.json', {'actual_response_ids': narrow_ids,
                                          'terminal_paths': rows})
        assert len(narrow_ids) == 2 and len(set(narrow_ids)) == 2
        assert len(rows) == 2 and {row['id'] for row in rows} == set(narrow_ids)
        for row in rows:
            assert row['requested_mtp'] == '1' and row['path'] == 'mtp_multi_b1'
            assert re.fullmatch(r'[1-9][0-9]*', row['mtp_steps'])
            assert row['fallback'] == 'none' and row['plain_tail_tokens'] == '0'

    def same_mode_recovery(pool):
        def request(path, body=None, first=None, label=None, reset=False,
                    total_timeout=120):
            # Only this scope uses absolute deadlines and bounded bodies. The
            # historical scopes retain their original HTTP behavior.
            end = time.monotonic() + total_timeout
            connection = HTTPConnection('127.0.0.1', port,
                                        timeout=min(10, total_timeout))
            sock, response, timer = None, None, None
            expired = threading.Event()
            received = bytearray()
            parsed_prefix = 0
            status, headers, failure = None, [], None
            if label:
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
                recovery_contract.require(remaining > 0, 'HTTP total deadline exceeded')
                sock.settimeout(remaining)
                timer = threading.Timer(remaining, expire)
                timer.daemon = True
                timer.start()
                connection.request('GET' if body is None else 'POST', path,
                    None if body is None else json.dumps(body),
                    {'Content-Type': 'application/json'})
                response = connection.getresponse()
                status, headers = response.status, response.getheaders()
                parsed_prefix = recovery_contract.read_body(response, received, first,
                    stop_after_content=reset, expired=expired.is_set)
                result = {'status': status, 'headers': headers,
                          'body': received[:parsed_prefix].decode()}
                if reset:
                    observed = recovery_contract.require_partial(result, label)
                    # HTTPResponse handles chunked framing. Close both its file
                    # and the held socket with linger=0 immediately after content.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack('ii', 1, 0))
                    response.close()
                    connection.close()
                    sock.close()
                    save(out / (label + '-partial-result.json'), observed)
                    save(out / (label + '-reset.json'), {'reset_sent': True,
                        'actual_response_id': label, 'after_nonempty_content': True})
                elif label:
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
                if label and (reset or failure is not None):
                    # These are HTTP-decoded body bytes, not raw wire framing.
                    (out / (label + '-partial-body.bin')).write_bytes(received)
                    save(out / (label + '-partial-response.json'), {
                        'status': status, 'headers': headers,
                        'body': received[:parsed_prefix].decode(errors='replace'),
                        'unparsed_tail_hex': received[parsed_prefix:].hex(),
                        'failure': failure,
                        'body_encoding': 'HTTP-decoded bytes; chunk framing removed'})

        def available(n, timeout=120):
            end = time.monotonic() + timeout
            while True:
                remaining = end - time.monotonic()
                recovery_contract.require(remaining > 0, 'slot recovery deadline exceeded')
                response = request('/healthz', total_timeout=min(10, remaining))
                assert response['status'] == 200
                h = json.loads(response['body'])
                assert h['gpu_healthy'] and h['seq_slots_total'] == 1
                if h['seq_slots_free'] == n:
                    return h
                time.sleep(min(.02, max(0, end - time.monotonic())))

        def cancel_request(label):
            response = request('/v1/requests/cancel',
                               {'request_id': label, 'cancel_token': keys[label]},
                               label=label + '-cancel', total_timeout=10)
            assert response['status'] == 202

        def snapshot(label):
            h = available(1)
            save(out / (label + '-health.json'), h)
            response = request('/metrics', total_timeout=10)
            save(out / (label + '-metrics.json'), response)
            assert response['status'] == 200
            return recovery_contract.metric_values(response['body'])

        before = snapshot('same-before')
        previous = before
        control = None
        for label, kind, fixture in recovery_contract.recovery_plan():
            body = payload(label, fixture, 3000 if kind == 'deadline' else None)
            if kind in ['control', 'recovery']:
                result = recovery_contract.completed_result(
                    request('/v1/chat/completions', body, label=label), label)
                save(out / (label + '-result.json'), result)
                if kind == 'control':
                    control = result
                else:
                    recovery_contract.require_recovery(result, control)
                expected_delta = (1, 0, 1)
            elif kind == 'rst':
                request('/v1/chat/completions', body, label=label, reset=True)
                expected_delta = (1, 1, 0)
            else:
                first = threading.Event()
                task = pool.submit(request, '/v1/chat/completions', body, first, label)
                if kind == 'prefill_cancel':
                    save(out / (label + '-active-health.json'), available(0))
                    time.sleep(.5)  # Frozen 500ms; never adjusted after a failure.
                    assert not task.done(), 'prefill finished before cancellation'
                    cancel_request(label)
                    response = task.result(timeout=120)
                    recovery_contract.require_prefill_response(response)
                else:
                    assert first.wait(120), 'no content before decode interruption'
                    if kind == 'explicit':
                        assert not task.done(), 'decode finished before cancellation'
                        cancel_request(label)
                    # Deadline stays fixed at request creation, not at first
                    # content. Missing content is a coverage failure, no retry.
                    result = recovery_contract.require_cancelled(
                        task.result(timeout=60), label)
                    save(out / (label + '-result.json'), result)
                expected_delta = (1, 1, 0)
            current = snapshot(label + '-after')
            recovery_contract.require_delta(previous, current, expected_delta)
            if kind == 'prefill_cancel':
                position = recovery_contract.require_prefill_cancel(
                    (out / 'server.log').read_text())
                save(out / (label + '-progress.json'), {'position': position,
                     'total': 45056, 'request_id': label})
            records.append({'case': label, 'kind': kind, 'passed': True,
                            'counter_delta': list(expected_delta)})
            previous = current
        recovery_contract.require_delta(before, previous, (9, 4, 5))
        save(out / 'same-mode-counts.json', {'before': before, 'after': previous,
                                           'delta': [9, 4, 5]})

    failure = None
    with (out / 'server.log').open('w') as log, ThreadPoolExecutor(max_workers=4) as pool:
        server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(args.startup_timeout):
                assert server.poll() is None
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError('startup timeout')
            if narrow or same_mode:
                startup = startup_evidence((out / 'server.log').read_text(), True,
                                           verifier)
                save(out / 'startup-mode.json', startup)
                assert startup['passed'], startup['errors']
                if same_mode:
                    same_mode_recovery(pool)
                else:
                    decode_recovery(pool)
                return
            assert '=> max_len=65536 max_seq=2' in (out / 'server.log').read_text()
            a = pool.submit(http, '/v1/chat/completions', payload('active-a', 'long'), None, 'active-a')
            wait_free(1)
            b = pool.submit(http, '/v1/chat/completions', payload('survivor', 'long'), None, 'survivor')
            wait_free(0)
            before = counter('q4t_requests_aborted_total')
            q = pool.submit(http, '/v1/chat/completions', payload('queued', 'short'), None, 'queued')
            time.sleep(.2)
            assert cancel('queued', '0' * 64, 'wrong-key') == 404
            duplicate = http('/v1/chat/completions', payload('active-a', 'short'))
            save(out / 'duplicate-id.json', duplicate)
            assert duplicate['status'] == 409
            # payload above generated another key; recover the active generation's key.
            keys['active-a'] = json.loads((out / 'active-a-request.json').read_text())['cancel_token']
            assert cancel('queued') == 202
            assert q.result(timeout=2)['status'] == 409
            assert not a.done() and not b.done() and health()['seq_slots_free'] == 0
            deadline = pool.submit(http, '/v1/chat/completions', payload('queued-deadline', 'short', 300), None, 'queued-deadline')
            assert deadline.result(timeout=2)['status'] == 409
            fin_request('queued-fin', 'short')
            fin_request('queued-rst', 'short', reset=True)
            assert counter('q4t_requests_aborted_total') == before + 4
            assert not a.done() and not b.done()
            records.append('queued explicit/deadline/FIN/RST without waiting for GPU slots')
            assert cancel('active-a') == 202
            repeated = cancel('active-a', tag='active-a-duplicate-cancel')
            assert repeated in [202, 404]  # May have completed cleanup already.
            assert a.result(timeout=120)['status'] == 409
            assert text(b.result(timeout=120)) == expected['long']
            wait_free(2)
            trace = (out / 'server.log').read_text()
            positions = re.findall(r'prefill cancelled seq=\d+ position=(\d+) total=45056', trace)
            assert positions and int(positions[0]) < 45056
            records.append('prefill explicit cancellation with unaffected active survivor')
            fin_request('active-fin', 'long', active=True)
            wait_free(2)
            fin_request('active-rst', 'long', active=True, reset=True)
            records.append('active prefill FIN/RST')
            first = threading.Event()
            d = pool.submit(http, '/v1/chat/completions', payload('decode-cancel', 'decode'), first, 'decode-cancel')
            assert first.wait(120)
            assert cancel('decode-cancel') == 202
            r = d.result(timeout=30)
            assert r['status'] == 200 and '"error"' in r['body'] and '"finish_reason":"length"' not in r['body']
            wait_free(2)
            r = http('/v1/chat/completions', payload('decode-deadline', 'decode', 2500), label='decode-deadline')
            assert r['status'] == 200 and '"error"' in r['body']
            wait_free(2)
            records.append('decode explicit/deadline without normal completion')
            body = payload('nonstream-cancel', 'decode')
            body['stream'] = False
            task = pool.submit(http, '/v1/chat/completions', body, None, 'nonstream-cancel')
            wait_free(1)
            time.sleep(1.5)
            assert cancel('nonstream-cancel') == 202
            assert task.result(timeout=30)['status'] == 409
            wait_free(2)
            records.append('non-streaming decode cancellation')
            for index, value in enumerate([0, -1, 1.5, 86400001, 1e300]):
                body = payload(f'invalid-deadline-{index}', 'short', value)
                r = http('/v1/chat/completions', body, label=f'invalid-deadline-{index}')
                assert r['status'] == 400
            records.append('deadline numeric boundaries rejected before GPU admission')

            old_key = keys['active-a']
            new_body = payload('active-a', 'long')
            r = pool.submit(http, '/v1/chat/completions', new_body, None, 'reused-id')
            wait_free(1)
            assert cancel('active-a', old_key, 'stale-key') == 404
            assert cancel('active-a', tag='new-key') == 202
            assert r.result(timeout=120)['status'] == 409
            wait_free(2)
            assert text(http('/v1/chat/completions', payload('canary', 'short'), label='canary')) == expected['short']
            assert cancel('canary') == 404
            records.append('old cancellation credential cannot cancel new generation; reuse canary')
            save(out / 'metrics-before-shutdown.json', http('/metrics'))
            # Shut down with a partial HTTP header plus active GPU work.
            stalled = socket.create_connection(('127.0.0.1', port), timeout=30)
            stalled.sendall(b'POST /v1/chat/completions HTTP/1.1\r\n')
            task = pool.submit(http, '/v1/chat/completions', payload('shutdown-active', 'long'), None, 'shutdown-active')
            wait_free(1)
            time.sleep(.5)  # Enter the first GPU chunk before shutdown.
            started = time.monotonic()
            server.terminate()
            assert server.wait(timeout=60) == 0
            try:
                task.result(timeout=5)
            except (HTTPException, ConnectionError, OSError):
                pass
            stalled.close()
            save(out / 'shutdown.json', {'seconds': time.monotonic() - started})
            shutdown_trace = (out / 'server.log').read_text().split('shutting down: draining in-flight requests...')[-1]
            assert 'shutdown complete (0 in-flight remaining)' in shutdown_trace
            progressed = re.findall(r'prefill cancelled seq=\d+ position=(\d+) total=45056', shutdown_trace)
            assert progressed and 0 < int(progressed[-1]) < 45056

            records.append('shutdown drains active request and interrupts incomplete HTTP reader')
        except BaseException as exc:
            failure = repr(exc)
            raise
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            if (narrow or same_mode) and failure is None:
                try:
                    assert server.returncode == 0, 'server did not stop normally'
                    if same_mode:
                        paths = recovery_contract.validate_same_mode_paths(
                            (out / 'server.log').read_text(), verifier)
                        save(out / 'request-modes.json', paths)
                    else:
                        check_narrow_paths()
                except Exception as error:
                    failure = repr(error)
            save(out / 'summary.json', {'records': records, 'failure': failure,
                 'server_exit': server.returncode,
                 'scope': args.scope,
                 'passed': failure is None and server.returncode == 0})
            if (narrow or same_mode) and failure is not None:
                raise RuntimeError(failure)
            if narrow or same_mode:
                print(json.dumps(records), flush=True)
    print(json.dumps(records), flush=True)


if __name__ == '__main__':
    main()
