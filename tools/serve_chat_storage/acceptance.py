"""Stored Chat real business: 1 start/7 Chat requests/600 seconds; no perf claim."""
import argparse
import base64
import copy
import http.client
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses.reconciliation_acceptance import own_object_worker
from tools.serve_responses.test_contract import errors as wire_errors
from tools.serve_service.package import read, require, save, sha

base = background.base
LIMITS = dict(starts=1, chats=7, controls=1000, seconds=600)
MODEL = background.foreground.MODEL
DOCUMENT = background.foreground.DOCUMENT


def requests():
    plain = dict(model=MODEL, temperature=0, enable_thinking=False,
        messages=[dict(role='user', content='Compute 17 + 25. Reply only with the decimal number.')],
        max_completion_tokens=64)
    tool = dict(plain, store=True, stream=True, stream_options=dict(include_usage=True),
        max_completion_tokens=192, metadata=dict(stage='tool'),
        messages=[dict(role='user', content='Read the first 3 lines of docs/MODEL.md with '
            'read_project_record. Use the actual tool result to return the title and exact citation.')],
        tools=[dict(type='function', function=dict(name='read_project_record',
            description='Read bounded real project document lines and citation.', strict=True,
            parameters=base.closed(dict(path=dict(type='string', const=DOCUMENT),
                start_line=dict(type='integer', const=1), line_count=dict(type='integer', const=3)))))],
        tool_choice='required', parallel_tool_calls=False)
    follow = dict(plain, store=True, max_completion_tokens=384, metadata=dict(stage='follow'),
        messages=[], tools=tool['tools'], tool_choice='none',
        response_format=dict(type='json_schema', json_schema=dict(name='document', strict=True,
            schema=base.closed(dict(title=dict(type='string', minLength=1, maxLength=256),
                citation=dict(type='string', pattern=r'^\[docs/MODEL\.md:L1-L3\]$'))))))
    fault = dict(plain, store=True, stream=True, stream_options=dict(include_usage=True),
        max_completion_tokens=64, metadata=dict(stage='fault'),
        messages=[dict(role='user', content='Write a long comma-separated list of integers '
                       'starting at 1 and ending at 1000. Continue until all are written.')])
    return dict(unretained=dict(plain, store=None, metadata=dict(stage='response-only')),
        json=dict(plain, store=True, n=2, metadata=dict(stage='json')),
        stream=dict(plain, store=True, stream=True, n=2, logprobs=True, top_logprobs=2,
            stream_options=dict(include_usage=True), metadata=dict(stage='stream')),
        tool=tool, follow=follow, fault=fault, chat=plain)


def shape(value, name):
    failures = wire_errors(value, name)
    require(not failures, str([(list(e.path), e.message[:160]) for e in failures]))


def check_stream(chunks, stored):
    """Compare actual wire deltas/probabilities/usage with the retained result."""
    require(chunks and stored['choices'], 'empty stream or retained result')
    observed = {}
    usage = []
    for chunk in chunks:
        shape(chunk, 'chat_chunk')
        require(chunk['id'] == stored['id'] and chunk['created'] == stored['created'] and
                chunk['model'] == stored['model'], 'stream identity differs from storage')
        if chunk.get('usage') is not None:
            usage.append(chunk['usage'])
        for choice in chunk['choices']:
            row = observed.setdefault(choice['index'], dict(text='', reasoning='', tools={}, logs=[], finish=[]))
            delta = choice['delta']
            row['text'] += delta.get('content') or ''
            row['reasoning'] += delta.get('reasoning_content') or ''
            require(not row['finish'], 'choice delta after finish')
            for call in delta.get('tool_calls') or []:
                tool = row['tools'].setdefault(call['index'], dict(id='', type='', function=dict(name='', arguments='')))
                for field in ('id', 'type'):
                    if field in call:
                        require(not tool[field] or tool[field] == call[field], 'tool identity changed')
                        tool[field] = call[field]
                for field in ('name', 'arguments'):
                    tool['function'][field] += call.get('function', {}).get(field, '')
            logs = choice.get('logprobs')
            if logs:
                row['logs'] += logs.get('content') or []
            if choice.get('finish_reason'):
                row['finish'].append(choice['finish_reason'])
    require(set(observed) == {c['index'] for c in stored['choices']}, 'choice lost from storage/stream')
    for choice in stored['choices']:
        row = observed[choice['index']]
        message = choice['message']
        require(row['text'] == (message.get('content') or '') and
                row['reasoning'] == (message.get('reasoning_content') or ''), 'stored text/reasoning differs')
        require([row['tools'][i] for i in sorted(row['tools'])] == (message.get('tool_calls') or []),
                'stored tool history differs')
        require(row['finish'] == [choice['finish_reason']], 'missing/repeated choice terminal')
        require(row['logs'] == ((choice.get('logprobs') or {}).get('content') or []),
                'stored token probabilities/bytes differ')
    require(usage == [stored['usage']], 'stream usage differs or repeats')


def check_fault(events, raw):
    failures = [e['error'] for e in events if 'error' in e]
    require(len(failures) == 1 and failures[0]['code'] == 'object_commit_unknown',
            'missing honest storage receipt error')
    require(events[-1].get('error') == failures[0], 'data after storage failure')
    require(raw.count(b'data: [DONE]') == 1 and
            not any(e.get('usage') for e in events) and
            not any(c.get('finish_reason') for e in events for c in e.get('choices', [])),
            'failed storage published successful finish/usage')
    require(any(c.get('delta', {}).get('content') for e in events
                for c in e.get('choices', [])), 'fault had no real generated prefix')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_chat_storage',
        'docs/serve-transport-v1.schema.json'], cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT/name)
    plan['mutations'] = ['SIGSTOP one verified own object worker after the first actual Chat text delta',
        'native storage timeout and worker recovery; never signal/restart the model']
    plan['derived_requests'] = ['GET/update/list/messages/delete use actual completion IDs',
        'real project tool result feeds stored Chat history and constrained JSON',
        'stream choices, probabilities, original bytes and usage compared with retained resource',
        'failed final save has no successful terminal; original ID queried without replay',
        'same-model Chat, earlier object retrieval, all owned cleanup and defaults verified']
    plan['maximum_generated_choices'] = 9
    save(path, plan)


class Stage(background.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_STORED_CHAT_BUSINESS'

    def sdk_chat(self, label, action, request=None, *, identity=None, query=None, expected_status=None):
        self.budget('chats' if action == 'create' else 'controls')
        command = dict(action=action, request=request, id=identity, query=query,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_chat_storage/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True, text=True,
            env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failed')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'request replay')
        if expected_status is not None:
            require(not result['ok'] and result['error']['status'] == expected_status, 'wrong SDK error')
            return None
        require(run.returncode == 0 and result['ok'], 'SDK failed: '+str(result.get('error')))
        self.same_model()
        if action == 'create' and request.get('stream'):
            require(result['chunks'], 'empty SDK stream')
            saved = self.sdk_chat(label+'-stored', 'retrieve', identity=result['chunks'][0]['id'])
            check_stream(result['chunks'], saved)
            return saved
        value = result['result']
        name = ('stored_chat_messages' if action == 'messages' else
                'stored_chat_list' if action == 'list' else
                'stored_chat_deleted' if action == 'delete' else 'stored_chat')
        shape(value, name)
        return value

    def fault(self):
        self.budget('chats')
        body = self.plan['requests']['fault']
        save(self.out/'fault.request.json', body)
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=15)
        events, chunks, stopped, identity = [], [], None, None
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(body).encode(),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            save(self.out/'fault.http.json', dict(status=response.status, headers=dict(response.getheaders())))
            require(response.status == 200, 'fault did not enter real stream')
            with (self.out/'fault.wire').open('xb') as wire:
                while True:
                    line = response.readline(1024*1024+1)
                    if not line:
                        break
                    require(len(line) <= 1024*1024 and sum(map(len, chunks))+len(line) <= 4*1024*1024,
                            'fault capture bound')
                    chunks.append(line)
                    wire.write(line)
                    if not line.startswith(b'data: {'):
                        continue
                    event = json.loads(line[6:])
                    events.append(event)
                    if 'choices' in event:
                        shape(event, 'chat_chunk')
                        identity = event['id']
                    if stopped is None and any(c.get('delta', {}).get('content')
                                               for c in event.get('choices', [])):
                        self.same_model()
                        pid, record = own_object_worker(base.lifecycle.guard.processes(), self.model.pid)
                        require(record['executable'] == self.plan['binary'], 'worker executable changed')
                        self.history[pid] = record
                        save(self.out/'fault-injection.json', dict(pid=pid, identity=record,
                            model_pid=self.model.pid, completion_id=identity, mutation='SIGSTOP'))
                        base.lifecycle.guard.signal_identity(pid, record, signal.SIGSTOP)
                        stopped = (pid, record)
        finally:
            connection.close()
            save(self.out/'fault.events.json', events)
        require(stopped is not None, 'fault was not injected')
        check_fault(events, b''.join(chunks))
        self.drained('fault')
        # The worker was stopped before its final Put, so this specific fault
        # did not commit. Unknown receipts in general must be queried by ID.
        self.sdk_chat('fault-not-committed', 'retrieve', identity=identity, expected_status=404)
        require(self.http('/readyz')[0] == 200, 'storage fault changed core readiness')
        require(base.lifecycle.guard.processes().get(stopped[0], {}).get('start_ticks') !=
                stopped[1]['start_ticks'], 'stopped worker not reaped')

    def business(self):
        rows = self.plan['requests']
        caps = read(self.out/'capabilities-1.json')
        require(caps['stored_chat']['final_success'] == 'after_durable_receipt', 'missing capability')
        plain = self.sdk_chat('unretained', 'create', rows['unretained'])
        require(plain['choices'][0]['message']['content'].strip() == '42' and
                plain['metadata'] == rows['unretained']['metadata'], 'unretained answer/metadata')
        self.sdk_chat('unretained-gone', 'retrieve', identity=plain['id'], expected_status=404)
        stored = self.sdk_chat('json', 'create', rows['json'])
        require(len(stored['choices']) == 2 and all(c['finish_reason'] == 'stop' and
            c['message']['content'].strip() == '42' for c in stored['choices']), 'JSON choices/answer')
        require(self.sdk_chat('json-retrieve', 'retrieve', identity=stored['id']) == stored,
                'JSON differs after storage')
        stream = self.sdk_chat('stream', 'create', rows['stream'])
        require(len(stream['choices']) == 2 and all(c['message']['content'].strip() == '42'
            for c in stream['choices']), 'stream choices/answer')
        for choice in stream['choices']:
            logs = choice['logprobs']['content']
            require(logs and all(len(row['top_logprobs']) == 2 and math.isfinite(row['logprob']) and
                row['logprob'] <= 0 and isinstance(row['bytes'], list) for row in logs),
                'actual stored token probabilities/bytes missing')
        page = self.sdk_chat('filtered-list', 'list', query=dict(metadata=dict(stage='stream'), model=MODEL, limit=1))
        require(page['data'] == [stream] and not page['has_more'], 'filtered native list')
        tool = self.sdk_chat('tool', 'create', rows['tool'])
        calls = tool['choices'][0]['message'].get('tool_calls') or []
        require(tool['choices'][0]['finish_reason'] == 'tool_calls' and len(calls) == 1 and
                calls[0]['function']['name'] == 'read_project_record', 'real tool missing')
        args = json.loads(calls[0]['function']['arguments'])
        require(args == dict(path=DOCUMENT, start_line=1, line_count=3), 'tool arguments')
        result = base.ProjectRecords(ROOT).read(args)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][DOCUMENT], 'document source drift')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow['messages'] = rows['tool']['messages'] + [tool['choices'][0]['message'],
            dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(result, ensure_ascii=False)),
            dict(role='developer', content='Return the actual first-line document title and exact '
                 'source citation from the tool result as JSON. Do not call another tool.')]
        final = self.sdk_chat('follow', 'create', follow)
        answer = json.loads(final['choices'][0]['message']['content'])
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'real project tool JSON closure')
        history = self.sdk_chat('stored-history', 'messages', identity=final['id'], query=dict(limit=100))
        for actual, original in zip(history['data'], follow['messages'], strict=True):
            require(all(actual.get(k) == v for k, v in original.items()), 'original input history lost')
        updated = self.sdk_chat('metadata-update', 'update', dict(metadata=None), identity=stored['id'])
        expected = dict(stored, metadata=None)
        require(updated == expected, 'metadata update modified output')
        self.fault()
        recovered = self.sdk_chat('same-pid-chat', 'create', rows['chat'])
        require(recovered['choices'][0]['message']['content'].strip() == '42', 'same-PID core continuation')
        require(self.sdk_chat('retained-after-worker-recovery', 'retrieve', identity=stored['id']) == expected,
                'earlier persisted output lost during worker recovery')
        self.sdk_chat('delete', 'delete', identity=stored['id'])
        self.sdk_chat('deleted-gone', 'retrieve', identity=stored['id'], expected_status=404)
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 7 and metrics['q4t_requests_success_total'] == 6 and
                metrics['q4t_requests_error_total'] == 1, 'Chat generation accounting')
        status, raw = self.http('/v1/config', label='final-config')
        require(status == 200, 'final config missing')
        save(self.out/'final-config.json', json.loads(raw))


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
            raise TimeoutError('stored Chat business deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
