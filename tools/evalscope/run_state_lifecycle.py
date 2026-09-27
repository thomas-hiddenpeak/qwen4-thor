"""Bounded HTTP state regression; no performance acceptance claim.

Direct HTTP allows deterministic TCP reset injection. Full raw responses and
pool occupancy are retained; matching text does not prove matching logits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--reference-run', type=Path, required=True)
    ap.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--require-early-cancel', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    source = args.reference_run.resolve()
    rows = json.loads((source / 'results.json').read_text())
    inputs = [json.loads(x) for x in (source / 'inputs/requests.jsonl').read_text().splitlines()]
    cases = {}
    for label, length in [('A', 1024), ('B', 45056)]:
        row = next(r for r in rows if r['actual_input'] == length)
        fixture = next(x for x in inputs if hashlib.sha256(x['prompt'].encode()).hexdigest() == row['prompt_sha256'])
        cases[label] = dict(model='qwen3.8-flash-next', prompt=fixture['prompt'],
                            temperature=0, max_tokens=32, stream=True,
                            stream_options={'include_usage': True})
    command = json.loads((source / 'server-command.json').read_text())['argv']
    command[0] = str(args.binary.resolve())
    for flag, value in [('--port', str(args.port)), ('--max-seq', '2'), ('--max-len', '65536')]:
        command[command.index(flag) + 1] = value
    save(out / 'plan.json', {'command': command, 'cases': cases,
         'require_early_cancel': args.require_early_cancel,
         'checks': ['A-B-A', 'long-short overlap twice', 'prefill reset then A',
                    'decode reset then A'],
         'limits': 'HTTP text/usage/finish and occupancy, not all logits or performance; no fault injection'})
    save(out / 'identity.json', {'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
         'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()})
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff'], cwd=ROOT))
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    records = []

    def get(path):
        conn = http.client.HTTPConnection('127.0.0.1', args.port, timeout=10)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            data = response.read().decode()
            assert response.status == 200, data
            return json.loads(data) if path == '/healthz' else data
        finally:
            conn.close()

    def idle(label):
        for _ in range(600):
            h = get('/healthz')
            if h['seq_slots_free'] == 2:
                save(out / (label + '-health.json'), h)
                assert h['gpu_healthy'] and h['seq_slots_total'] == 2
                return
            time.sleep(.1)
        raise RuntimeError('slots did not drain')

    def request(label, payload):
        conn = http.client.HTTPConnection('127.0.0.1', args.port, timeout=300)
        save(out / (label + '-request.json'), payload)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(payload), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            raw = response.read()
            (out / (label + '.sse')).write_bytes(raw)
            assert response.status == 200
            data = [line[6:] for line in raw.decode().splitlines() if line.startswith('data: ')]
            assert data[-1] == '[DONE]'
            events = [json.loads(x) for x in data[:-1]]
            result = {'text': ''.join(c.get('delta', {}).get('content', '') for e in events for c in e.get('choices', [])),
                      'finish': [c['finish_reason'] for e in events for c in e.get('choices', []) if c.get('finish_reason')],
                      'usage': next(e['usage'] for e in events if e.get('usage'))}
            save(out / (label + '-result.json'), result)
            return result
        finally:
            conn.close()

    def cancel(label, payload, decode):
        save(out / (label + '-request.json'), payload)
        sock = socket.create_connection(('127.0.0.1', args.port), timeout=300)
        try:
            body = json.dumps(payload).encode()
            sock.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % len(body)).encode() + body)
            raw = b''
            if decode:
                while True:
                    chunk = sock.recv(65536)
                    assert chunk, 'ended before content'
                    raw += chunk
                    lines = raw.split(b'\n')[:-1]
                    if any(json.loads(x[6:]).get('choices', [{}])[0].get('delta', {}).get('content')
                           for x in lines if x.startswith(b'data: {')):
                        break
            else:
                for _ in range(300):
                    if get('/healthz')['seq_slots_free'] == 1:
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('cancel request not admitted')
            (out / (label + '-partial.bin')).write_bytes(raw)
            save(out / (label + '-before-reset.json'), get('/healthz'))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        finally:
            sock.close()
        idle(label)

    failure = None
    with (out / 'server.log').open('w') as log:
        server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(180):
                assert server.poll() is None
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError('startup timeout')
            startup = (out / 'server.log').read_text()
            assert '=> max_len=65536 max_seq=2' in startup
            assert 'MTP disabled; plain decode' in startup
            a = request('A-first', cases['A'])
            b = request('B-first', cases['B'])
            assert a['usage']['prompt_tokens'] == 1024 and b['usage']['prompt_tokens'] == 45056
            assert request('A-reuse', cases['A']) == a
            records.append({'case': 'A-B-A', 'passed': True})
            for repeat in range(2):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    long = pool.submit(request, f'overlap-{repeat}-B', cases['B'])
                    for _ in range(300):
                        if get('/healthz')['seq_slots_free'] == 1:
                            break
                        assert not long.done()
                        time.sleep(.1)
                    else:
                        raise RuntimeError('long not admitted')
                    short = pool.submit(request, f'overlap-{repeat}-A', cases['A'])
                    occupancy = []
                    while not (long.done() and short.done()):
                        occupancy.append(get('/healthz'))
                        time.sleep(.1)
                    save(out / f'overlap-{repeat}-occupancy.json', occupancy)
                    assert any(x['seq_slots_free'] == 0 for x in occupancy)
                    ar, br = short.result(), long.result()
                    records.append({'case': f'overlap-{repeat}', 'A_equal': ar == a, 'B_equal': br == b})
                    # A different packed arithmetic path is retained, not silently accepted.
                idle(f'overlap-{repeat}')
            for label, decode in [('cancel-prefill', False), ('cancel-decode', True)]:
                before = get('/metrics')
                (out / (label + '-metrics-before.txt')).write_text(before)
                cancel(label, cases['B'], decode)
                after = get('/metrics')
                (out / (label + '-metrics-after.txt')).write_text(after)
                def aborted(text):
                    return int(next(x.split()[1] for x in text.splitlines() if x.startswith('q4t_requests_aborted_total ')))
                assert aborted(after) == aborted(before) + 1
                assert request(label + '-recovery', cases['A']) == a
                if args.require_early_cancel and not decode:
                    import re
                    trace = (out / 'server.log').read_text()
                    matches = re.findall(r'prefill cancelled seq=\d+ position=(\d+) total=(\d+)', trace)
                    assert matches and int(matches[-1][0]) < int(matches[-1][1]) == 45056
                    save(out / 'early-cancel.json', {'position': int(matches[-1][0]), 'total': 45056})
                records.append({'case': label, 'passed': True})
                print(label + ': passed', flush=True)
            idle('final')
        except Exception as exc:
            failure = repr(exc)
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save(out / 'summary.json', {'records': records, 'failure': failure, 'server_exit': server.returncode})
        assert server.returncode == 0
    print(json.dumps(records), flush=True)


if __name__ == '__main__':
    main()
