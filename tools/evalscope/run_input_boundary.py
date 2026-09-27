"""Real HTTP rejection and recovery tests for bounded JSON/HTTP parsing."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--quality-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--port', type=int, default=18093)
    ap.add_argument('--contract', action='store_true',
                    help='Also check the text/greedy parameter contract')
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    prior = args.quality_run.resolve()
    rows = json.loads((prior / 'results.json').read_text())
    expected = next(r for r in rows if r['actual_input'] == 1024)
    requests = [json.loads(s) for s in (prior / 'inputs/requests.jsonl').read_text().splitlines()]
    prompt = next(r['prompt'] for r in requests if hashlib.sha256(
        r['prompt'].encode()).hexdigest() == expected['prompt_sha256'])
    payload = {'prompt': prompt, 'max_tokens': 32, 'stream': False}
    if args.contract:
        payload.update(temperature=0, top_p=1, n=1, seed=20260920,
                       presence_penalty=0, frequency_penalty=0,
                       repetition_penalty=1, stop=[], logprobs=False,
                       top_logprobs=0, logit_bias={},
                       response_format={'type': 'text'}, user='contract-test')
    command = json.loads((prior / 'server-command.json').read_text())['argv']
    command[0] = str(args.binary.resolve())
    for k, v in [('--port', str(args.port)), ('--max-len', '8192')]:
        command[command.index(k) + 1] = v
    save(out / 'identity.json', {'command': command,
         'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest()})
    records = []

    def request_http(path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', args.port, timeout=120)
        try:
            conn.request('GET' if data is None else 'POST', path, data,
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return {'status': response.status, 'body': response.read().decode()}
        finally:
            conn.close()

    def healthy():
        response = request_http('/healthz')
        assert response['status'] == 200
        body = json.loads(response['body'])
        assert body['gpu_healthy'] and body['seq_slots_free'] == 1
        return response

    def normal(label):
        response = request_http('/v1/chat/completions', json.dumps(payload))
        save(out / (label + '.json'), {'request': payload, 'response': response})
        assert response['status'] == 200
        body = json.loads(response['body'])
        assert body['choices'][0]['message']['content'] == expected['text']
        assert body['choices'][0]['finish_reason'] == 'stop'
        assert body['usage']['prompt_tokens'] == expected['actual_input']
        assert body['usage']['completion_tokens'] == expected['actual_output']

    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
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
            normal('before')
            invalid = {
                'deep': b'[' * 100000 + b'0' + b']' * 100000,
                'nodes': b'{"padding":[' + b'0,' * 65536 + b'0]}',
                'exponent': b'{"max_tokens":1e}',
                'leading-zero': b'{"max_tokens":01}',
                'overflow': b'{"max_tokens":1e999}',
                'control': b'{"prompt":"a\tb"}',
                'surrogate': b'{"prompt":"\\ud800"}',
                'utf8': b'{"prompt":"\xc0\xaf"}',
                'duplicate': b'{"max_tokens":1,"max_\\u0074okens":2}',
                'array-root': b'[]',
                'stream-type': b'{"prompt":"hello","stream":1}',
            }
            for label, arguments in [
                    ('tool-deep', '[' * 100000 + '0' + ']' * 100000),
                    ('tool-duplicate', '{"x":1,"x":2}')]:
                invalid[label] = json.dumps({'messages': [
                    {'role': 'user', 'content': 'call tool'},
                    {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'type': 'function', 'function': {'name': 'f',
                         'arguments': arguments}}]},
                    {'role': 'tool', 'content': 'done'},
                    {'role': 'user', 'content': 'continue'}]}).encode()
            for n in [0, -1, 1.5, '1', True, None, 2147483648, 1e20]:
                invalid['max-tokens-' + repr(n)] = json.dumps(
                    {'prompt': 'hello', 'max_tokens': n}).encode()
            if args.contract:
                fields = {'temperature': 0.8, 'top_p': 0.9, 'n': 2,
                          'presence_penalty': 1, 'frequency_penalty': 1,
                          'repetition_penalty': 1.1, 'stop': ['END'],
                          'logprobs': True, 'top_logprobs': 1,
                          'logit_bias': {'1': 1}, 'top_k': 1,
                          'response_format': {'type': 'json_object'},
                          'tool_choice': 'required', 'parallel_tool_calls': False,
                          'max_completion_tokens': 1, 'model': 'unknown',
                          'temprature': 0, 'seed': 9007199254740992,
                          'stream_options': {'include_usage': 1},
                          'chat_template_kwargs': {'enable_thinking': False}}
                for key, value in fields.items():
                    invalid['contract-' + key] = json.dumps(
                        {'prompt': 'hello', key: value}).encode()
                invalid['contract-both-inputs'] = json.dumps(
                    {'prompt': 'hello', 'messages': [{'role': 'user', 'content': 'hello'}]}).encode()
                for kind, part in [('image', {'type': 'image_url', 'image_url': {'url': 'invalid'}}),
                                   ('video', {'video_frames': ['invalid']})]:
                    invalid['media-' + kind] = json.dumps(
                        {'messages': [{'role': 'user', 'content': [part]}]}).encode()
            for label, data in invalid.items():
                response = request_http('/v1/chat/completions', data)
                (out / (label + '-request.bin')).write_bytes(data)
                save(out / (label + '-response.json'), response)
                assert response['status'] == 400, label
                if label.startswith('media-'):
                    assert 'media disabled' in response['body']
                records.append({'case': label, 'health': healthy()})
            # Cancellation shares the same parser budgets and strict grammar.
            for label in ['deep', 'nodes', 'duplicate']:
                response = request_http('/v1/requests/cancel', invalid[label])
                save(out / ('cancel-' + label + '.json'), response)
                assert response['status'] == 400
                records.append({'case': 'cancel-' + label, 'health': healthy()})
            fields = {
                'x-length': b'X-Content-Length: 2',
                'junk-length': b'Content-Length: 2junk',
                'duplicate-length': b'Content-Length: 2\r\nContent-Length: 99',
                'te-cl': b'Transfer-Encoding: chunked\r\nContent-Length: 2',
                'chunked': b'Transfer-Encoding: chunked',
                'large-body': b'Content-Length: 16777217',
                'oversize-header': b'X: ' + b'x' * 65536,
                'expect': b'Expect: 100-continue',
            }
            for label, header in fields.items():
                wire = b'POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n' + header + b'\r\n\r\n{}'
                (out / (label + '-request.bin')).write_bytes(wire)
                with socket.create_connection(('127.0.0.1', args.port), timeout=30) as conn:
                    conn.sendall(wire)
                    response = http.client.HTTPResponse(conn)
                    response.begin()
                    raw = {'status': response.status, 'body': response.read().decode()}
                save(out / (label + '-response.json'), raw)
                assert raw['status'] == 400, label
                records.append({'case': label, 'health': healthy()})
            normal('after')
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
            save(out / 'summary.json', {'failure': failure, 'server_exit': server.returncode,
                                       'rejections': records})
    assert server.returncode == 0
    print(f'{len(records)} rejected cases with healthy recovery; normal before/after match', flush=True)


if __name__ == '__main__':
    main()
