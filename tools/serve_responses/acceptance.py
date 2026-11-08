"""A07 foreground Responses/state real business short acceptance, no perf claim."""
import argparse
import base64
import copy
import http.client
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MODEL = 'qwen3.8-flash-next'
DOCUMENT = 'docs/MODEL.md'


def requests():
    common = dict(model=MODEL, temperature=0, max_output_tokens=192,
                  reasoning=dict(effort='none'))
    plain = dict(common, input='Compute 17 + 25. Reply only with the decimal number.')
    parameters = base.closed(dict(path=dict(type='string', const=DOCUMENT),
        start_line=dict(type='integer', const=1),
        line_count=dict(type='integer', const=3)))
    tool = dict(common, input='Read the first 3 lines of docs/MODEL.md with '
        'read_project_record. Use the actual tool result to return the title '
        'and exact citation.', tools=[dict(type='function', name='read_project_record',
            description='Read bounded real project document lines and citation.',
            parameters=parameters, strict=True)], tool_choice='required',
        parallel_tool_calls=False, stream=True,
        stream_options=dict(include_obfuscation=False))
    schema = base.closed(dict(title=dict(type='string', minLength=1, maxLength=256),
        citation=dict(type='string', pattern=r'^\[docs/MODEL\.md:L1-L3\]$')))
    follow = dict(common, max_output_tokens=384, input=[],
        instructions='Return the actual first-line document title and its exact '
        'source citation from the tool result as JSON. Do not call another tool.',
        tools=tool['tools'], tool_choice='none',
        text=dict(format=dict(type='json_schema', name='document', strict=True,
                              schema=schema)))
    return dict(plain=dict(plain, store=False),
        probabilities=dict(plain, include=['message.output_text.logprobs'], top_logprobs=2),
        reasoning=dict(plain, reasoning=dict(effort='low'), max_output_tokens=256),
        tool=tool, follow=follow, truncated=dict(tool, max_output_tokens=1),
        chat=dict(model=MODEL, temperature=0, enable_thinking=False,
            messages=[dict(role='user', content=plain['input'])], max_completion_tokens=64))


def text(response):
    return ''.join(part['text'] for item in response['output']
        if item['type'] == 'message' for part in item['content']
        if part['type'] == 'output_text')


def check_usage(response):
    usage = response['usage']
    require(all(type(usage[k]) is int and usage[k] >= 0 for k in
                ('input_tokens', 'output_tokens', 'total_tokens')), 'usage integers')
    require(usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
            'usage sum')
    cached = usage['input_tokens_details']['cached_tokens']
    reasoning = usage['output_tokens_details']['reasoning_tokens']
    require(type(cached) is int and 0 <= cached <= usage['input_tokens'],
            'cache accounting')
    require(type(reasoning) is int and 0 <= reasoning <= usage['output_tokens'],
            'reasoning accounting')


def check_probabilities(response):
    logs = [row for item in response['output'] if item['type'] == 'message'
        for part in item['content'] if part['type'] == 'output_text'
        for row in part['logprobs']]
    require(bool(logs), 'missing probability output')
    for row in logs:
        require(len(row['top_logprobs']) == 2, 'missing top two candidates')
        for probability in [row, *row['top_logprobs']]:
            require(type(probability['logprob']) in (float, int) and
                    math.isfinite(probability['logprob']) and
                    probability['logprob'] <= 0, 'invalid actual log probability')
            require(isinstance(probability['bytes'], list) and
                    all(type(byte) is int and 0 <= byte <= 255
                        for byte in probability['bytes']), 'missing raw token bytes')
    return len(logs)


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_responses'],
                                         cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=1, chats=7)
    plan['derived_requests'] = [
        'follow input is actual function_call_output; previous_response_id is actual tool response ID',
        'read/retrieve/replay/input_items/delete reference actual generated IDs; no extra generation',
        'all state writes are isolated in this stage output directory; default package is protected']
    save(path, plan)


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_FOREGROUND_BUSINESS'

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '384', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--object-store-dir', str(self.out/'objects')]
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic() + 180
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
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        self.client.protocol_sha256 = read(self.out/'capabilities.json')['protocol_source_sha256']

    def sdk(self, label, action, request=None, *, response_id=None,
            query=None, expected_status=None):
        self.budget('chats' if action == 'create' else 'controls')
        command = dict(action=action, request=request, id=response_id, query=query,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {key: value for key, value in os.environ.items() if key not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True,
            text=True, env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failure')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(
            base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'SDK replayed request')
        if expected_status is not None:
            require(not result['ok'] and result['error']['status'] == expected_status,
                    'expected protocol error: '+label)
        else:
            require(run.returncode == 0 and result['ok'], 'SDK request failed: '+label)
        self.same_model()
        self.drained(label+'-drain')
        return result.get('result')

    def business(self):
        rows = self.plan['requests']
        for label in ('plain', 'probabilities', 'reasoning'):
            response = self.sdk(label, 'create', rows[label])
            require(response['status'] == 'completed' and text(response).strip() == '42',
                    label+' arithmetic result')
            check_usage(response)
            if label == 'plain':
                self.sdk('stateless-not-stored', 'retrieve', response_id=response['id'], expected_status=404)
            if label == 'probabilities':
                check_probabilities(response)
                stored = self.sdk('probabilities-get', 'retrieve', response_id=response['id'],
                    query=dict(include=['message.output_text.logprobs']))
                require(stored == response, 'stored response differs from committed result')
                replay = self.sdk('probabilities-replay', 'retrieve', response_id=response['id'],
                    query=dict(stream=True, include_obfuscation=False,
                               include=['message.output_text.logprobs']))
                require(replay == {key: value for key, value in response.items()
                                   if key != 'output_text'}, 'replayed terminal differs')
            if label == 'reasoning':
                require(response['usage']['output_tokens_details']['reasoning_tokens'] > 0,
                        'unbounded reasoning not counted')
        tool = self.sdk('tool', 'create', rows['tool'])
        check_usage(tool)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(tool['status'] == 'completed' and len(calls) == 1 and
                calls[0]['status'] == 'completed' and calls[0]['name'] == 'read_project_record',
                'completed tool call missing')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=DOCUMENT, start_line=1, line_count=3), 'tool arguments')
        result = base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][DOCUMENT],
                'actual document identity')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', follow)
        require(final['status'] == 'completed', 'tool follow-up terminal')
        check_usage(final)
        answer = json.loads(text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'real document answer/citation')
        inputs = self.sdk('follow-inputs', 'input_items', response_id=final['id'],
                          query=dict(order='asc', limit=100))
        require(not inputs['has_more'] and any(item.get('call_id') == calls[0]['call_id'] and
            item['type'] == 'function_call_output' for item in inputs['data']), 'tool history missing')
        truncated = self.sdk('truncated', 'create', rows['truncated'])
        require(truncated['status'] == 'incomplete' and
            truncated['incomplete_details']['reason'] == 'max_output_tokens', 'truncation status')
        raw = (self.out/'truncated.wire').read_bytes()
        require(b'response.function_call_arguments.done' not in raw and
            b'response.completed' not in raw, 'truncated tool was authorized')
        check_usage(truncated)
        deleted = self.sdk('delete', 'delete', response_id=final['id'])
        require(deleted == dict(id=final['id'], object='response.deleted', deleted=True), 'delete receipt')
        self.sdk('deleted-absent', 'retrieve', response_id=final['id'], expected_status=404)
        chat = self.sdk_request('same-pid-chat', rows['chat'])
        require(chat.finish_reason == 'stop' and chat.message['content'].strip() == '42', 'Chat continued')
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 7 and metrics['q4t_requests_success_total'] == 7 and
                metrics['q4t_requests_error_total'] == 0, 'generation accounting')


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
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
