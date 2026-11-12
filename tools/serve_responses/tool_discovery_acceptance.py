"""Bounded real deferred discovery, loaded calls and project document results."""
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
LIMITS = dict(starts=1, chats=10, controls=2000, seconds=900)


def requests():
    original = identity.requests()
    rows = {}
    for name, old in [('server_function', 'tool'), ('server_custom', 'custom')]:
        row = copy.deepcopy(original[old])
        namespace = row['tools'][0]
        for tool in namespace['tools']:
            tool['defer_loading'] = True
        row.update(tools=[dict(type='tool_search'), namespace],
                   tool_choice='required', max_tool_calls=1, max_output_tokens=256)
        row['input'] = ('First discover the project namespace with the search '
                        'input {"paths":["project"]}. After its tool definitions '
                        'are loaded, call the project tool. ' + row['input'])
        row['stream'] = name == 'server_custom'
        if not row['stream']:
            row.pop('stream_options', None)
        rows[name] = row
    rows['client_search'] = dict(model=foreground.MODEL, temperature=0,
        reasoning=dict(effort='none'), max_output_tokens=256, max_tool_calls=1,
        tool_choice='required', input='Search for project documentation using '
            'the query project. Wait for the application to provide the tools.',
        tools=[dict(type='tool_search', execution='client',
            description='Find the tools for reading real project documentation.',
            parameters=base.closed(dict(query=dict(type='string', const='project'))))])
    rows['loaded_function'] = dict(model=foreground.MODEL, temperature=0,
        reasoning=dict(effort='none'), max_output_tokens=256,
        tools=copy.deepcopy(rows['client_search']['tools']),
        tool_choice=dict(type='function', name='project.read_project_record'),
        parallel_tool_calls=False, input=[])
    definitions = [copy.deepcopy(rows['server_function']['tools'][1])]
    rows['additional'] = dict(rows['loaded_function'], tools=[], input=[
        dict(type='additional_tools', role='developer', tools=definitions),
        dict(role='user', content='Read the first 3 lines of docs/MODEL.md '
            'using project.read_project_record.')])
    rows['follow'] = dict(original['follow'], max_output_tokens=256)
    return rows


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/tool_discovery_acceptance.py'] = sha(Path(__file__))
    plan['mutations'] = ['own stored Responses/conversation only',
                         'four bounded read-only executions of docs/MODEL.md lines1-3']
    plan['derived_requests'] = [
        'server search loads the real declared namespace before function/custom generation',
        'client search returns a real call_id; application supplies new definitions outside its catalog',
        'additional_tools supplies the same actual document reader without an initial catalog',
        'all prior/call/conversation IDs come from real outputs; no retries or model restart',
        'one invalid result rejected before generation; all four valid answers use actual file text',
        'JSON/SSE, SDK item/event shapes, stored projections and shared output/native budgets']
    plan['model_output_capacity'] = Stage.max_output_tokens
    save(path, plan)


def completed_call(response, custom=False):
    background.check_response(response, 'completed')
    kind = 'custom_tool_call' if custom else 'function_call'
    calls = [item for item in response['output'] if item['type'] == kind]
    require(len(calls) == 1 and calls[0]['namespace'] == 'project' and
            calls[0]['name'] == ('raw' if custom else 'read_project_record'),
            'wrong dynamically loaded call')
    if not custom:
        require(calls[0]['status'] == 'completed', 'unfinished function call')
    return calls[0]


def search_items(response, *, server, definitions=None):
    background.check_response(response, 'completed')
    calls = [x for x in response['output'] if x['type'] == 'tool_search_call']
    results = [x for x in response['output'] if x['type'] == 'tool_search_output']
    require(len(calls) == 1 and calls[0]['status'] == 'completed' and
            calls[0]['execution'] == ('server' if server else 'client'),
            'search call execution/status mismatch')
    call = calls[0]
    require(call['arguments'] == (dict(paths=['project']) if server else
                                 dict(query='project')), 'search arguments differ')
    if server:
        require(call['call_id'] is None and len(results) == 1,
                'hosted search did not pair its output')
        result = results[0]
        require(result['execution'] == 'server' and result['call_id'] is None and
                result['status'] == 'completed' and result['tools'] == definitions and
                response['output'].index(call) < response['output'].index(result),
                'hosted search returned wrong definitions or order')
    else:
        require(isinstance(call['call_id'], str) and call['call_id'] and not results,
                'client search executed before receiving an actual result')
    return call


def streamed_search(events, response):
    require([e['sequence_number'] for e in events] == list(range(len(events))),
            'SSE sequence gap')
    for item in response['output']:
        if item['type'] not in ('tool_search_call', 'tool_search_output'):
            continue
        own = [e for e in events if e.get('item', {}).get('id') == item['id']]
        require([e['type'] for e in own] ==
                ['response.output_item.added', 'response.output_item.done'] and
                own[-1]['item'] == item, 'search item lifecycle differs')
        require(not any(e.get('item_id') == item['id'] and
                        e['type'].startswith('response.function_call_arguments.')
                        for e in events), 'private function argument events exposed')
    loaded = completed_call(response, custom=True)
    loaded_index = response['output'].index(loaded)
    result_index = next(i for i, x in enumerate(response['output'])
                        if x['type'] == 'tool_search_output')
    require(result_index < loaded_index, 'loaded custom executed before discovery')


class Stage(identity.Stage):
    limits = LIMITS
    max_output_tokens = 256
    request_templates = staticmethod(requests)
    success_status = 'PASS_TOOL_DISCOVERY_BUSINESS'

    def sdk(self, label, action, request=None, **kwargs):
        value = super().sdk(label, action, request, **kwargs)
        if value and value.get('object') == 'response':
            if action == 'create':
                require(value['usage']['output_tokens'] <= request['max_output_tokens'],
                        'continuation replenished output budget')
            wire = (self.out / (label + '.wire')).read_text()
            require('q4t_builtin_' not in wire, 'private search adapter exposed')
        return value

    def projection(self, label, response):
        actual = self.sdk(label, 'retrieve', response_id=response['id'])
        expected = copy.deepcopy(response)
        for item in expected['output']:
            for part in item.get('content', []):
                part.pop('logprobs', None)
        require(actual == expected, 'stored projection differs')

    def finish(self, label, response, call, *, conversation=None, reject=False,
               stream=False):
        rows = self.plan['requests']
        follow = copy.deepcopy(rows['follow'])
        follow['conversation' if conversation else 'previous_response_id'] = (
            conversation or response['id'])
        follow.update(stream=stream, input=[dict(type=call['type'] + '_output',
            call_id=call['call_id'], output='{"ok":false,"source":{}}')])
        if stream:
            follow['stream_options'] = dict(include_obfuscation=False)
        if reject:
            self.sdk('invalid-result', 'create', follow, expected_status=400)
            rejected = read(self.out / 'invalid-result.sdk.json')['error']
            require(rejected['code'] == 'invalid_tool_output' and
                    rejected['param'].endswith('.output'), 'loaded output schema bypassed')
        result = self.result(label, call)
        follow['input'][0]['output'] = json.dumps(result, ensure_ascii=False)
        completed = self.answer(label + '-answer', follow, result)
        self.projection(label + '-get', completed)
        return completed

    def business(self):
        caps = read(self.out / 'capabilities.json')['responses']['tool_discovery']
        require(caps['profile'] == 'q4t-tool-discovery-v1', 'missing discovery capability')
        rows = self.plan['requests']
        for custom in (False, True):
            label = 'server-custom' if custom else 'server-function'
            request = copy.deepcopy(rows['server_custom' if custom else 'server_function'])
            conversation = None
            if custom:
                conversation = self.sdk('conversation', 'conversation_create', {})['id']
                request['conversation'] = conversation
            response = self.sdk(label, 'create', request)
            search_items(response, server=True, definitions=[request['tools'][1]])
            call = completed_call(response, custom)
            require(response['output'].index(call) > next(
                i for i, x in enumerate(response['output'])
                if x['type'] == 'tool_search_output'), 'call preceded loaded definition')
            if custom:
                streamed_search(read(self.out / (label + '.sdk.json'))['chunks'], response)
            self.finish(label, response, call, conversation=conversation,
                        reject=not custom, stream=custom)
            if conversation:
                self.sdk('conversation-items', 'conversation_items',
                         response_id=conversation, query=dict(order='asc', limit=100))
        response = self.sdk('client-search', 'create', rows['client_search'])
        search = search_items(response, server=False)
        self.projection('client-search-get', response)
        request = copy.deepcopy(rows['loaded_function'])
        definitions = [copy.deepcopy(rows['server_function']['tools'][1])]
        request.update(previous_response_id=response['id'], input=[
            dict(type='tool_search_output', execution='client', status='completed',
                 call_id=search['call_id'], tools=definitions),
            dict(role='user', content='Read the first 3 lines of docs/MODEL.md '
                 'with the newly loaded project reader.')])
        response = self.sdk('client-loaded', 'create', request)
        completed = self.finish('client-function', response, completed_call(response))
        self.sdk('client-inputs', 'input_items', response_id=completed['id'],
                 query=dict(order='asc', limit=100))
        response = self.sdk('additional-tools', 'create', rows['additional'])
        self.finish('additional-function', response, completed_call(response))
        self.drained('final')
        save(self.out / 'business-summary.json', dict(model_starts=1,
            create_requests=10, successful_responses=9, rejected_before_generation=1,
            actual_read_executions=4, server_discoveries=2, client_discoveries=1,
            additional_tool_activations=1, output_capacity=256,
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
