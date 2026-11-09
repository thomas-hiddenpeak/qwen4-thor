"""Actual native summaries, shared budgets and one client admission; bounded GPU."""
import argparse
import base64
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MAX_MODEL_STEPS = 13
HEADERS = {'X-Q4T-Client-Id': 'summary-test', 'X-Q4T-Gateway-Token': 'a' * 64}


def requests():
    old = foreground.requests()
    plain = dict(old['plain'], store=True, max_output_tokens=384,
                 reasoning=dict(effort='low', summary='concise'))
    tool = dict(old['tool'], max_output_tokens=384,
                reasoning=dict(effort='low', summary='concise'))
    follow = dict(old['follow'], reasoning=dict(effort='low', summary='auto'))
    return dict(concise=plain,
        detailed=dict(plain, reasoning=dict(effort='low', summary='detailed'),
                      stream=True, stream_options=dict(include_obfuscation=False)),
        auto=dict(plain, reasoning=dict(effort='low', summary='auto'), background=True),
        no_analysis=dict(plain, reasoning=dict(effort='none', summary='auto')),
        incomplete=dict(plain, max_output_tokens=1, stream=True,
                        stream_options=dict(include_obfuscation=False)),
        tool=tool, follow=follow, chat=old['chat'])


def check_summary(value, *, arithmetic=False):
    background.check_response(value, 'completed')
    raw = '\n'.join(p['text'] for item in value['output']
                    if item['type'] == 'reasoning' for p in item.get('content', [])
                    if p['type'] == 'reasoning_text')
    summaries = [item for item in value['output']
                 if item['type'] == 'reasoning' and item.get('summary')]
    require(raw.strip() and len(summaries) == 1, 'actual reasoning and derived summary required')
    item = summaries[0]
    require(not item.get('content') and item['status'] == 'completed',
            'derived summary must be a distinct completed item')
    summary = ''.join(p['text'] for p in item['summary'])
    require(summary.strip() and summary.strip() != raw.strip(), 'raw reasoning renamed as summary')
    if arithmetic:
        require(foreground.text(value).strip() == '42' and '42' in summary,
                'arithmetic answer or summary lost the actual result')
    return dict(raw=raw, summary=summary, item_id=item['id'])


def check_steps(log, response, expected):
    choices = [int(v) for v in re.findall(r'\[sampling\] request=' +
        re.escape(response['id']) + r' choice=(\d+)/', log)]
    require(choices == list(range(expected)), 'actual internal model steps differ')


def prepare(args):
    prior = None
    if args.resume_from:
        directory = args.resume_from.resolve(strict=True)
        old = read(directory/'plan.json')
        result = read(directory/'result.json')
        cleanup = read(directory/'cleanup.json')
        require(result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
                result['counts']['chats'] == 3 and 'detailed-replay' in result['error'] and
                cleanup['model_exit'] == 0 and not cleanup['remaining'] and
                cleanup['ports_released'], 'resume requires known first failure and cleanup')
        allowed = {'src/server/responses_api.cpp', 'tests/server_responses_api_test.cpp',
                   'tools/serve_responses/summary_acceptance.py',
                   'tools/serve_responses/test_summary_acceptance.py'}
        changes = {n for n, h in old['sources'].items() if sha(ROOT/n) != h}
        require(changes <= allowed and 'src/server/responses_api.cpp' in changes,
                'resume changed generation or unrelated sources')
        samples = [json.loads(v) for v in (directory/'lease/nvml-queries.jsonl').read_text().splitlines()]
        require(samples and all(v['status'] == 'OK' for v in samples), 'prior monitoring failed')
        current = foreground.base.lifecycle.guard.processes()
        require(not any(current.get(int(pid), {}).get('start_ticks') == v['start_ticks']
                        for pid, v in cleanup['owned'].items()), 'prior owned process remains')
        background.probe_ports((old['port'], old['operations_port']))
        with foreground.base.lifecycle.guard.common_lock().open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        values = [read(directory/(name+'-summary.json'))['response']
                  for name in ('concise', 'detailed', 'auto')]
        for value in values:
            check_summary(value, arithmetic=True)
            check_steps((directory/'model.log').read_text(), value, 2)
        metrics = read(directory/'auto-drain.metrics.json')[-1]
        require(metrics['q4t_client_rate_admitted_total{client="summary-test"}'] == 3 and
                metrics['q4t_client_rate_reserved_tokens{client="summary-test"}'] == 0 and
                metrics['q4t_client_rate_settled_tokens_total{client="summary-test"}'] ==
                sum(v['usage']['total_tokens'] for v in values), 'prior actual rate accounting')
        replay = read(directory/'detailed-replay.sdk.json')
        require(not replay['ok'] and replay['error']['message'] == 'terminated' and
                replay['chunks'][-1]['type'] == 'response.completed', 'different replay defect')
        prior = dict(directory=str(directory), counts=result['counts'], seconds=result['seconds'],
            files={str(p): sha(p) for p in directory.rglob('*') if p.is_file()},
            reused_generation=3, reused_model_steps=6, verified_cleanup=True,
            source_changes=sorted(changes))
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=1, chats=8, maximum_internal_model_steps=MAX_MODEL_STEPS)
    if prior:
        plan['prior'] = prior
        plan['planned'].update(starts=2, additional_starts=1, additional_chats=5)
    plan['mutations'] = ['only owned test model, isolated objects and synthetic test client policy']
    plan['derived_requests'] = ['actual document tool result and previous_response_id',
        'SDK retrieve/input_items/replay only refer to owned response identities',
        'primary arithmetic Chat preflight measures real token input without generation',
        'no retry, no default changes, no long run or performance acceptance']
    save(path, plan)


class Stage(foreground.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_SUMMARY_BUSINESS'

    def __init__(self, plan):
        super().__init__(plan)
        self.prior = None
        if plan.get('prior'):
            prior = plan['prior']
            require(all(sha(p) == h for p, h in prior['files'].items()), 'prior evidence drift')
            self.prior = Path(prior['directory'])
            self.counts.update(prior['counts'])
            self.started -= prior['seconds']
            # Only completed, owned test records. Preserve the original evidence.
            shutil.copytree(self.prior/'objects', self.out/'objects')

    def http(self, path, body=None, *, label=None, timeout=30):
        if body is None:
            self.budget('controls')
        headers = {} if path in ('/readyz', '/metrics', '/v1/config') else dict(HEADERS)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=timeout)
        started = time.monotonic()
        try:
            connection.request('GET' if body is None else 'POST', path,
                body=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, 'response size bound')
            if label:
                (self.out / (label + '.wire')).write_bytes(raw)
                save(self.out / (label + '.http.json'), dict(status=response.status,
                    headers=dict(response.getheaders()), seconds=time.monotonic()-started))
            return response.status, raw
        finally:
            connection.close()

    def start(self):
        background.probe_ports((self.plan['port'], self.plan['operations_port']))
        policy = dict(version=3, revision=0, fair_queue=True,
            gateway_token=HEADERS['X-Q4T-Gateway-Token'], clients=[dict(id='summary-test',
                max_inflight=4, max_control_inflight=4, max_prompt_tokens=16384,
                max_output_tokens=512, max_body_bytes=1048576,
                requests_per_minute=8, tokens_per_minute=32768)])
        save(self.out/'client-policy.json', policy)
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '384', '--max-inflight-requests', '8',
            '--max-queued-requests', '2', '--object-store-dir', str(self.out/'objects'),
            '--client-policy-file', str(self.out/'client-policy.json')]
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = foreground.base.lifecycle.guard.processes()[self.model.pid]
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
            status, raw = self.http('/v1/' + name, label='initial-' + name)
            require(status == 200, name + ' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        self.client.protocol_sha256 = read(self.out/'capabilities.json')['protocol_source_sha256']

    def sdk(self, label, action, request=None, *, response_id=None, query=None, drain=True):
        self.budget('chats' if action == 'create' else 'controls')
        command = dict(action=action, request=request, id=response_id, query=query,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1", headers=HEADERS,
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True,
            text=True, env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failed')
        result = json.loads(run.stdout)
        (self.out/(label+'.wire')).write_bytes(base64.b64decode(result.pop('wire_base64'), validate=True))
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1 and run.returncode == 0 and result['ok'],
                'SDK request failed: ' + label + ' ' + str(result.get('error')))
        for event in result.get('chunks') or []:
            require(not errors(event, 'response_event'), 'event schema: ' + event['type'])
        value = result['result']
        if value.get('object') == 'response':
            background.check_response(value)
        self.same_model()
        if drain:
            self.drained(label+'-drain')
        return value

    def business(self):
        rows = self.plan['requests']
        require(read(self.out/'capabilities.json')['responses']['reasoning_summary'],
                'summary capability absent')
        if self.prior:
            prompt_tokens = read(self.prior/'primary-preflight.wire')['prompt_tokens']
            values = [read(self.prior/(name+'-summary.json'))['response']
                      for name in ('concise', 'detailed', 'auto')]
        else:
            self.budget('controls')
            status, raw = self.http('/v1/chat/preflight', dict(request=dict(
                model=foreground.MODEL, messages=[dict(role='user', content=rows['concise']['input'])],
                enable_thinking=True, reasoning_effort='low', max_completion_tokens=384,
                temperature=0), include_token_ids=True), label='primary-preflight')
            require(status == 200, 'real primary preflight failed')
            prompt_tokens = json.loads(raw)['prompt_tokens']
            values = []
        for label in (() if self.prior else ('concise', 'detailed', 'auto')):
            value = self.sdk(label, 'create', rows[label], drain=label != 'auto')
            if label == 'auto':
                value = background.Stage.wait_response(self, 'auto-done', value['id'],
                    lambda v: v['status'] == 'completed')
                self.drained('auto-drain')
            derived = check_summary(value, arithmetic=True)
            require(value['usage']['input_tokens'] > prompt_tokens and
                    value['usage']['output_tokens'] <= 384, 'shared usage/budget')
            check_steps((self.out/'model.log').read_text(), value, 2)
            save(self.out/(label+'-summary.json'), dict(response=value, **derived))
            retrieved = self.sdk(label+'-get', 'retrieve', response_id=value['id'])
            require(check_summary(retrieved, arithmetic=True) == derived, 'stored summary changed')
            values.append(value)
        replay = self.sdk('detailed-replay', 'retrieve', response_id=values[1]['id'],
                          query=dict(stream=True, include_obfuscation=False))
        require(check_summary(replay, arithmetic=True) == check_summary(values[1], arithmetic=True),
                'summary replay changed')
        for label in ('no_analysis', 'incomplete'):
            value = self.sdk(label, 'create', rows[label])
            background.check_response(value, 'incomplete' if label == 'incomplete' else 'completed')
            require(not any(item.get('summary') for item in value['output']), 'fabricated summary')
            check_steps((self.out/'model.log').read_text(), value, 1)
            require(value['usage']['output_tokens'] <= rows[label]['max_output_tokens'], 'budget renewed')
            values.append(value)
        tool = self.sdk('tool', 'create', rows['tool'])
        check_summary(tool)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'actual tool call missing')
        args = json.loads(calls[0]['arguments'])
        require(args == dict(path=foreground.DOCUMENT, start_line=1, line_count=3), 'tool arguments')
        result = foreground.base.ProjectRecords(ROOT).read(args)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT],
                'real document identity')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', follow)
        check_summary(final)
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'actual document closure')
        for value in (tool, final):
            check_steps((self.out/'model.log').read_text(), value, 2)
        values += [tool, final]
        chat_status, chat_raw = self.submit('chat', rows['chat'])
        chat = json.loads(chat_raw)
        require(chat_status == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary Chat continuation')
        metrics = self.drained('final')
        current_values = values[3:] if self.prior else values
        expected_input = sum(v['usage']['input_tokens'] for v in current_values) + chat['usage']['prompt_tokens']
        expected_output = sum(v['usage']['output_tokens'] for v in current_values) + chat['usage']['completion_tokens']
        key = '{client="summary-test"}'
        current_count = 5 if self.prior else 8
        require(metrics['q4t_requests_total'] == current_count and metrics['q4t_requests_success_total'] == current_count and
                metrics['q4t_requests_error_total'] == 0, 'one external request accounting')
        require(metrics['q4t_client_rate_admitted_total'+key] == current_count and
                metrics['q4t_client_rate_reserved_tokens'+key] == 0 and
                metrics['q4t_client_rate_settled_tokens_total'+key] == expected_input + expected_output,
                'client RPM/TPM/reservation differs from actual total usage')
        require(metrics['q4t_prompt_tokens_total'] == expected_input and
                metrics['q4t_generation_tokens_total'] == expected_output, 'internal step token metrics')
        steps = len(re.findall(r'^\[sampling\] request=', (self.out/'model.log').read_text(), re.M))
        if self.prior:
            steps += self.plan['prior']['reused_model_steps']
        require(steps == MAX_MODEL_STEPS, 'internal model step bound')
        save(self.out/'business-checks.json', dict(internal_model_steps=steps,
            external_requests=8, current_run_requests=current_count,
            current_run_input_tokens=expected_input, current_run_output_tokens=expected_output,
            real_tool_executions=1, shared_budget=True, actual_summary=True,
            single_rpm=True, cumulative_tpm=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18983)
    command.add_argument('--resume-from', type=Path)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
