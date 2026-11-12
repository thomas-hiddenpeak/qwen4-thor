"""Actual context truncation: bounded official SDK/HTTP, no performance claim."""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)


def history(question):
    archive = 'archive record\n' * 4200
    return [dict(role='user', content=archive),
            dict(role='assistant', content=archive),
            dict(role='user', content=question)]


def requests():
    original = foreground.requests()
    plain = dict(original['plain'], store=True, truncation='auto',
                 instructions='Answer the current question using only its result.',
                 input=history(original['plain']['input']))
    tool = dict(original['tool'], truncation='auto',
                input=history(original['tool']['input']))
    return dict(json=plain, sse=dict(plain, stream=True,
                    stream_options=dict(include_obfuscation=False)),
                disabled=dict(plain, truncation='disabled'),
                oversized=dict(plain, input=[plain['input'][0]]),
                tool=tool, follow=dict(original['follow'], truncation='auto'),
                background=dict(plain, background=True))


def check_history(actual, expected):
    require(not actual['has_more'] and len(actual['data']) == len(expected),
            'truncation changed durable input count')
    for saved, original in zip(actual['data'], expected):
        require(bool(saved.get('id')), 'saved item identity missing')
        projected = dict(original)
        if isinstance(projected.get('content'), str):
            projected['content'] = [dict(type='input_text', text=projected['content'])]
        require(all(saved.get(k) == v for k, v in projected.items()),
                'truncation changed durable input content')


def check_error(value):
    require(not value['ok'] and value['error']['status'] == 400 and
            value['error']['code'] == 'context_length_exceeded',
            'overflow must remain an SDK context error')


def prepare(args):
    prior = None
    if args.resume_from:
        directory = args.resume_from.resolve(strict=True)
        old = read(directory/'plan.json')
        result = read(directory/'result.json')
        cleanup = read(directory/'cleanup.json')
        require(old['binary_sha256'] == sha(args.binary) and
                result['counts']['starts'] == 1 and result['counts']['chats'] == 1
                and result['status'] == 'FAILED' and
                not cleanup['remaining'] and cleanup['model_exit'] == 0 and
                cleanup['ports_released'], 'resume requires known clean first run')
        expected = {'tools/serve_responses/context_acceptance.py',
                    'tools/serve_responses/test_context_acceptance.py'}
        changes = {n for n, h in old['sources'].items() if sha(ROOT/n) != h}
        require(changes == expected, 'resume changed more than the driver')
        queries = [json.loads(v) for v in
                   (directory/'lease/nvml-queries.jsonl').read_text().splitlines()]
        require(bool(queries) and all(v['status'] == 'OK' for v in queries),
                'prior monitoring incomplete')
        current = foreground.base.lifecycle.guard.processes()
        require(not any(current.get(int(pid), {}).get('start_ticks') == v['start_ticks']
                        for pid, v in cleanup['owned'].items()), 'prior process remains')
        background.probe_ports((old['port'], old['operations_port']))
        with foreground.base.lifecycle.guard.common_lock().open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        check_history(read(directory/'json-inputs.sdk.json')['result'],
                      requests()['json']['input'])
        paths = ['plan.json', 'result.json', 'cleanup.json', 'json.sdk.json',
                 'json-inputs.sdk.json', 'retained-preflight.wire',
                 'prefix-preflight.wire', 'lease/exit.json', 'lease/nvml-queries.jsonl']
        prior = dict(directory=str(directory), files={str(directory/n):
                     sha(directory/n) for n in paths}, verified_cleanup=True,
                     reuse='Completed JSON/current task/input token count and full original input_items')
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['planned'] = dict(starts=1, chats=7)
    if prior:
        plan['prior'] = prior
        plan['planned'] = dict(starts=2, chats=7, additional_starts=1, additional_chats=6)
    plan['mutations'] = ['only owned test model and isolated application objects']
    plan['derived_requests'] = [
        'Chat preflight measures exactly the retained prompt; long prefix must exceed 8192 tokens',
        'actual document function_call_output and previous_response_id feed follow request',
        'SDK retrieve/input_items/replay only reference this stage response IDs',
        'no retries, model restart, visual processing or default publication']
    save(path, plan)


class Stage(foreground.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_CONTEXT_BUSINESS'

    def __init__(self, plan):
        super().__init__(plan)
        self.prior = None
        if plan.get('prior'):
            prior = plan['prior']
            require(all(sha(p) == h for p, h in prior['files'].items()),
                    'prior evidence drift')
            self.prior = Path(prior['directory'])
            spent = read(self.prior/'result.json')
            require(spent['counts']['starts'] == 1 and spent['counts']['chats'] == 1
                    and spent['status'] == 'FAILED', 'unexpected resume boundary')
            self.counts.update(spent['counts'])
            self.started -= spent['seconds']

    def preflight(self, label, messages, expected_status):
        self.budget('controls')
        body = dict(request=dict(model=foreground.MODEL, enable_thinking=False,
            max_completion_tokens=192, temperature=0, messages=messages),
            include_prompt=True, include_token_ids=True)
        status, raw = self.http('/v1/chat/preflight', body, label=label)
        require(status == expected_status, 'actual preflight: '+label)
        return json.loads(raw)

    def business(self):
        rows = self.plan['requests']
        caps = read(self.out/'capabilities.json')
        require(caps['responses']['truncation']['auto'], 'missing native truncation')
        instructions = dict(role='system', content=rows['json']['instructions'])
        if self.prior:
            retained = read(self.prior/'retained-preflight.wire')
            overflow = read(self.prior/'prefix-preflight.wire')
            original = read(self.prior/'json.sdk.json')['result']
            background.check_response(original, 'completed')
            require(foreground.text(original).strip() == '42' and
                    original['usage']['input_tokens'] == retained['prompt_tokens'],
                    'prior JSON generation cannot be reused')
            check_history(read(self.prior/'json-inputs.sdk.json')['result'],
                          rows['json']['input'])
        else:
            retained = self.preflight('retained-preflight',
                [instructions, rows['json']['input'][-1]], 200)
            overflow = self.preflight('prefix-preflight',
                [instructions, *rows['json']['input'][1:]], 400)
        require(overflow['error']['code'] == 'context_length_exceeded',
                'fixture prefix does not exceed real Qwen context')
        for label in (('sse',) if self.prior else ('json', 'sse')):
            response = self.sdk(label, 'create', rows[label])
            background.check_response(response, 'completed')
            require(foreground.text(response).strip() == '42', 'current task lost')
            require(response['usage']['input_tokens'] == retained['prompt_tokens'],
                    'usage differs from actual retained Qwen prompt')
            inputs = self.sdk(label+'-inputs', 'input_items',
                response_id=response['id'], query=dict(order='asc', limit=100))
            check_history(inputs, rows[label]['input'])
        for label in ('disabled', 'oversized'):
            self.sdk(label, 'create', rows[label], expected_status=400)
            check_error(read(self.out/(label+'.sdk.json')))
        tool = self.sdk('tool', 'create', rows['tool'])
        background.check_response(tool, 'completed')
        calls = [v for v in tool['output'] if v['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'tool call missing')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'tool arguments differ')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'document identity')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', follow)
        background.check_response(final, 'completed')
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'tool closure')
        inputs = self.sdk('follow-inputs', 'input_items', response_id=final['id'],
                          query=dict(order='asc', limit=100))
        require(not inputs['has_more'] and len(inputs['data']) >= 5 and
                any(v.get('call_id') == calls[0]['call_id'] and
                    v['type'] == 'function_call_output' for v in inputs['data']),
                'call/output or original durable history was lost')
        check_history(dict(has_more=False, data=inputs['data'][:3]),
                      rows['tool']['input'])
        queued = background.Stage.sdk(self, 'background', 'create', rows['background'])
        done = background.Stage.wait_response(self, 'background-done', queued['id'],
            lambda v: v['status'] == 'completed')
        require(foreground.text(done).strip() == '42' and
                done['usage']['input_tokens'] == retained['prompt_tokens'],
                'background context differs')
        self.drained('background-drained')
        inputs = self.sdk('background-inputs', 'input_items', response_id=done['id'],
                          query=dict(order='asc', limit=100))
        check_history(inputs, rows['background']['input'])
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == (6 if self.prior else 7) and
                metrics['q4t_requests_success_total'] == (4 if self.prior else 5) and
                metrics['q4t_requests_error_total'] == 2, 'terminal accounting')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18981)
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
