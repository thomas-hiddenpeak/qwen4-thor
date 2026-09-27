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

ROOT = Path(__file__).resolve().parents[2]


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--quality-run', type=Path, required=True)
    p.add_argument('--performance-run', type=Path, required=True)
    args = p.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
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
    port = int(cmd[cmd.index('--port') + 1])
    save(out / 'plan.json', {'command': cmd, 'cases': ['queued explicit/FIN/RST/deadline', 'duplicate/wrong key', 'prefill explicit/FIN/RST', 'surviving request', 'decode explicit/deadline', 'ID reuse', 'shutdown with stalled reader'], 'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest()})
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
        if timeout is not None:
            d['request_timeout_ms'] = timeout
        return d

    def http(path, body=None, first=None, label=None):
        c = HTTPConnection('127.0.0.1', port, timeout=300)
        if label:
            save(out / (label + '-request.json'), body)
        try:
            c.request('GET' if body is None else 'POST', path,
                      None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            r = c.getresponse()
            raw = bytearray()
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

    failure = None
    with (out / 'server.log').open('w') as log, ThreadPoolExecutor(max_workers=4) as pool:
        server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(180):
                assert server.poll() is None
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError('startup timeout')
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
        except Exception as exc:
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
            save(out / 'summary.json', {'records': records, 'failure': failure, 'server_exit': server.returncode})
    print(json.dumps(records), flush=True)


if __name__ == '__main__':
    main()
