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
    p.add_argument('--scope', choices=['full', 'decode-recovery'], default='full')
    p.add_argument('--reference-run', type=Path,
                   help='Completed ordinary-decode run with fixed 1K performance evidence')
    p.add_argument('--mtp', action='store_true')
    p.add_argument('--port', type=int)
    p.add_argument('--startup-timeout', type=int, default=180)
    args = p.parse_args()
    narrow = args.scope == 'decode-recovery'
    if args.startup_timeout <= 0:
        p.error('--startup-timeout must be positive')
    if narrow and (not args.mtp or args.reference_run is None):
        p.error('decode-recovery requires --mtp and --reference-run')
    if not narrow and (args.quality_run is None or args.performance_run is None):
        p.error('full scope requires --quality-run and --performance-run')
    if not narrow and (args.mtp or args.reference_run is not None):
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
    })
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff'], cwd=ROOT))
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    keys = {}
    records = []

    def payload(label, fixture, timeout=None):
        key = secrets.token_hex(32)
        keys[label] = key
        d = dict(model='qwen3.8-flash-next', prompt=fixtures[fixture], stream=True,
                 max_tokens=256 if fixture == 'decode' else 32, temperature=0,
                 stream_options={'include_usage': True}, request_id=label, cancel_token=key)
        if narrow:
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
            if narrow and label:
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
            if narrow:
                startup = startup_evidence((out / 'server.log').read_text(), True)
                save(out / 'startup-mode.json', startup)
                assert startup['passed'], startup['errors']
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
            if narrow and failure is None:
                try:
                    assert server.returncode == 0, 'server did not stop normally'
                    check_narrow_paths()
                except Exception as error:
                    failure = repr(error)
            save(out / 'summary.json', {'records': records, 'failure': failure,
                 'server_exit': server.returncode,
                 'scope': args.scope,
                 'passed': failure is None and server.returncode == 0})
            if narrow and failure is not None:
                raise RuntimeError(failure)
            if narrow:
                print(json.dumps(records), flush=True)
    print(json.dumps(records), flush=True)


if __name__ == '__main__':
    main()
