"""Check serve CLI rejection and real default/media initialization over HTTP."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--quality-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out, binary = args.output.resolve(), args.binary.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / 'run.py')
    save = lambda name, obj: (out / name).write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + '\n')
    save('identity.json', {'binary': str(binary), 'sha256':
                           hashlib.sha256(binary.read_bytes()).hexdigest()})
    # Do not interfere with an existing service.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe.name.startswith('q4t') and b'serve' in argv)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    rejected = []
    for tail in [[], ['--max-seq', '0'], ['--max-seq', '2junk'],
                 ['--max-seq', '2147483647'], ['--max-seq', '-1'],
                 ['--max-tokens', '0'], ['--max-len', '-1'],
                 ['--mem-fraction', 'nan'], ['--mem-fraction', '1.1'],
                 ['--port', '65536'], ['--port', '8000oops'],
                 ['--max-prefill'], ['--host', 'localhost'], ['--unknown']]:
        # The missing-value case uses only --port; other cases prove rejection
        # precedes opening an intentionally nonexistent model directory.
        command = [str(binary), 'serve'] + (
            ['--model-dir', '/nonexistent/q4t-options-test'] + tail
            if tail else ['--port'])
        run = subprocess.run(command, capture_output=True, text=True,
                             env=env, timeout=10)
        item = {'command': command, 'exit': run.returncode,
                'stdout': run.stdout, 'stderr': run.stderr}
        rejected.append(item)
        save('rejections.json', rejected)
        assert run.returncode == 2 and 'serve options:' in run.stderr
        assert '[startup]' not in run.stderr and 'tokenizer load' not in run.stderr
    prior = args.quality_run.resolve()
    rows = json.loads((prior / 'results.json').read_text())
    ref = next(r for r in rows if r['actual_input'] == 1024)
    inputs = [json.loads(s) for s in
              (prior / 'inputs/requests.jsonl').read_text().splitlines()]
    fixture = next(r for r in inputs if hashlib.sha256(
        r['prompt'].encode()).hexdigest() == ref['prompt_sha256'])
    payload = {'prompt': fixture['prompt'], 'max_tokens': 32,
               'temperature': 0, 'stream': True,
               'stream_options': {'include_usage': True}}
    base = json.loads((prior / 'server-command.json').read_text())['argv']
    model = base[base.index('--model-dir') + 1]
    port = 18094

    def request(route, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
        try:
            conn.request('GET' if data is None else 'POST', route,
                         None if data is None else json.dumps(data),
                         {'Content-Type': 'application/json'})
            r = conn.getresponse()
            return {'status': r.status, 'body': r.read().decode()}
        finally:
            conn.close()

    records = []
    for media in [False, True]:
        name = 'media-enabled' if media else 'default'
        command = [str(binary), 'serve', '--model-dir', model, '--port', str(port)]
        if media:
            command.append('--allow-media')
        save(name + '-plan.json', {'command': command, 'request': payload,
                                   'expected': ref})
        log_path = out / (name + '-server.log')
        server = None
        record = {'mode': name, 'failure': None}
        try:
            with log_path.open('w') as log:
                server = subprocess.Popen(command, env=env, stdout=log,
                                          stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 180
            while 'serving on port' not in log_path.read_text():
                assert server.poll() is None, 'startup failed'
                assert time.monotonic() < deadline, 'startup timeout'
                time.sleep(1)
            startup = log_path.read_text()
            assert 'MTP disabled; plain decode' in startup
            assert (f'effective mtp=0 media_allowed={int(media)} '
                    f'vision_loaded={int(media)} max_seq=1') in startup
            assert ('[startup] vision_load' in startup) == media
            health = request('/healthz')
            save(name + '-health.json', health)
            h = json.loads(health['body'])
            assert health['status'] == 200 and h['gpu_healthy']
            assert h['seq_slots_total'] == h['seq_slots_free'] == 1
            if not media:
                rejected_media = request('/v1/chat/completions', {
                    'messages': [{'role': 'user', 'content': [{'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,invalid'}}]}]})
                save('media-rejection.json', rejected_media)
                assert rejected_media['status'] == 400
            response = request('/v1/chat/completions', payload)
            save(name + '-response.json', response)
            assert response['status'] == 200
            lines = [s[6:] for s in response['body'].splitlines()
                     if s.startswith('data: ')]
            assert lines[-1] == '[DONE]'
            events = [json.loads(s) for s in lines[:-1]]
            assert not any('error' in e for e in events)
            choices = [c for e in events for c in e.get('choices', [])]
            assert ''.join(c.get('delta', {}).get('content', '')
                           for c in choices) == ref['text']
            assert [c['finish_reason'] for c in choices
                    if c.get('finish_reason')] == ['stop']
            usage = next(e['usage'] for e in events if e.get('usage'))
            assert usage['prompt_tokens'] == ref['actual_input']
            assert usage['completion_tokens'] == ref['actual_output']
            after = request('/healthz')
            save(name + '-health-after.json', after)
            assert after['status'] == 200
            assert json.loads(after['body'])['seq_slots_free'] == 1
        except BaseException as exc:
            record['failure'] = repr(exc)
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                    record['failure'] = record['failure'] or 'shutdown timeout'
                record['server_exit'] = server.returncode
            records.append(record)
            save('summary.json', {'rejections': len(rejected), 'records': records})
        assert record['failure'] is None and record['server_exit'] == 0, record
    print('14 CLI rejections; default and explicit media initialization: PASS')


if __name__ == '__main__':
    main()
