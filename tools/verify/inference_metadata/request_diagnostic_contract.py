"""Bounded real HTTP diagnostic contracts; caller holds the GPU lease."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import secrets
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--quality-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(exist_ok=False)
    command = json.loads((args.quality_run / 'server-command.json').read_text())['argv']
    command[0] = str(args.binary.resolve())
    command[command.index('--max-len') + 1] = '65536'
    port = int(command[command.index('--port') + 1])
    (out / 'command.json').write_text(json.dumps(command, indent=2))
    rows = json.loads((args.reference / 'inputs/manifest.json').read_text())
    requests = [json.loads(line) for line in
                (args.reference / 'inputs/requests.jsonl').read_text().splitlines()]
    prompts = {length: next(req['prompt'] for row, req in zip(rows, requests)
                           if row['length'] == length)
               for length in (1024, 45056, 204800)}
    records = []

    def http(path, payload=None, raw=None):
        connection = HTTPConnection('127.0.0.1', port, timeout=180)
        try:
            body = raw if raw is not None else (
                None if payload is None else json.dumps(payload))
            connection.request('GET' if body is None else 'POST', path, body,
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return {'status': response.status, 'body': response.read().decode()}
        finally:
            connection.close()

    def payload(identity, length=1024, **extra):
        return dict(model='qwen3.8-flash-next', prompt=prompts[length],
                    max_tokens=32, temperature=0, stream=True,
                    request_id=identity, cancel_token=secrets.token_hex(32), **extra)

    def wait_for(check, seconds=60):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return
            if server.poll() is not None:
                raise RuntimeError('server exited')
            time.sleep(0.02)
        raise RuntimeError('condition timed out')

    def log_has(identity, event):
        return any(' id=' + identity + ' ' in line and event in line
                   for line in (out / 'server.log').read_text().splitlines())

    def record(name, result, status):
        (out / (name + '.json')).write_text(json.dumps(result, indent=2))
        if result['status'] != status:
            raise ValueError(name + ' unexpected HTTP status')
        records.append(name)

    with (out / 'server.log').open('w') as log:
        server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for(lambda: 'serving on port' in
                     (out / 'server.log').read_text(), 300)
            record('invalid-json', http('/v1/chat/completions', raw='{'), 400)
            record('over-capacity', http('/v1/chat/completions',
                                         payload('diag-over', 204800)), 400)
            with ThreadPoolExecutor(max_workers=3) as pool:
                blocker = pool.submit(http, '/v1/chat/completions',
                                      payload('diag-blocker', 45056))
                wait_for(lambda: log_has('diag-blocker', 'event=admitted'))
                queued = payload('diag-reuse')
                cancelled = pool.submit(http, '/v1/chat/completions', queued)
                wait_for(lambda: log_has('diag-reuse', 'stage=slot_admission'))
                cancel = http('/v1/requests/cancel',
                              {key: queued[key] for key in
                               ('request_id', 'cancel_token')})
                record('cancel-ack', cancel, 202)
                record('queued-cancel', cancelled.result(timeout=30), 409)
                record('queued-deadline', http('/v1/chat/completions',
                       payload('diag-deadline', request_timeout_ms=50)), 409)
                record('blocker', blocker.result(timeout=180), 200)
            record('id-reuse', http('/v1/chat/completions', payload('diag-reuse')), 200)
            normal = payload('diag-nonstream')
            normal['stream'] = False
            record('nonstream', http('/v1/chat/completions', normal), 200)
            wait_for(lambda: log_has('diag-nonstream', 'event=end'))
            lines = [line for line in (out / 'server.log').read_text().splitlines()
                     if line.startswith('[q4t][request]')]
            events = [dict(item.split('=', 1) for item in line.split()[1:])
                      for line in lines]
            instances = {}
            for event in events:
                instances.setdefault(event['instance'], []).append(event)
            if len(instances) != 7:
                raise ValueError('expected seven handler instances')
            for group in instances.values():
                ends = [e for e in group if e['event'] == 'end']
                if len(ends) != 1 or group[0]['event'] != 'begin' or len(group) > 9:
                    raise ValueError('lifecycle incomplete or unbounded')
                end = ends[0]
                if end['seq'] != '-1' and end['released'] != '1':
                    raise ValueError('slot not released')
                times = [float(e['elapsed_ms']) for e in group]
                if times != sorted(times):
                    raise ValueError('nonmonotonic event time')
            ends = [e for e in events if e['event'] == 'end']
            reused = [e for e in ends if e['id'] == 'diag-reuse']
            if {e['outcome'] for e in reused} != {'success', 'cancelled'}:
                raise ValueError('ID reuse outcomes not separated')
            if not any(e['reason'] == 'deadline_exceeded' for e in ends):
                raise ValueError('deadline reason absent')
            if not any(e['reason'] == 'explicit_cancel' for e in reused):
                raise ValueError('cancel reason absent')
            if not any(e['id'] == 'diag-over' and e['outcome'] == 'error' for e in ends):
                raise ValueError('registered error missing')
            if any('cancel_token' in line or queued['cancel_token'] in line for line in lines):
                raise ValueError('credential in diagnostic log')
            (out / 'diagnostics.json').write_text(json.dumps(events, indent=2))
            (out / 'result.json').write_text(json.dumps(
                {'status': 'PASS', 'handlers': 7, 'cases': records}, indent=2))
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            (out / 'exit.json').write_text(json.dumps({'server': server.returncode}))
        if server.returncode != 0:
            raise ValueError('server exit failed')


if __name__ == '__main__':
    main()
