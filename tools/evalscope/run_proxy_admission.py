"""Qualify real nginx cancellation and bounded admission; no throughput claim."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--nginx', type=Path, required=True)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--lifecycle', type=Path, required=True)
    ap.add_argument('--quality-run', type=Path, required=True)
    ap.add_argument('--performance-run', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    save = lambda name, obj: (out / name).write_text(json.dumps(obj, indent=2) + '\n')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    prior = args.lifecycle.resolve()
    plan = json.loads((prior / 'plan.json').read_text())
    cmd = plan['command']
    cmd[0] = str(args.binary.resolve())
    cmd[cmd.index('--max-seq') + 1] = '1'
    cmd[cmd.index('--max-len') + 1] = '208896'
    back = int(cmd[cmd.index('--port') + 1])
    front = back + 80
    template = ROOT / 'tools/deploy/nginx/q4t.conf.in'
    text = template.read_text().replace('@PREFIX@', str(out)).replace('@BACK_PORT@', str(back)).replace('@FRONT_PORT@', str(front))
    (out / 'nginx.conf').write_text(text)
    nginx = [str(args.nginx.resolve()), '-p', str(out), '-c', str(out / 'nginx.conf')]
    shutil.copy2(__file__, out)
    shutil.copy2(template, out)
    save('plan.json', dict(server=cmd, nginx=nginx, binary_sha256=sha(args.binary), nginx_sha256=sha(args.nginx), template_sha256=sha(template), generation_limit=4, control_limit=8))
    (out / 'nginx-version.txt').write_bytes(subprocess.check_output([nginx[0], '-V'], stderr=subprocess.STDOUT))
    (out / 'config-check.txt').write_bytes(subprocess.check_output(nginx + ['-t'], stderr=subprocess.STDOUT))
    (out / 'source-commit.txt').write_bytes(subprocess.check_output(['git', 'rev-parse', 'HEAD']))
    records = []
    sockets = []

    def http(path, body=None, port=front):
        c = HTTPConnection('127.0.0.1', port, timeout=300)
        try:
            c.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            r = c.getresponse()
            return dict(status=r.status, body=r.read().decode())
        finally:
            c.close()

    def metric(key):
        r = http('/metrics')
        assert r['status'] == 200, r
        return int(next(x.split()[1] for x in r['body'].splitlines() if x.startswith(key + ' ')))

    def wait(predicate, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.05)
        raise AssertionError('condition timeout')

    def payload(label, short=False):
        name = 'canary-request.json' if short else 'active-a-request.json'
        d = json.loads((prior / name).read_text())
        d.update(request_id=label, cancel_token=secrets.token_hex(32))
        save(label + '-request.json', d)
        return d

    def cancel(d):
        return http('/v1/requests/cancel', {k: d[k] for k in ['request_id', 'cancel_token']})

    def wire(d, port=front):
        body = json.dumps(d).encode()
        s = socket.create_connection(('127.0.0.1', port), timeout=10)
        sockets.append(s)
        s.sendall(f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        return s

    failure = None
    proxy = None
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    with (out / 'server.log').open('w') as log, (out / 'proxy.log').open('w') as proxy_log, ThreadPoolExecutor(max_workers=4) as pool:
        server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait(lambda: 'serving on port' in (out / 'server.log').read_text() or server.poll() is not None, 180)
            assert server.poll() is None
            proxy = subprocess.Popen(nginx + ['-g', 'daemon off;'], stdout=proxy_log, stderr=subprocess.STDOUT)
            def proxy_ready():
                assert proxy.poll() is None, 'nginx exited before readiness'
                try:
                    return http('/healthz')['status'] == 200
                except ConnectionRefusedError:
                    return False
            wait(proxy_ready)
            # Reproduce the unprotected backend cap without model computation.
            for _ in range(128):
                s = socket.create_connection(('127.0.0.1', back), timeout=5)
                sockets.append(s)
                s.sendall(b'POST /v1/chat/completions HTTP/1.1\r\n')
            time.sleep(.3)
            blocked = http('/v1/requests/cancel', {'request_id': 'missing', 'cancel_token': 'a' * 64}, port=back)
            save('direct-cap-response.json', blocked)
            assert blocked['status'] == 503
            for s in sockets:
                s.close()
            sockets.clear()
            wait(lambda: http('/healthz')['status'] == 200)
            records.append('direct backend cap blocks cancellation (known deployment constraint)')
            # Slow client uploads stay at nginx and do not occupy backend threads.
            before = metric('q4t_num_requests_running')
            for _ in range(4):
                s = socket.create_connection(('127.0.0.1', front), timeout=5)
                sockets.append(s)
                s.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100000\r\n\r\n{')
            time.sleep(.3)
            assert metric('q4t_num_requests_running') == before
            assert http('/healthz')['status'] == 200
            for s in sockets:
                s.close()
            sockets.clear()
            time.sleep(.2)
            records.append('buffered partial uploads do not consume backend threads')
            # Fill the generation budget with one running and three queued jobs.
            initial = metric('q4t_requests_total')
            jobs = [payload('full-' + str(i)) for i in range(4)]
            futures = [pool.submit(http, '/v1/chat/completions', d) for d in jobs]
            wait(lambda: metric('q4t_requests_total') == initial + 4)
            extra = http('/v1/chat/completions', payload('overload', True))
            save('overload-response.json', extra)
            assert extra['status'] == 429
            assert metric('q4t_requests_total') == initial + 4
            assert http('/healthz')['status'] == 200
            start = time.monotonic()
            for d in reversed(jobs):
                r = cancel(d)
                assert r['status'] == 202, r
            save('full-cancel.json', {'seconds': time.monotonic() - start})
            for i, f in enumerate(futures):
                r = f.result(timeout=30)
                save(f'full-{i}-response.json', r)
                assert r['status'] == 409
            wait(lambda: json.loads(http('/healthz')['body'])['seq_slots_free'] == 1)
            records.append('full generation budget rejects excess before backend; control remains available')
            # FIN and RST before the first response must reach the upstream.
            for reset in [False, True]:
                label = 'proxy-rst' if reset else 'proxy-fin'
                n = metric('q4t_requests_aborted_total')
                s = wire(payload(label))
                wait(lambda: json.loads(http('/healthz')['body'])['seq_slots_free'] == 0)
                time.sleep(.5)
                if reset:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                s.close()
                start = time.monotonic()
                wait(lambda: metric('q4t_requests_aborted_total') == n + 1)
                save(label + '-result.json', {'cancel_observed_seconds': time.monotonic() - start})
                wait(lambda: json.loads(http('/healthz')['body'])['seq_slots_free'] == 1)
            records.append('proxy FIN/RST before response aborts prefill and recovers slot')
            # Streaming response cancellation must also propagate after headers.
            n = metric('q4t_requests_aborted_total')
            d = payload('decode-disconnect', True)
            d['prompt'] = json.loads((args.performance_run / 'inputs/context-1024.jsonl').read_text().splitlines()[0])['prompt']
            d['max_tokens'] = 256
            save('decode-disconnect-request.json', d)
            c = HTTPConnection('127.0.0.1', front, timeout=30)
            c.request('POST', '/v1/chat/completions', json.dumps(d), {'Content-Type': 'application/json'})
            r = c.getresponse()
            assert r.status == 200
            prefix = []
            while True:
                line = r.readline()
                assert line, 'stream ended before first content'
                prefix.append(line.decode())
                if line.startswith(b'data: {'):
                    event = json.loads(line[6:])
                    if any(x.get('delta', {}).get('content') for x in event.get('choices', [])):
                        break
            r.close()
            c.close()
            (out / 'decode-disconnect-prefix.sse').write_text(''.join(prefix))
            wait(lambda: metric('q4t_requests_aborted_total') == n + 1)
            wait(lambda: json.loads(http('/healthz')['body'])['seq_slots_free'] == 1)
            records.append('proxy disconnect after first content cancels decode')
            # Long prefill exceeds nginx's usual 60-second upstream idle default.
            quality = args.quality_run.resolve()
            row = next(x for x in json.loads((quality / 'results.json').read_text()) if x['actual_input'] == 204800)
            prompts = [json.loads(x)['prompt'] for x in (quality / 'inputs/requests.jsonl').read_text().splitlines()]
            d = payload('context-200k')
            d['prompt'] = next(x for x in prompts if hashlib.sha256(x.encode()).hexdigest() == row['prompt_sha256'])
            save('context-200k-request.json', d)
            start = time.monotonic()
            result = http('/v1/chat/completions', d)
            save('context-200k-response.json', result)
            elapsed = time.monotonic() - start
            assert result['status'] == 200 and result['body'].strip().endswith('data: [DONE]')
            events = [json.loads(x[6:]) for x in result['body'].splitlines() if x.startswith('data: {')]
            choices = [c for e in events for c in e.get('choices', [])]
            assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == row['text']
            assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
            usage = next(e['usage'] for e in events if e.get('usage'))
            assert usage['prompt_tokens'] == 204800 and usage['completion_tokens'] == row['actual_output']
            save('context-200k-result.json', {'seconds': elapsed, 'usage': usage, 'reference_prompt_sha256': row['prompt_sha256'], 'reference_text': row['text']})
            records.append('200K complete output and usage match reference through proxy')
            # Repeated recovery checks validate output, not merely HTTP 200.
            for i in range(3):
                r = http('/v1/chat/completions', payload('recovery-' + str(i), True))
                save(f'recovery-{i}-response.json', r)
                assert r['status'] == 200 and r['body'].strip().endswith('data: [DONE]')
                events = [json.loads(x[6:]) for x in r['body'].splitlines() if x.startswith('data: {')]
                choices = [c for e in events for c in e.get('choices', [])]
                assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == '710003'
                assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
            records.append('three post-overload recovery outputs match frozen reference')
        except Exception as exc:
            failure = repr(exc)
            raise
        finally:
            for s in sockets:
                s.close()
            if proxy and proxy.poll() is None:
                proxy.terminate()
                proxy.wait(timeout=20)
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save('exit.json', dict(failure=failure, server=server.returncode, proxy=proxy.returncode if proxy else None, records=records))
    assert server.returncode == 0 and proxy.returncode == 0
    assert 'shutdown complete (0 in-flight remaining)' in (out / 'server.log').read_text()
    print('proxy admission qualification passed', flush=True)


if __name__ == '__main__':
    main()
