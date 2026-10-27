"""Bounded session-cache HTTP gate; reuse the delivered lease and telemetry."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'session_multi', ROOT / 'tools/mtp_suffix_multi/run_stage.py')
multi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi)
reuse, legacy, http = multi.reuse, multi.legacy, multi.http
save = legacy.save


def validate(plan):
    resumed = plan['stage'] == 'SESSION_A_REMAINING'
    if plan['stage'] not in ['SESSION_A', 'SESSION_A_REMAINING'] or \
            plan['http_requests'] != (18 if resumed else 36):
        raise ValueError('frozen session A scope')
    if len(plan['steps']) != (1 if resumed else 2) or \
            plan['http_loads'] != (1 if resumed else 2):
        raise ValueError('session A start ledger')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    if plan['stage_timeout_seconds'] > 3600:
        raise ValueError('phase A wall budget')


def released_count(raw):
    # RequestDiagnostics::Released updates the end record; there is no
    # standalone event=released line. Count the actual production contract.
    return len(re.findall(r'event=end [^\n]* released=1(?: |$)', raw))


def request(plan, out, name, prompt, tokens, session=None, disconnect=False):
    body = dict(model='qwen3.8-flash-next', prompt=prompt, max_tokens=tokens,
                stream=True, stream_options={'include_usage': True},
                temperature=0)
    if session:
        body['session_id'] = session
    save(out / (name + '-request.json'), body)
    start = time.monotonic()
    req = urllib.request.Request(
        f"http://127.0.0.1:{plan['port']}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    messages, text, first, done, usage = [], '', None, False, None
    with urllib.request.urlopen(req, timeout=600) as response:
        for raw in response:
            if not raw.startswith(b'data: '):
                continue
            value = raw[6:].strip()
            if value == b'[DONE]':
                done = True
                break
            obj = json.loads(value)
            messages.append(obj)
            if 'error' in obj:
                raise RuntimeError('HTTP error: ' + repr(obj))
            if obj.get('usage'):
                usage = obj['usage']
            for choice in obj.get('choices', []):
                content = choice.get('delta', {}).get('content', '')
                if content and first is None:
                    first = time.monotonic()
                text += content
            if disconnect and first is not None:
                break
    result = dict(text=text, messages=messages, usage=usage, done=done,
                  ttft=None if first is None else first-start,
                  latency=time.monotonic()-start, disconnected=disconnect)
    save(out / (name + '-response.json'), result)
    if not disconnect and (not done or not usage):
        raise RuntimeError('missing HTTP terminal/usage')
    return result


def lifecycle(plan, out, check):
    prompt = Path(plan['session_prompt']).read_text()
    key = hashlib.sha256(b'q4t-session-A-frozen-20261027').hexdigest()
    first = request(plan, out, 'session-save', prompt, 8, key)
    if first['usage']['completion_tokens'] != 8:
        raise RuntimeError('save request did not reach frozen length boundary')
    check()
    request(plan, out, 'slot-overwrite', plan['short_prompt'], 16)
    # Full text is still sent. The cached target prefix must survive a different
    # request using the same physical slot; the new tool turn changes draft b.
    resumed = prompt + first['text'] + (
        '<|im_end|>\n<|im_start|>user\nIgnore the previous task. '
        'Reply with exactly the number 43 and nothing else.<|im_end|>\n'
        '<|im_start|>assistant\n<think>\n\n</think>\n\n')
    hit = request(plan, out, 'session-resume', resumed, 16, key)
    fresh = request(plan, out, 'fresh-reference', resumed, 16)
    if hit['usage']['prompt_tokens_details']['cached_tokens'] < 8192:
        raise RuntimeError('cross-request restore did not reuse long prefix')
    if hit['text'].strip() != '43' or fresh['text'].strip() != '43':
        raise RuntimeError('resumed/fresh exact answer mismatch')
    # Changing an early committed token must never reuse an old state.
    mismatch = request(plan, out, 'prefix-mismatch', resumed.replace(
        'archive', 'record', 1), 16, key)
    if mismatch['usage']['prompt_tokens_details']['cached_tokens'] != 0:
        raise RuntimeError('changed prefix was reused')
    request(plan, out, 'cancel', prompt, 256, key, disconnect=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        check()
        raw = (out / 'server.log').read_text()
        if released_count(raw) >= 17:
            break
        time.sleep(.1)
    else:
        raise TimeoutError('cancelled request did not release slot')
    request(plan, out, 'after-cancel', plan['short_prompt'], 16, key)
    save(out / 'lifecycle.json', dict(passed=True, requests=7,
         cached_tokens=hit['usage']['prompt_tokens_details']['cached_tokens'],
         exact_answers=['43', '43'], pending_boundary_repair=True))


def custom_step(plan, name):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    out = Path(plan['output']) / name
    out.mkdir()
    cache = out / 'cache'
    cache.mkdir(mode=0o700)
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
               '--host', '127.0.0.1', '--port', str(plan['port']),
               '--max-seq', '4', '--max-prefill', '4096', '--max-len', '208896',
               '--seq-capacities', '208896,65536,65536,65536',
               '--mtp-suffix', '--session-cache-dir', str(cache)]
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    env = legacy.clean_env()
    save(out / 'server-command.json', dict(argv=command, environment={
        k:v for k,v in env.items() if k.startswith('Q4T_')}))
    server, failure, passed = None, None, False
    deadline = min(time.time() + step['timeout'] - 45, plan['task_deadline_unix'])
    def check():
        if time.time() >= deadline:
            raise TimeoutError('session A bounded step')
        if server is not None and server.poll() is not None:
            raise RuntimeError('server exited early')
    with (out / 'server.log').open('x') as log:
        try:
            http.event(out, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                        stderr=subprocess.STDOUT)
            http.event(out, 'server_started', pid=server.pid)
            while True:
                check()
                raw = (out / 'server.log').read_text()
                if 'serving on port' in raw:
                    break
                time.sleep(1)
            if 'capacities=208896,65536,65536,65536' not in raw:
                raise RuntimeError('capacity contract changed')
            http.event(out, 'server_ready', pid=server.pid)
            result = multi.case(plan, step, plan['quality_case'], out, env, check)
            save(out / 'first-http-gate.json', dict(passed=True, result=result,
                 candidate_sha256=plan['candidate_sha256']))
            if step['head'] == 'full':
                with (out / 'cpu-contract.log').open('x') as cpu_log:
                    proc = subprocess.run([plan['storage_contract'],
                          str(out / 'cpu-cache')], cwd=ROOT, env=env,
                          stdout=cpu_log, stderr=subprocess.STDOUT, timeout=90)
                if proc.returncode:
                    raise RuntimeError('CPU storage contract failed')
            lifecycle(plan, out, check)
            check()
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http.event(out, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http.event(out, 'server_after_shutdown', pid=server.pid,
                             server_returncode=server.returncode)
            save(out / 'exit.json', dict(server=server.returncode if server else None,
                 http_output_checks_passed=passed and server.returncode == 0,
                 failure=failure))


if __name__ == '__main__':
    from audit import audit_server
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = audit_server
    reuse.main()
