"""Frozen serving reliability HTTP matrix; no throughput acceptance claim.

The entire run must be a child of gpu_exclusive.py. Fault binaries are separate
from the release binary. Requests/startups are charged before attempted IO.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time
import traceback

import gpu_exclusive
import nvml_process_query
from monitor_memory import meminfo, process_memory, process_stat

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fields(raw, prefix):
    return [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw.splitlines()
            if line.startswith(prefix)]


def capability(label):
    return hashlib.sha256(label.encode()).hexdigest()


def turn(text):
    return '<|im_end|>\n<|im_start|>user\n' + text + (
        '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')


def decoded(result, success=True):
    """Every SSE event is independently strict UTF-8/JSON, not byte-repaired."""
    raw = bytes.fromhex(result['wire_hex'])
    text, usage, finish, errors, done = '', None, [], [], 0
    if result['stream'] and result['status'] == 200:
        events = []
        for line in raw.splitlines():
            if not line.startswith(b'data: '):
                continue
            if line == b'data: [DONE]':
                done += 1
                continue
            if done:
                raise AssertionError('event after DONE')
            event = json.loads(line[6:].decode('utf-8', errors='strict'))
            events.append(event)
            if 'error' in event:
                errors.append(event['error'])
            if event.get('usage'):
                usage = event['usage']
            for choice in event.get('choices', []):
                text += choice.get('delta', {}).get('content', '')
                if choice.get('finish_reason') is not None:
                    finish.append(choice['finish_reason'])
        if done != 1 or (success and (errors or len(finish) != 1)):
            raise AssertionError('SSE terminal contract')
        if not success and (not errors or finish):
            raise AssertionError('exception/cancel reported success')
    else:
        event = json.loads(raw.decode('utf-8', errors='strict'))
        if success:
            text = event['choices'][0]['message']['content']
            usage = event['usage']
            finish = [event['choices'][0]['finish_reason']]
        else:
            errors = [event['error']]
    if success and (result['status'] != 200 or not usage or errors):
        raise AssertionError('successful response contract')
    return dict(text=text, usage=usage, finish=finish, errors=errors)


class Runner:
    def __init__(self, plan):
        self.plan = plan
        self.out = Path(plan['output'])
        self.out.mkdir(exist_ok=False)
        self.mu = threading.Lock()
        self.http_count = self.starts = 0
        self.completed = []

    def check(self):
        if time.time() >= self.plan['deadline_unix'] - 45:
            raise TimeoutError('frozen task deadline')

    def charge(self, kind, label):
        with self.mu:
            self.check()
            if kind == 'generation':
                self.http_count += 1
            else:
                self.starts += 1
            if self.http_count > 104 or self.starts > 21:
                raise RuntimeError('measurement budget; health reserve retained')
            with (self.out/'ledger.jsonl').open('a') as log:
                log.write(json.dumps(dict(kind=kind, label=label,
                    time=time.time(), http=self.http_count, starts=self.starts))+'\n')

    def http(self, folder, label, body=None, path='/v1/chat/completions'):
        self.check()
        if body is not None and path == '/v1/chat/completions':
            self.charge('generation', label)
        if folder:
            save(folder/(label+'-request.json'), body)
        conn = HTTPConnection('127.0.0.1', self.plan['port'], timeout=300)
        started, first, wire = time.monotonic(), None, bytearray()
        result = dict(stream=bool(body and body.get('stream')), status=None)
        try:
            conn.request('POST' if body is not None else 'GET', path,
                None if body is None else json.dumps(body),
                {'Content-Type': 'application/json'})
            response = conn.getresponse()
            result['status'] = response.status
            if result['stream'] and response.status == 200:
                while True:
                    self.check()
                    line = response.readline()
                    if not line:
                        break
                    wire.extend(line)
                    if first is None and line.startswith(b'data: {'):
                        event = json.loads(line[6:].decode('utf-8'))
                        if any(c.get('delta', {}).get('content')
                               for c in event.get('choices', [])):
                            first = time.monotonic() - started
            else:
                wire.extend(response.read())
        except Exception as error:
            result['transport_error'] = repr(error)
        finally:
            conn.close()
            result.update(wire_hex=wire.hex(), first_content_seconds=first,
                          seconds=time.monotonic()-started)
            if folder:
                save(folder/(label+'-response.json'), result)
        return result

    def body(self, label, prompt, stream=True, maximum=32, **extra):
        return dict(model='qwen3.8-flash-next', prompt=prompt, stream=stream,
            max_tokens=maximum, temperature=0,
            stream_options={'include_usage': True}, request_id=label,
            cancel_token=capability(label), **extra)

    def health(self):
        response = self.http(None, 'health', path='/healthz')
        if response['status'] != 200:
            raise RuntimeError('health unavailable')
        return json.loads(bytes.fromhex(response['wire_hex']))

    def wait(self, predicate, seconds=30):
        end = min(time.time()+seconds, self.plan['deadline_unix']-45)
        while time.time() < end:
            if predicate():
                return
            time.sleep(.02)
        raise TimeoutError('bounded condition wait')

    def free(self):
        self.wait(lambda: self.health()['seq_slots_free'] == 2, 90)
        if not self.health()['gpu_healthy']:
            raise RuntimeError('GPU unhealthy')

    def cancel(self, folder, label):
        result = self.http(folder, label+'-cancel', dict(request_id=label,
            cancel_token=capability(label)), '/v1/requests/cancel')
        if result['status'] != 202:
            raise AssertionError('cancel not accepted')

    def raw(self, folder, label, body, small=False):
        self.charge('generation', label)
        save(folder/(label+'-request.json'), body)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if small:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(30)
        sock.connect(('127.0.0.1', self.plan['port']))
        data = json.dumps(body).encode()
        sock.sendall(('POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\n'
            'Content-Type: application/json\r\nContent-Length: %d\r\n\r\n'
            % len(data)).encode()+data)
        return sock

    @contextmanager
    def service(self, step):
        self.charge('startup', step['name'])
        folder = self.out/step['name']
        folder.mkdir()
        env = {k:v for k,v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        env.update(step.get('environment', {}))
        if step['kind'] == 'cache':
            env.update(Q4T_SERVE_PAUSE='handler.before_response',
                       Q4T_SERVE_GATE=str(folder/'gate'))
        command = [self.plan[step['binary']], 'serve', '--model-dir',
            self.plan['model'], '--host', '127.0.0.1', '--port',
            str(self.plan['port']), '--max-seq', '2', '--max-prefill', '4096',
            '--max-len', '65536']
        if step['mode'] == 'plain':
            command += ['--no-mtp']
        elif step['mode'] == 'subset':
            command += ['--mtp-subset-head', self.plan['artifact']]
        if step['kind'] == 'cache':
            command += ['--seq-capacities', '65536,65536',
                        '--prefix-cache-checkpoints', '8192']
            for kind in ['session', 'prefix']:
                (folder/kind).mkdir(mode=0o700)
                command += ['--'+kind+'-cache-dir', str(folder/kind)]
        if step['kind'] == 'stats':
            command += ['--moe-resident-slots', '256']
        save(folder/'command.json', dict(argv=command,
            environment={k:v for k,v in env.items() if k.startswith('Q4T_')}))
        stopped = threading.Event()
        samples, monitor_errors = [], []
        server = None
        def monitor():
            while not stopped.is_set():
                try:
                    row = dict(time=time.time(), system=meminfo())
                    if server is not None and server.poll() is None:
                        row.update(pid=server.pid, identity=process_stat(server.pid),
                                   process=process_memory({server.pid}))
                    samples.append(row)
                except Exception as error:
                    monitor_errors.append(repr(error))
                stopped.wait(1)
        worker = threading.Thread(target=monitor)
        worker.start()
        passed, failure = False, None
        try:
            with (folder/'server.log').open('x') as log:
                server = subprocess.Popen(command, cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                def ready():
                    self.check()
                    if server.poll() is not None:
                        raise RuntimeError('server exited during startup')
                    return 'serving on port' in (folder/'server.log').read_text()
                self.wait(ready, 180)
                save(folder/'initial-health.json', self.health())
                if self.health()['seq_slots_total'] != 2:
                    raise AssertionError('silent capacity reduction')
                print(step['name']+': ready', flush=True)
                yield folder, server
                passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            stopped.set()
            worker.join(timeout=5)
            save(folder/'resources.json', dict(samples=samples,
                errors=monitor_errors, scope='separate system and process counters; '
                'not additive physical totals or an SSD capacity acceptance'))
            expected_exit = step.get('exit', 0)
            code = server.returncode if server else None
            raw = (folder/'server.log').read_text()
            begins = fields(raw, '[q4t][request] ')
            groups = {}
            for row in begins:
                groups.setdefault(row['instance'], []).append(row)
            lifecycle = all(sum(r['event']=='begin' for r in rows)==1 and
                sum(r['event']=='end' for r in rows)==1 and
                all(r['event']!='end' or int(r['seq'])<0 or r['released']=='1'
                    for r in rows) for rows in groups.values())
            swaps = [r['process']['process_swap_kb'] for r in samples
                     if r.get('process', {}).get('process_swap_kb') is not None]
            accepted = (passed and code == expected_exit and lifecycle and
                not monitor_errors and not worker.is_alive() and swaps and
                max(swaps) == 0 and 'shutdown complete (0 in-flight remaining)' in raw)
            save(folder/'exit.json', dict(passed=bool(accepted), failure=failure,
                server_exit=code, expected_exit=expected_exit,
                lifecycle=lifecycle, requests=len(groups)))
            if not accepted and failure is None:
                raise RuntimeError('service exit/lifecycle/resource audit')

    def quality(self, step, folder):
        rows = []
        for length in [1024, 8192, 45056]:
            fixture = self.plan['quality'][str(length)]
            for stream in [False, True]:
                label = step['name']+f'-{length}-{int(stream)}'
                response = self.http(folder, label, self.body(label,
                    fixture['prompt'], stream))
                row = decoded(response)
                if (row['text'].strip() != fixture['expected'] or
                        row['usage']['prompt_tokens'] != length):
                    raise AssertionError('quality/length oracle')
                row.update(label=label, length=length, stream=stream,
                           ttft=response['first_content_seconds'],
                           seconds=response['seconds'])
                rows.append(row)
                self.free()
        if step['binary'] == 'candidate':
            prior = json.loads((self.out/('parent-'+step['mode'])/'quality.json').read_text())
            for old, new in zip(prior, rows):
                if any(old[k] != new[k] for k in ['text', 'usage', 'finish']):
                    raise AssertionError('parent/candidate complete response changed')
            metrics = self.http(None, 'metrics', path='/metrics')
            text = bytes.fromhex(metrics['wire_hex']).decode()
            save(folder/'metrics.json', metrics)
            for key, count in [('q4t_ttft_seconds_count', 3),
                               ('q4t_target_ready_seconds_count', 6),
                               ('q4t_response_seconds_count', 6)]:
                if not re.search(r'^'+key+' '+str(count)+r'$', text, re.M):
                    raise AssertionError('metric population: '+key)
        save(folder/'quality.json', rows)

    def fault(self, step, folder, server):
        label = step['name']+'-fault'
        stream = step['point'] in ['handler.after_active', 'handler.before_content',
                                   'scheduler.after_submit']
        result = self.http(folder, label,
            self.body(label, self.plan['decode_prompt'], stream, 128))
        if step.get('exit') == 1:
            if server.wait(timeout=45) != 1:
                raise AssertionError('scheduler failure must exit nonzero')
            if 'scheduler exception:' not in (folder/'server.log').read_text():
                raise AssertionError('scheduler injection did not execute')
        else:
            decoded(result, False)
            self.free()
            label = step['name']+'-recovery'
            fixture = self.plan['quality']['1024']
            row = decoded(self.http(folder, label,
                self.body(label, fixture['prompt'])))
            if row['text'].strip() != fixture['expected']:
                raise AssertionError('post-exception quality')
            self.free()
            if step.get('slow'):
                self.slow(step, folder)

    def slow(self, step, folder):
        label = step['name']+'-slow'
        log_offset = len((folder/'server.log').read_text())
        sock = self.raw(folder, label, self.body(label,
            self.plan['decode_prompt'], True, 256), small=True)
        try:
            self.wait(lambda: 'observed=write.backpressure' in
                      (folder/'server.log').read_text()[log_offset:], 60)
            with ThreadPoolExecutor(max_workers=1) as pool:
                healthy = label+'-peer'
                fixture = self.plan['quality']['1024']
                future = pool.submit(self.http, folder, healthy,
                    self.body(healthy, fixture['prompt']))
                self.wait(lambda: any(r.get('id') == healthy and
                    r['event'] == 'stage' and r['stage'] == 'prefill'
                    for r in fields(
                    (folder/'server.log').read_text(), '[q4t][request] ')))
                start = time.monotonic()
                self.cancel(folder, label)
                row = decoded(future.result(timeout=120))
                self.free()
                if row['text'].strip() != fixture['expected']:
                    raise AssertionError('slow-peer survivor quality')
                save(folder/'slow-result.json', dict(backpressure_observed=True,
                    cancel_to_free_seconds=time.monotonic()-start))
        finally:
            sock.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            label = step['name']+'-explicit'
            future = pool.submit(self.http, folder, label, self.body(label,
                self.plan['quality']['45056']['prompt'], False, 32))
            self.wait(lambda: self.health()['seq_slots_free'] == 1)
            self.cancel(folder, label)
            decoded(future.result(timeout=120), False)
        self.free()
        for suffix, prompt, timeout in [
            ('deadline', self.plan['quality']['45056']['prompt'], 100),
            ('encode', '<|im_start|>'*50000, 10)]:
            label = step['name']+'-'+suffix
            result = self.http(folder, label, self.body(label, prompt, False,
                request_timeout_ms=timeout))
            decoded(result, False)
            self.free()
        label = step['name']+'-unicode'
        result = decoded(self.http(folder, label, self.body(label,
            '<|im_start|>user\n只回复“你好世界”，不要添加其他文字。' +
            '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')))
        if result['text'].strip() != '你好世界':
            raise AssertionError('Unicode recovery quality')
        self.free()

    def cache(self, step, folder):
        prompt = self.plan['cache_prompt']
        scope, session = capability(step['name']), capability(step['name']+'session')
        extra = dict(session_id=session, prefix_cache_scope=scope,
                     prefix_cache_write_tokens=8192)
        label = step['name']+'-failed-send'
        sock = self.raw(folder, label, self.body(label, prompt, False, 16, **extra))
        try:
            self.wait(lambda: (folder/'gate.entered').exists(), 150)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()
            (folder/'gate.release').touch(exist_ok=False)
            self.free()
        finally:
            sock.close()
        self.wait(lambda: any(r['id'] == label and r['event'] == 'end'
            for r in fields((folder/'server.log').read_text(), '[q4t][request] ')))
        raw = (folder/'server.log').read_text()
        end = [r for r in fields(raw, '[q4t][request] ')
               if r['id'] == label and r['event']=='end']
        if len(end) != 1 or end[0]['outcome'] != 'cancelled':
            raise AssertionError('failed send was counted as success')
        label = step['name']+'-seed'
        seed = decoded(self.http(folder, label, self.body(label, prompt, False, 16, **extra)))
        if seed['usage'].get('prompt_tokens_details', {}).get('cached_tokens', 0):
            raise AssertionError('failed private/public response published cache')
        self.free()
        label = step['name']+'-overwrite'
        decoded(self.http(folder, label,
            self.body(label, self.plan['quality']['1024']['prompt'])))
        self.free()
        label = step['name']+'-resume'
        resume = decoded(self.http(folder, label, self.body(label,
            prompt+seed['text']+turn('Reply with exactly 44.'),
            session_id=session, prefix_cache_scope=scope)))
        if (resume['text'].strip() != '44' or
            resume['usage']['prompt_tokens_details']['cached_tokens'] <= 8192):
            raise AssertionError('overwritten private snapshot restore')
        self.free()
        label = step['name']+'-shared'
        child = decoded(self.http(folder, label, self.body(label,
            prompt+seed['text']+turn('What is the audit_code in the authoritative '
                'shared record? Return only its six digits.'), prefix_cache_scope=scope)))
        if (child['text'].strip() != '831907' or
            child['usage']['prompt_tokens_details']['cached_tokens'] != 8192):
            raise AssertionError('public restore quality')
        self.free()
        save(folder/'cache.json', dict(passed=True, seed=seed, resume=resume, child=child))

    def stats(self, step, folder):
        fixture = self.plan['quality']['1024']
        def one(i):
            label = step['name']+f'-{i}'
            row = decoded(self.http(folder, label, self.body(label, fixture['prompt'],
                                                          bool(i%2))))
            if row['text'].strip() != fixture['expected']:
                raise AssertionError('offload statistics quality')
            return row
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(one, range(4)))
        self.free()
        raw = (folder/'server.log').read_text()
        counts = fields(raw, '[q4t][residency] id=')
        timing = fields(raw, '[q4t][residency][timing] ')
        if (len(counts) != 4 or len(timing) != (4 if step['timing'] else 0)
                or sorted(int(r['interval']) for r in counts) != [1, 2, 3, 4]
                or any(r['scope'] != 'global_interval' for r in counts+timing)):
            raise AssertionError('concurrent interval/timing records')
        for row in counts+timing:
            for key, value in row.items():
                if key not in ['id', 'scope', 'finish'] and float(value) >= 1e12:
                    raise AssertionError('counter underflow/wrap')
        save(folder/'stats.json', dict(rows=rows, counts=counts, timing=timing))

    def run(self):
        for step in self.plan['steps']:
            before = self.http_count
            with self.service(step) as (folder, server):
                if step['kind'] == 'quality':
                    self.quality(step, folder)
                elif step['kind'] == 'fault':
                    self.fault(step, folder, server)
                elif step['kind'] == 'cache':
                    self.cache(step, folder)
                elif step['kind'] == 'stats':
                    self.stats(step, folder)
            if self.http_count-before != step['requests']:
                raise AssertionError('frozen request allocation changed')
            self.completed.append(step['name'])
            save(self.out/'progress.json', dict(completed=self.completed,
                 http=self.http_count, starts=self.starts))
            print(step['name']+': PASS', flush=True)
        final = nvml_process_query.query(timeout=5)
        save(self.out/'final-gpu.json', final)
        if final['status'] != 'OK' or final['pids']:
            raise RuntimeError('GPU contexts remain after owned service exits')
        save(self.out/'result.json', dict(passed=True, http=self.http_count,
            starts=self.starts, performance_acceptance=False,
            completed=self.completed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    lease = json.loads(gpu_exclusive.common_lock().read_text())
    parent_argv = Path(f'/proc/{os.getppid()}/cmdline').read_bytes().split(b'\0')
    if (lease.get('state') != 'held' or lease.get('pid') != os.getppid() or
            not any(arg.endswith(b'/gpu_exclusive.py') for arg in parent_argv)):
        raise RuntimeError('full driver must run under gpu_exclusive.py')
    for path, digest in plan['files'].items():
        if sha(path) != digest:
            raise RuntimeError('frozen identity changed: '+path)
    runner = Runner(plan)
    try:
        runner.run()
    except BaseException as error:
        save(runner.out/'failure.json', dict(error=repr(error),
            traceback=traceback.format_exc(), http=runner.http_count,
            starts=runner.starts, completed=runner.completed))
        raise


if __name__ == '__main__':
    main()
