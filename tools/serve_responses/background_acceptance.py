"""Native background Responses: 2 starts/8 creates/900 seconds, no perf claim."""
import argparse
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses.test_contract import errors as wire_errors
from tools.serve_service.package import read, require, save, sha

base = foreground.base
LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)


def requests():
    old = foreground.requests()
    long = dict(model=foreground.MODEL, background=True, temperature=0,
        reasoning=dict(effort='none'), max_output_tokens=1536,
        input='Return exactly 512 true values in the values array. Continue until all 512 are present.',
        text=dict(format=dict(type='json_schema', name='long', strict=True,
            schema=base.closed(dict(values=dict(type='array', minItems=512,
                maxItems=512, items=dict(type='boolean', const=True)))))))
    return dict(plain=dict(old['plain'], background=True),
        tool=dict(old['tool'], background=True),
        follow=dict(old['follow'], background=True),
        cancel_running=dict(long, stream=True, stream_options=dict(include_obfuscation=False)),
        cancel_queued=dict(old['plain'], background=True, store=True),
        crash_running=long, crash_queued=dict(old['plain'], background=True, store=True),
        chat=old['chat'])


def prepare(args, *, rows=None, limits=LIMITS, scope=__doc__):
    base.prepare(args, rows=requests() if rows is None else rows,
                 limits=limits, scope=scope)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_responses',
        'docs/serve-application-v1.schema.json', 'docs/serve-config-v1.schema.json'],
        cwd=ROOT, text=True).splitlines()
    for name in names:
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=limits['starts'], chats=limits['chats'])
    plan['mutations'] = ['abort only own SSE subscriber',
        'API cancel own queued and running response',
        'SIGKILL exact own first model process then bounded cleanup and explicit second start']
    plan['derived_requests'] = ['tool result is actual read of docs/MODEL.md lines1-3',
        'follow previous_response_id and call_id come from the actual tool response',
        'retrieve/cancel/replay/delete use actual generated IDs, no automatic retries',
        'queued crash work resumes its original ID and deadline; no extra HTTP create']
    save(path, plan)


def check_response(value, status=None):
    require(not wire_errors(value, 'response'), 'response wire schema: '+str(
        [(list(e.path), e.message[:100]) for e in wire_errors(value, 'response')]))
    if status:
        require(value['status'] == status, 'unexpected response status')
    if value['status'] in ('completed', 'incomplete'):
        foreground.check_usage(value)


def check_cancel(first, repeated, projected):
    """Compare like-for-like cancel replies, then check the GET projection."""
    for value in (first, repeated, projected):
        check_response(value, 'cancelled')
    require(first == repeated, 'cancel changed already-terminal resource')
    expected = copy.deepcopy(first)
    for item in expected['output']:
        for part in item.get('content', []):
            part.pop('logprobs', None)
    require(expected == projected, 'cancel and GET differ beyond logprobs projection')


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_BACKGROUND_BUSINESS'

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        generation = self.counts['starts'] + 1
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '1536', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--object-store-dir', str(self.out/'objects')]
        save(self.out/f'model-{generation}-command.json', args)
        self.budget('starts')
        with (self.out/f'model-{generation}.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/f'model-{generation}-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('model readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label=f'initial-{generation}-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/f'{name}-{generation}.json', json.loads(raw))
        caps = read(self.out/f'capabilities-{generation}.json')
        require(caps['responses']['background']['cancel'] and
                caps['responses']['profile'] == 'q4t-responses-v1', 'missing native capability')
        self.client.protocol_sha256 = caps['protocol_source_sha256']

    def sdk(self, label, action, request=None, *, response_id=None, query=None,
            stream_mode=None, expected_status=None, operations=False):
        self.budget('chats' if action == 'create' else 'controls')
        port = self.plan['operations_port'] if operations else self.plan['port']
        command = dict(action=action, request=request, id=response_id, query=query,
            stream_mode=stream_mode, base_url=f'http://127.0.0.1:{port}/v1',
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_background_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True, text=True,
            env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failure')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'request replay')
        if expected_status:
            require(not result['ok'] and result['error']['status'] == expected_status,
                    'expected error missing')
            return None
        require(run.returncode == 0 and result['ok'], 'SDK: '+str(result.get('error')))
        for event in result.get('chunks') or []:
            require(not wire_errors(event, 'response_event'), 'event wire schema: '+event['type'])
        value = result['result']
        if value.get('object') == 'response':
            check_response(value)
        self.same_model()
        return value

    def wait_response(self, label, identity, predicate, seconds=60):
        samples = []
        end = time.monotonic()+seconds
        try:
            while time.monotonic() < end:
                self.same_model()
                status, raw = self.http('/v1/responses/'+identity, timeout=5)
                require(status == 200, 'background query failed')
                value = json.loads(raw)
                check_response(value)
                samples.append(value)
                if predicate(value):
                    return value
                require(value['status'] not in ('failed', 'cancelled', 'incomplete', 'completed'),
                        'unexpected early terminal: '+value['status'])
                time.sleep(.05)
            raise TimeoutError('background state: '+label)
        finally:
            save(self.out/(label+'.poll.json'), samples)

    def completed(self, label, value):
        return self.wait_response(label, value['id'], lambda v: v['status'] == 'completed')

    def cleanup(self):
        super().cleanup()
        for name, digest in self.plan['protected'].items():
            require(sha(name) == digest, 'default changed during acceptance')

    def business(self):
        rows = self.plan['requests']
        plain = self.completed('plain-completed', self.sdk('plain', 'create', rows['plain']))
        require(foreground.text(plain).strip() == '42' and not plain['store'], 'temporary answer')
        self.drained('plain')
        self.sdk('temporary-delete', 'delete', response_id=plain['id'])
        self.sdk('temporary-gone', 'retrieve', response_id=plain['id'], expected_status=404)
        tool = self.sdk('tool', 'create', rows['tool'])
        check_response(tool, 'completed')
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'tool did not finish')
        args = json.loads(calls[0]['arguments'])
        require(args == dict(path=foreground.DOCUMENT, start_line=1, line_count=3), 'tool arguments')
        result = base.ProjectRecords(ROOT).read(args)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT], 'document changed')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        final = self.completed('follow-completed', self.sdk('follow', 'create', follow))
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'actual tool/JSON closure')
        self.drained('tool-closure')

        stream = self.sdk('cancel-running', 'create', rows['cancel_running'], stream_mode='first_text_delta')
        queued = self.sdk('cancel-queued', 'create', rows['cancel_queued'])
        active = self.sdk('after-disconnect', 'retrieve', response_id=stream['id'], operations=True)
        check_response(active, 'in_progress')
        self.wait_response('queued-before-cancel', queued['id'], lambda v: v['status'] == 'queued')
        cancelled = self.sdk('queued-cancel', 'cancel', response_id=queued['id'], operations=True)
        check_response(cancelled, 'cancelled')
        first = self.sdk('running-cancel', 'cancel', response_id=stream['id'], operations=True)
        cancelled = self.wait_response('running-cancelled', stream['id'], lambda v: v['status'] == 'cancelled')
        require(cancelled['output'], 'cancel discarded partial output')
        again = self.sdk('running-cancel-idempotent', 'cancel', response_id=stream['id'], operations=True)
        check_cancel(first, again, cancelled)
        self.sdk('cancelled-replay', 'retrieve', response_id=stream['id'],
            query=dict(stream=True, include_obfuscation=False), stream_mode='cancelled_replay', operations=True)
        self.drained('cancelled')

        self.crash_recovery()

    def crash_recovery(self):
        rows = self.plan['requests']
        crashed = self.sdk('crash-running', 'create', rows['crash_running'])
        partial = self.wait_response('crash-partial', crashed['id'],
            lambda v: v['status'] == 'in_progress' and bool(foreground.text(v)))
        resumed = self.sdk('crash-queued', 'create', rows['crash_queued'])
        self.wait_response('queued-before-crash', resumed['id'], lambda v: v['status'] == 'queued')
        self.same_model()
        save(self.out/'crash-injection.json', dict(pid=self.model.pid, identity=self.model_identity,
            partial=partial, queued=resumed, replay_allowed=False))
        base.lifecycle.guard.signal_identity(self.model.pid, self.model_identity, signal.SIGKILL)
        require(self.model.wait(10) == -signal.SIGKILL, 'owned model crash not observed')
        self.cleanup()
        save(self.out/'after-crash-cleanup.json', read(self.out/'cleanup.json'))
        self.model = None
        self.start()
        failed = self.wait_response('interrupted-failed', crashed['id'], lambda v: v['status'] == 'failed')
        require(failed['error']['code'] == 'background_execution_interrupted' and
                foreground.text(failed).startswith(foreground.text(partial)), 'interrupted state lost or replayed')
        restored = self.completed('queued-resumed', resumed)
        require(restored['id'] == resumed['id'] and foreground.text(restored).strip() == '42', 'queued recovery')
        self.drained('restored')
        chat = self.sdk_request('same-pid-chat', rows['chat'])
        require(chat.finish_reason == 'stop' and chat.message['content'].strip() == '42', 'Chat after recovery')
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 2 and metrics['q4t_requests_success_total'] == 2 and
                metrics['q4t_requests_error_total'] == 0, 'extra/replayed model execution')
        status, raw = self.http('/v1/config', label='final-config')
        config = json.loads(raw)
        jobs = config['responses_background']
        require(status == 200 and jobs['recovered'] == 1 and jobs['interrupted'] == 1 and
                jobs['owned'] == 0 and jobs['recovery_pending'] == 0, 'background recovery ownership')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18164)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_signal, _frame):
            raise TimeoutError('background acceptance phase deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        try:
            # Reserve the final 40 seconds for the existing bounded own-tree
            # cleanup even if an SDK/socket call spans the phase deadline.
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
