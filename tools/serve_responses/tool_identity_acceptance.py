"""Bounded native namespace/result-schema business with the official SDK."""
import argparse
import copy
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

base = foreground.base
LIMITS = dict(starts=1, chats=6, controls=2000, seconds=900)
CUSTOM_LIMITS = dict(starts=1, chats=3, controls=2000, seconds=600)


def requests():
    old = foreground.requests()
    function = copy.deepcopy(old['tool']['tools'][0])
    function['output_schema'] = dict(type='object', required=['ok', 'source'],
        properties=dict(ok=dict(const=True), source=dict(type='object',
            required=['sha256', 'citation', 'text'], properties=dict(
                sha256=dict(type='string', pattern='^[a-f0-9]{64}$'),
                citation=dict(type='string', pattern=r'^\[docs/MODEL\.md:L1-L3\]$'),
                text=dict(type='string', minLength=1)))))
    namespace = dict(type='namespace', name='project', description='Project records',
                     tools=[function])
    tool = dict(old['tool'], max_output_tokens=384,
        tools=[namespace, dict(namespace, name='catalog', description='Catalog records')],
        tool_choice=dict(type='function', name='project.read_project_record'))
    custom = dict(old['tool'], max_output_tokens=384,
        input='Use the project raw tool with exactly: read docs/MODEL.md',
        tools=[dict(type='namespace', name='project', description='Raw project reader',
            tools=[dict(type='custom', name='raw', format=dict(type='grammar',
                syntax='regex', definition=r'read docs/MODEL\.md'))])],
        tool_choice=dict(type='custom', name='project.raw'))
    follow = dict(old['follow'], tools=[], tool_choice='none')
    return dict(tool=tool, follow=follow, custom=custom,
                truncated=dict(custom, max_output_tokens=1))


def prepare(args):
    prior = reuse_function(args.reuse_function, args.binary) if args.reuse_function else None
    background.prepare(args, rows=requests(),
        limits=CUSTOM_LIMITS if prior else LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/tool_identity_acceptance.py'] = sha(Path(__file__))
    plan['mutations'] = ['own stored Responses/conversation only; no service fault injection',
                         'two actual bounded reads of docs/MODEL.md lines1-3']
    plan['prior_function_evidence'] = prior
    plan['derived_requests'] = [
        'call IDs, namespace and previous/conversation IDs come from actual outputs',
        'invalid JSON result returns 400 before generation even with tools removed',
        'valid follow uses the actual source text and citation; no invented tool result',
        'custom call/input list/conversation use their distinct official SDK shapes',
        'truncated custom call never executes; no retries or automatic model restart']
    save(path, plan)


def check_shape(value, shape):
    issues = errors(value, shape)
    require(not issues, shape + ': ' + str([(list(e.path), e.message[:120]) for e in issues]))


class Stage(foreground.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_TOOL_IDENTITY_RESULT_CONTRACT_BUSINESS'

    def sdk(self, label, action, request=None, **kwargs):
        value = super().sdk(label, action, request, **kwargs)
        if kwargs.get('expected_status') is not None:
            return value
        shape = {'create': 'response', 'retrieve': 'response',
                 'conversation_create': 'conversation',
                 'conversation_items': 'conversation_items',
                 'input_items': 'input_items_list'}[action]
        # Some official SDK versions add output_text as a convenience property.
        public = {k: v for k, v in value.items() if k != 'output_text'}
        check_shape(public, shape)
        events = read(self.out / (label + '.sdk.json')).get('chunks') or []
        for event in events:
            check_shape(event, 'response_event')
        wire = (self.out / (label + '.wire')).read_text()
        if not events:
            check_shape(json.loads(wire), shape)
        require('q4t_ns_' not in wire, 'private model alias exposed')
        return public

    def result(self, label, call):
        if call['type'] == 'function_call':
            args = json.loads(call['arguments'])
            require(args == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                    'wrong function arguments')
        else:
            require(call['input'] == 'read docs/MODEL.md', 'wrong raw input')
            args = dict(path=foreground.DOCUMENT, start_line=1, line_count=3)
        result = base.ProjectRecords(ROOT).read(args)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'actual document changed')
        save(self.out / (label + '-execution.json'), dict(call=call, result=result, executions=1))
        return result

    def answer(self, label, request, result):
        response = self.sdk(label, 'create', request)
        background.check_response(response, 'completed')
        answer = json.loads(foreground.text(response))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'],
                'actual title/citation differs')
        return response

    def business(self):
        caps = read(self.out / 'capabilities.json')['responses']
        require(caps['tool_identity']['namespaces'] and
                caps['tool_results']['binding'] == 'invocation_time', 'capability missing')
        rows = self.plan['requests']
        tool = self.sdk('namespace-function', 'create', rows['tool'])
        background.check_response(tool, 'completed')
        calls = [x for x in tool['output'] if x['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['name'] == 'read_project_record' and
                calls[0]['namespace'] == 'project', 'wrong namespaced function')
        result = self.result('function', calls[0])
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output='{"ok":false,"source":{}}')])
        self.sdk('invalid-result', 'create', follow, expected_status=400)
        rejected = read(self.out / 'invalid-result.sdk.json')['error']
        require(rejected['code'] == 'invalid_tool_output' and
                rejected['param'].endswith('.output'), 'result contract was bypassed')
        follow['input'][0]['output'] = json.dumps(result, ensure_ascii=False)
        completed = self.answer('function-follow', follow, result)
        self.sdk('function-inputs', 'input_items', response_id=completed['id'])
        self.custom_business()
        save(self.out / 'business-summary.json', dict(model_starts=1, create_requests=6,
            complete_generations=4, invalid_results=1, incomplete_generations=1,
            actual_tool_executions=2, identity_and_result_contract=True,
            full_quality_or_performance_claim=False))

    def custom_business(self):
        rows = self.plan['requests']
        conversation = self.sdk('conversation', 'conversation_create', {})
        custom_request = dict(rows['custom'], conversation=conversation['id'])
        custom = self.sdk('namespace-custom', 'create', custom_request)
        background.check_response(custom, 'completed')
        calls = [x for x in custom['output'] if x['type'] == 'custom_tool_call']
        require(len(calls) == 1 and calls[0]['name'] == 'raw' and
                calls[0]['namespace'] == 'project' and 'status' not in calls[0],
                'wrong custom public identity/shape')
        result = self.result('custom', calls[0])
        follow = copy.deepcopy(rows['follow'])
        follow.update(conversation=conversation['id'], input=[dict(type='custom_tool_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        completed = self.answer('custom-follow', follow, result)
        self.sdk('custom-inputs', 'input_items', response_id=completed['id'])
        self.sdk('conversation-items', 'conversation_items', response_id=conversation['id'])
        truncated = self.sdk('truncated', 'create', rows['truncated'])
        background.check_response(truncated, 'incomplete')
        events = read(self.out / 'truncated.sdk.json')['chunks']
        require(not any(e['type'] == 'response.custom_tool_call_input.done' for e in events),
                'truncated call claimed executable input')
        self.drained('final')


class CustomStage(Stage):
    limits = CUSTOM_LIMITS
    success_status = 'PASS_CUSTOM_IDENTITY_REMAINING_BUSINESS'

    def business(self):
        prior = self.plan['prior_function_evidence']
        require(prior and prior['binary_sha256'] == self.plan['binary_sha256'],
                'prior function engine differs')
        for path, digest in prior['records'].items():
            require(sha(Path(path)) == digest, 'prior evidence changed: ' + path)
        self.custom_business()
        save(self.out / 'business-summary.json', dict(model_starts=1, create_requests=3,
            complete_generations=2, incomplete_generations=1, actual_tool_executions=1,
            reused_function_evidence=prior, aggregate_model_starts=2,
            aggregate_create_requests=6, full_quality_or_performance_claim=False))


def reuse_function(directory, binary):
    directory = directory.resolve(strict=True)
    result = read(directory / 'result.json')
    require(result['status'] == 'FAILED' and result['counts']['starts'] == 1 and
            result['counts']['chats'] == 3 and 'first_id' in result['error'] and
            'last_id' in result['error'], 'unrecognized partial attempt')
    plan = read(directory / 'plan.json')
    require(plan['binary_sha256'] == sha(binary) and
            plan['sources'][foreground.DOCUMENT] == sha(ROOT / foreground.DOCUMENT),
            'prior engine/document changed')
    cleanup = read(directory / 'cleanup.json')
    require(cleanup['model_exit'] == 0 and not cleanup['remaining'] and
            cleanup['ports_released'] and cleanup['lease_continuously_held'],
            'prior cleanup incomplete')
    for name in ['namespace-function', 'function-follow']:
        response = read(directory / (name + '.sdk.json'))
        require(response['ok'] and len(response['exchanges']) == 1, 'prior SDK failed')
        background.check_response(response['result'], 'completed')
    rejected = read(directory / 'invalid-result.sdk.json')
    require(not rejected['ok'] and rejected['error']['status'] == 400 and
            rejected['error']['code'] == 'invalid_tool_output', 'prior rejection differs')
    check_shape(read(directory / 'function-inputs.wire'), 'input_items_list')
    lease = directory.parent / 'lease-01'
    guard = read(lease / 'exit.json')
    events = [json.loads(line) for line in (lease / 'events.jsonl').read_text().splitlines()]
    monitors = [row for row in events if row['event'] == 'monitor']
    require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1 and
            guard['compute_backend'] == 'nvml' and monitors and
            events[-1]['event'] == 'lease_released' and
            all(not row.get('conflicts') and not row.get('unreadable_processes')
                for row in events) and
            not any(row['event'] in ('env_abort', 'monitor_error') for row in events),
            'prior strict GPU environment not accepted')
    names = ['plan.json', 'result.json', 'cleanup.json', 'function-execution.json']
    for name in ['namespace-function', 'invalid-result', 'function-follow', 'function-inputs']:
        names.extend([name + '.sdk.json', name + '.wire'])
    return dict(binary_sha256=plan['binary_sha256'], original_attempt_failed=True,
        accepted_prefix='namespaced function, result rejection and actual document JSON closure',
        records={str(path): sha(path) for path in
                 [*(directory / name for name in names),
                  lease / 'exit.json', lease / 'events.jsonl']})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18164)
    p.add_argument('--reuse-function', type=Path)
    p = modes.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            plan = read(args.plan)
            cls = CustomStage if plan.get('prior_function_evidence') else Stage
            cls(plan).run()


if __name__ == '__main__':
    main()
