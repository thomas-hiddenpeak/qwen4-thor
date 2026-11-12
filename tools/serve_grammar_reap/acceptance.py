"""R02: six bounded real requests, one owned grammar fault, no model restart."""
import argparse
import copy
from dataclasses import asdict
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope'), str(ROOT/'tools/examples')]
import gpu_exclusive as guard
from chat_client import read_json, read_sse
from project_records import ProjectRecords
from tools.release.release import child_subreaper
from tools.serve_recovery.child import verify_lease
from tools.serve_service.package import require, sha, save

LIMITS = dict(starts=1, chats=6, controls=2000, seconds=600)
DOCUMENT = 'docs/STATUS.md'


def requests():
    tool = dict(type='function', function=dict(name='read_project_header',
        description='Read the first three lines of a real project document.',
        strict=True, parameters=dict(type='object', properties=dict(path=dict(
            type='string', enum=[DOCUMENT])), required=['path'],
            additionalProperties=False)))
    base = dict(model='qwen3.8-flash-next', enable_thinking=False,
                max_tokens=256, temperature=0, stream=False)
    initial = dict(base, messages=[dict(role='user', content=
        '调用read_project_header读取docs/STATUS.md。')], tools=[tool],
        tool_choice='required', parallel_tool_calls=False)
    plain = dict(base, max_tokens=32, messages=[dict(role='user',
        content='只回复数字42，不要解释。')])
    schema = dict(type='object', properties=dict(ok=dict(type='boolean',
        enum=[True])), required=['ok'], additionalProperties=False)
    structured = dict(base, messages=[dict(role='user',
        content='返回包含ok为true的JSON。')], response_format=dict(type='json_schema',
        json_schema=dict(name='ready', strict=True, schema=schema)),
        stream=True, stream_options=dict(include_usage=True))
    fault = copy.deepcopy(structured)
    fault['response_format']['json_schema']['schema'] = dict(type='object',
        properties=dict(data=dict(type='array', minItems=64, maxItems=64,
            items=dict(type='boolean', enum=[True]))), required=['data'],
        additionalProperties=False)
    return dict(tool=initial, plain=plain, structured=structured, fault=fault)


def fault_terminal(status, raw):
    require(status == 200, 'fault must exercise committed SSE headers')
    require(raw.count(b'data: [DONE]') == 1 and
            raw.rstrip().endswith(b'data: [DONE]'), 'fault terminal truncated')
    values = [json.loads(line[6:]) for line in raw.splitlines()
              if line.startswith(b'data: {')]
    errors = [value for value in values if 'error' in value]
    require(len(errors) == 1 and values[-1] == errors[0],
            'one final error envelope required')
    code = errors[0]['error']['code']
    require(code in ('constraint_compile_timeout', 'structured_output_invalid'),
            'unexpected stopped-worker failure: '+str(code))
    require(all(value.get('usage') is None and all(
        choice.get('finish_reason') is None for choice in value.get('choices', []))
        for value in values), 'failed request advertised successful terminal')
    return dict(code=code, complete_error=True, replay=False)


def parse_metrics(raw):
    return {key: float(value) for line in raw.decode().splitlines()
            if line and not line.startswith('#')
            for key, value in [line.rsplit(' ', 1)]}


def prepare(args):
    out = args.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = args.binary.resolve(strict=True)
    require(not subprocess.check_output(['git', 'status', '--porcelain'],
                                        cwd=ROOT), 'commit reviewed source first')
    sources = subprocess.check_output(['git', 'ls-files', 'src', 'include',
        'cmake', 'CMakeLists.txt', 'tools/serve_grammar_reap',
        'tools/evalscope/gpu_exclusive.py', 'tools/evalscope/nvml_process_query.py',
        'tools/evalscope/bounded_log.py', 'tools/examples/chat_client.py',
        'tools/examples/deadline_http.py', 'tools/examples/project_records.py',
        'tools/serve_recovery/child.py', 'tools/serve_recovery/journal.py',
        'tools/serve_recovery/ledger.py', 'tools/serve_service/package.py',
        'tools/release/release.py', DOCUMENT], cwd=ROOT, text=True).splitlines()
    protected = [ROOT/'build/q4t', ROOT/'build/q4t.release.json',
                 ROOT/'build/service.release.json', ROOT/'build/service/manifest.json']
    model = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    plan = dict(version=1, limits=LIMITS, output=str(out), binary=str(binary),
        binary_sha256=sha(binary), model=str(model), port=args.port,
        operations_port=args.port+1, requests=requests(),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        sources={name: sha(ROOT/name) for name in sources},
        protected={str(path): sha(path) for path in protected},
        mutation='SIGSTOP one exact child worker; engine timeout kills/reaps it',
        non_goals=['kernel D-state reproduction', 'performance', 'full model quality'])
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), source_files=len(sources))))


class Stage:
    def __init__(self, plan):
        self.plan, self.out = plan, Path(plan['output'])
        self.started = time.monotonic()
        self.counts = dict(starts=0, chats=0, controls=0)
        self.model = None
        self.model_identity = None
        self.history = {}
        self.fault_thread = None
        self.fault_result = {}
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        verify_lease(self.lease, guard.common_lock())
        require(plan['limits'] == LIMITS and plan['requests'] == requests(),
                'frozen budget or requests changed')
        for name, digest in plan['sources'].items():
            require(sha(ROOT/name) == digest, 'source changed: '+name)
        for name, digest in plan['protected'].items():
            require(sha(name) == digest, 'default changed: '+name)
        require(sha(plan['binary']) == plan['binary_sha256'], 'binary changed')

    def budget(self, kind):
        require(time.monotonic()-self.started < LIMITS['seconds']-40,
                'business deadline exhausted')
        require(self.counts[kind] < LIMITS[kind], 'attempt limit: '+kind)
        self.counts[kind] += 1
        save(self.out/'counts.json', self.counts)

    def http(self, path, body=None, *, label=None, timeout=30):
        # Chat attempts are reserved on the main thread before submission.
        if body is None:
            self.budget('controls')
        conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        start = time.monotonic()
        try:
            conn.request('GET' if body is None else 'POST', path,
                body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                headers={} if body is None else {'Content-Type': 'application/json'})
            response = conn.getresponse()
            raw = response.read(4*1024*1024+1)
            require(len(raw) <= 4*1024*1024, 'response size limit')
            metadata = dict(status=response.status, headers=dict(response.getheaders()),
                            seconds=time.monotonic()-start)
            if label:
                (self.out/(label+'.wire')).write_bytes(raw)
                save(self.out/(label+'.http.json'), metadata)
            return response.status, raw
        finally:
            conn.close()

    def submit(self, label, body):
        self.budget('chats')
        save(self.out/(label+'.request.json'), body)
        return self.http('/v1/chat/completions', body, label=label)

    def success(self, label, body):
        status, raw = self.submit(label, body)
        require(status == 200, label+' HTTP failure')
        value = (read_sse if body.get('stream') else read_json)(io.BytesIO(raw))
        save(self.out/(label+'.result.json'), asdict(value))
        return value

    def same_model(self):
        current = guard.processes().get(self.model.pid, {})
        require(self.model.poll() is None and
                current.get('start_ticks') == self.model_identity['start_ticks'],
                'core model changed during grammar fault')

    def drained(self, label):
        end = time.monotonic()+5
        samples = []
        while time.monotonic() < end:
            self.same_model()
            status, raw = self.http('/metrics')
            require(status == 200, 'metrics unavailable')
            values = parse_metrics(raw)
            samples.append(values)
            if (values['q4t_structured_workers_started_total'] ==
                values['q4t_structured_workers_reaped_total'] and
                all(values[name] == 0 for name in ('q4t_structured_cleanup_pending',
                    'q4t_structured_workers_active', 'q4t_structured_active',
                    'q4t_active_chats', 'q4t_structured_preparing'))):
                save(self.out/(label+'.metrics.json'), samples)
                return values
            time.sleep(.025)
        save(self.out/(label+'.metrics.json'), samples)
        raise RuntimeError('owned worker/request did not drain')

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '256', '--max-inflight-requests', '8',
            '--max-queued-requests', '2']
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
        while time.monotonic() < end:
            self.same_model()
            try:
                status, _ = self.http('/readyz', timeout=1)
                if status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('model readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))

    def tool_loop(self):
        body = self.plan['requests']['tool']
        answer = self.success('01-tool', body)
        calls = answer.message.get('tool_calls', [])
        require(answer.finish_reason == 'tool_calls' and len(calls) == 1,
                'required tool call missing')
        call = calls[0]
        require(call['function']['name'] == 'read_project_header' and
                json.loads(call['function']['arguments']) == {'path': DOCUMENT},
                'tool arguments differ')
        result = ProjectRecords(ROOT).read(dict(path=DOCUMENT, start_line=1, line_count=3))
        save(self.out/'tool-result.json', result)
        follow = copy.deepcopy(body)
        follow['tool_choice'] = 'none'
        follow['messages'] += [answer.message,
            dict(role='tool', tool_call_id=call['id'], content=json.dumps(result, ensure_ascii=False)),
            dict(role='user', content='只返回文档第一行的标题，并原样附上工具给出的引用，不再调用工具。')]
        self.drained('01-drain')
        final = self.success('02-tool-result', follow)
        require(final.finish_reason == 'stop' and not final.message.get('tool_calls') and
                '当前状态' in final.message['content'] and
                result['source']['citation'] in final.message['content'],
                'real document title/citation missing')
        self.drained('02-drain')

    def fault(self):
        body = self.plan['requests']['fault']
        self.budget('chats')
        save(self.out/'03-fault.request.json', body)
        def invoke():
            try:
                self.fault_result['response'] = self.http('/v1/chat/completions', body,
                                                         label='03-fault')
            except BaseException as error:
                self.fault_result['exception'] = repr(error)
        self.fault_thread = threading.Thread(target=invoke, daemon=True)
        self.fault_thread.start()
        end = time.monotonic()+10
        while time.monotonic() < end and self.fault_thread.is_alive():
            self.same_model()
            records = guard.processes()
            workers = {pid: row for pid, row in records.items()
                       if row.get('ppid') == self.model.pid and
                       '--q4t-grammar-worker' in row.get('argv', [])}
            if workers:
                require(len(workers) == 1, 'multiple grammar children')
                pid, row = next(iter(workers.items()))
                mapped = (Path('/proc')/str(pid)/'exe').stat()
                expected = Path(self.plan['binary']).stat()
                require((mapped.st_dev, mapped.st_ino) ==
                        (expected.st_dev, expected.st_ino),
                        'worker executable differs')
                self.history[pid] = row
                save(self.out/'fault-identity.json', dict(pid=pid, **row))
                guard.signal_identity(pid, row, signal.SIGSTOP)
                break
            time.sleep(.005)
        else:
            raise RuntimeError('owned live grammar worker was not observed')
        plain = self.success('04-plain-json-during-fault', self.plan['requests']['plain'])
        require(plain.message['content'].strip() == '42', 'plain JSON answer differs')
        self.fault_thread.join(15)
        require(not self.fault_thread.is_alive() and
                'response' in self.fault_result, 'fault request did not terminate')
        save(self.out/'fault-verdict.json', fault_terminal(*self.fault_result['response']))
        self.drained('04-drain')
        stream = dict(self.plan['requests']['plain'], stream=True,
                      stream_options=dict(include_usage=True))
        plain = self.success('05-plain-sse-after-fault', stream)
        require(plain.message['content'].strip() == '42', 'plain SSE answer differs')
        recovered = self.success('06-structured-recovered',
                                 self.plan['requests']['structured'])
        require(json.loads(recovered.message['content']) == {'ok': True},
                'structured output did not recover')
        final = self.drained('06-drain')
        require(final['q4t_requests_total'] == 6 and
                final['q4t_requests_success_total'] == 5 and
                final['q4t_requests_error_total'] == 1,
                'business accounting differs or request replayed')
        status, raw = self.http('/v1/capabilities', label='final-capabilities')
        caps = json.loads(raw)
        require(status == 200 and caps['structured_output']['available'] and
                not caps['structured_output']['cleanup_pending'], 'capability did not recover')
        self.same_model()

    def cleanup(self):
        if self.model is not None:
            if self.model.poll() is None:
                guard.signal_identity(self.model.pid, self.model_identity, signal.SIGTERM)
                try:
                    self.model.wait(20)
                except subprocess.TimeoutExpired:
                    guard.signal_identity(self.model.pid, self.model_identity, signal.SIGKILL)
                    self.model.wait(5)
            if self.fault_thread:
                self.fault_thread.join(5)
        end = time.monotonic()+5
        while time.monotonic() < end:
            records = guard.processes()
            children = {pid: row for pid, row in records.items()
                        if row.get('ppid') == os.getpid()}
            if not children:
                break
            for pid, row in children.items():
                self.history[pid] = row
                guard.signal_identity(pid, row, signal.SIGKILL)
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            time.sleep(.025)
        records = guard.processes()
        remaining = [pid for pid, row in self.history.items()
                     if records.get(pid, {}).get('start_ticks') == row['start_ticks']]
        require(not remaining, 'owned process cleanup unconfirmed')
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
        save(self.out/'cleanup.json', dict(owned=self.history, remaining=remaining,
            model_exit=None if self.model is None else self.model.returncode,
            ports_released=True, lease_continuously_held=True))

    def run(self):
        result = dict(status='FAILED')
        try:
            self.start()
            self.tool_loop()
            self.fault()
            result['status'] = 'PASS_GRAMMAR_BOUNDED_REAP_BUSINESS'
        except BaseException:
            result['error'] = traceback.format_exc()
            raise
        finally:
            try:
                self.cleanup()
            except BaseException:
                result['status'] = 'FAILED'
                result['cleanup_error'] = traceback.format_exc()
                raise
            finally:
                result.update(counts=self.counts, seconds=time.monotonic()-self.started,
                    binary_sha256=self.plan['binary_sha256'],
                    kernel_uninterruptible_fault='CPU simulation only; not manufactured on GPU')
                save(self.out/'result.json', result)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='mode', required=True)
    stage = sub.add_parser('prepare')
    stage.add_argument('--binary', type=Path, required=True)
    stage.add_argument('--output', type=Path, required=True)
    stage.add_argument('--port', type=int, default=18164)
    run = sub.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with child_subreaper():
            Stage(json.loads(args.plan.read_text())).run()


if __name__ == '__main__':
    main()
