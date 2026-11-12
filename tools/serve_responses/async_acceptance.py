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
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
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
    save(path, plan)


class Stage(identity.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_ASYNC_TOOL_LATE_RESULT_BUSINESS'

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
        save(self.out / 'business-summary.json', dict(model_starts=1, create_requests=8,
            successful_responses=6, maximum_model_steps=8, rejected_before_generation=2,
            actual_tool_executions=2, independent_work_before_result=True,
            full_quality_or_performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18168)
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
