"""Bounded async function/custom calls, independent work and real late results."""
import argparse
import copy
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import tool_identity_acceptance as identity
from tools.serve_responses import background_acceptance as background
from tools.serve_service.package import read, require, save, sha

foreground = identity.foreground
base = identity.base
LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)
REPAIR_LIMITS = dict(starts=1, chats=5, controls=2000, seconds=600)


def reuse_function(directory):
    directory = directory.resolve(strict=True)
    previous = read(directory / 'result.json')
    require(previous['status'] == 'FAILED' and
            previous['counts']['starts'] == 1 and previous['counts']['chats'] == 5 and
            'same response did not complete independent work' in previous['error'],
            'unrecognized async attempt')
    plan = read(directory / 'plan.json')
    changed = {name for name, digest in plan['sources'].items()
               if name.startswith(('src/', 'include/')) and sha(ROOT / name) != digest}
    require(changed == {'include/q4t/server/chat_template.h',
                       'src/server/chat_template.cpp', 'src/server/chat_prompt.cpp'},
            'reuse requires only the reviewed history renderer change')
    require(plan['sources'][foreground.DOCUMENT] == sha(ROOT / foreground.DOCUMENT),
            'actual document changed')
    cleanup = read(directory / 'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'previous cleanup incomplete')
    for label in ['function-call', 'function-independent', 'function-result']:
        response = read(directory / (label + '.sdk.json'))
        require(response['ok'], 'previous SDK failure')
        background.check_response(response['result'], 'completed')
    require(foreground.text(read(directory / 'function-independent.sdk.json')
                            ['result']).strip() == '42', 'independent answer differs')
    rejected = read(directory / 'invalid-result.sdk.json')['error']
    require(rejected['status'] == 400 and rejected['code'] == 'invalid_tool_output',
            'result rejection differs')
    execution = read(directory / 'function-execution.json')
    answer = json.loads(foreground.text(read(directory / 'function-result.sdk.json')
                                       ['result']))
    require(execution['executions'] == 1 and answer['citation'] ==
            execution['result']['source']['citation'], 'real function closure differs')
    lease = directory.parent / 'lease-01'
    events = [json.loads(line) for line in (lease / 'events.jsonl').read_text().splitlines()]
    guard = read(lease / 'exit.json')
    require(guard['status'] == 'COMMAND_FAILED' and guard['compute_backend'] == 'nvml' and
            events[-1]['event'] == 'lease_released' and
            any(row['event'] == 'monitor' for row in events) and
            all(not row.get('conflicts') and not row.get('unreadable_processes') and
                row['event'] not in ('env_abort', 'monitor_error') for row in events),
            'prior strict environment failed')
    records = [p for p in directory.iterdir() if p.is_file() and
               (p.name.startswith(('function-', 'invalid-result')) or
                p.name in ('plan.json', 'result.json', 'cleanup.json'))]
    records.extend([lease / 'exit.json', lease / 'events.jsonl'])
    return dict(directory=str(directory), binary_sha256=plan['binary_sha256'],
        original_attempt_failed=True, changed_sources=sorted(changed),
        scope='function late result and output schema only; both initial continuations rechecked',
        records={str(p): sha(p) for p in records})


def requests():
    rows = identity.requests()
    for key in ('tool', 'custom'):
        rows[key] = copy.deepcopy(rows[key])
        for namespace in rows[key]['tools']:
            for tool in namespace['tools']:
                tool['async'] = True
                tool['allowed_callers'] = ['direct']
        rows[key]['input'] += (' Independently, explain what LLM stands for in '
                               'one short English sentence while the result is pending.')
        rows[key]['max_output_tokens'] = 384
    rows['independent'] = dict(model=foreground.MODEL, temperature=0,
        reasoning=dict(effort='none'), tools=[], tool_choice='none',
        max_output_tokens=48, input='Compute 17 + 25. Reply only with 42.')
    rows['follow'].update(tools=[], tool_choice='none')
    rows.pop('truncated')
    return rows


def prepare(args):
    prior = reuse_function(args.reuse_function) if args.reuse_function else None
    background.prepare(args, rows=requests(), limits=REPAIR_LIMITS if prior else LIMITS,
                       scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/async_acceptance.py'] = sha(Path(__file__))
    plan['mutations'] = ['own stored Responses/conversation only',
                         'two bounded reads of docs/MODEL.md after unrelated turns']
    plan['derived_requests'] = [
        'function uses previous_response_id, custom uses a native conversation',
        'call done precedes independent text within the same response',
        'unrelated turn precedes actual client tool execution and result submission',
        'two pre-generation rejections: wrong output_schema and missing sync result',
        'all model steps share the original output allowance and cancellation; no retries']
    if prior:
        plan['reused_function'] = prior
        plan['derived_requests'].append(
            'repair: both initial continuations, custom independent/result and missing sync only')
    save(path, plan)


class Stage(identity.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_ASYNC_TOOL_LATE_RESULT_BUSINESS'

    def __init__(self, plan):
        if prior := plan.get('reused_function'):
            require(reuse_function(Path(prior['directory'])) == prior,
                    'prior function evidence changed')
            self.limits = REPAIR_LIMITS
        super().__init__(plan)

    def business(self):
        caps = read(self.out / 'capabilities.json')['responses']['async_tools']
        require(caps['same_response_continuation'] and caps['late_results'] and
                caps['max_independent_steps'] == 1, 'async capability missing')
        rows = self.plan['requests']
        for custom in (False, True):
            name = 'custom' if custom else 'function'
            request = copy.deepcopy(rows['custom' if custom else 'tool'])
            conversation = None
            if custom:
                conversation = self.sdk('conversation', 'conversation_create', {})['id']
                request['conversation'] = conversation
            response = self.sdk(name + '-call', 'create', request)
            background.check_response(response, 'completed')
            kind = 'custom_tool_call' if custom else 'function_call'
            calls = [x for x in response['output'] if x['type'] == kind]
            require(len(calls) == 1 and calls[0].get('async') is True and
                    calls[0].get('caller') == dict(type='direct'), 'async call identity')
            call = calls[0]
            require('large language model' in foreground.text(response).lower(),
                    'same response did not complete independent work')
            require(response['usage']['output_tokens'] <= request['max_output_tokens'],
                    'independent answer replenished output budget')
            events = read(self.out / (name + '-call.sdk.json'))['chunks']
            done = next(i for i, event in enumerate(events)
                        if event['type'] == 'response.output_item.done' and
                        event['item'].get('call_id') == call['call_id'])
            require(any(event['type'] == 'response.output_text.delta'
                        for event in events[done + 1:]), 'no streamed answer after async done')
            if not custom and self.plan.get('reused_function'):
                continue
            independent = copy.deepcopy(rows['independent'])
            independent['conversation' if custom else 'previous_response_id'] = (
                conversation if custom else response['id'])
            middle = self.sdk(name + '-independent', 'create', independent)
            background.check_response(middle, 'completed')
            require(foreground.text(middle).strip() == '42', 'unrelated turn failed')
            follow = copy.deepcopy(rows['follow'])
            follow['conversation' if custom else 'previous_response_id'] = (
                conversation if custom else middle['id'])
            follow['input'] = [dict(type=kind + '_output', call_id=call['call_id'],
                                    output='{"ok":false,"source":{}}')]
            if not custom:
                self.sdk('invalid-result', 'create', follow, expected_status=400)
                rejected = read(self.out / 'invalid-result.sdk.json')['error']
                require(rejected['code'] == 'invalid_tool_output', 'original schema unbound')
            result = self.result(name, call)  # Execute once, only after unrelated work.
            follow['input'][0]['output'] = json.dumps(result, ensure_ascii=False)
            follow['stream'] = custom
            if custom:
                follow['stream_options'] = dict(include_obfuscation=False)
            completed = self.answer(name + '-result', follow, result)
            self.sdk(name + '-inputs', 'input_items', response_id=completed['id'])
        sync = dict(rows['independent'], input=[dict(type='function_call',
            name='read_project_record', call_id='synchronous_missing', arguments='{}'),
            dict(role='user', content='Continue without a result')])
        self.sdk('missing-sync', 'create', sync, expected_status=400)
        self.drained('final')
        repair = bool(self.plan.get('reused_function'))
        save(self.out / 'business-summary.json', dict(model_starts=1,
            create_requests=5 if repair else 8,
            successful_responses=4 if repair else 6,
            maximum_model_steps=6 if repair else 8,
            rejected_before_generation=1 if repair else 2,
            actual_tool_executions=1 if repair else 2,
            reused_function=self.plan.get('reused_function'),
            independent_work_before_result=True,
            full_quality_or_performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18168)
    p.add_argument('--reuse-function', type=Path)
    p = modes.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
