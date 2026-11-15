"""One start/eight creates: real programmatic project reads and final answers."""
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

foreground, base = identity.foreground, identity.base
LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)


def requests():
    original = identity.requests()
    rows = {}
    code = {
        'function': 'const r = await tools.project.read_project_record('
            '{path:"docs/MODEL.md",start_line:1,line_count:3}); text(r);',
        'custom': 'text(await tools.project.raw("read docs/MODEL.md"));'}
    for name, key in [('function', 'tool'), ('custom', 'custom')]:
        row = copy.deepcopy(original[key])
        namespace = row['tools'][0]
        for tool in namespace['tools']:
            tool['allowed_callers'] = ['programmatic']
        row.update(tools=[dict(type='programmatic_tool_calling'), namespace],
            max_output_tokens=256, max_tool_calls=1,
            tool_choice=dict(type='programmatic_tool_calling'),
            input='Use a JavaScript program to read the real project document. '
                'Run this code exactly, without inventing its result: ' + code[name],
            stream=name == 'custom', parallel_tool_calls=False)
        if not row['stream']:
            row.pop('stream_options', None)
        rows[name] = row
    rows['follow'] = dict(original['follow'], max_output_tokens=256, max_tool_calls=1)
    rows['local'] = dict(rows['function'], tools=[dict(type='programmatic_tool_calling')],
        input='Run exactly this JavaScript program: text(6*7); '
              'After execution, answer only with the resulting decimal number.')
    rows['truncated'] = dict(rows['function'], max_output_tokens=1)
    rows['plain'] = dict(foreground.requests()['plain'], max_output_tokens=64)
    return rows


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/programmatic_acceptance.py'] = sha(Path(__file__))
    plan['mutations'] = ['own Responses/conversation state only',
        'two read-only executions of docs/MODEL.md lines1-3']
    plan['derived_requests'] = [
        'program/call IDs, fingerprints and caller come from actual model outputs',
        'functions and grammar custom tools execute only as program children',
        'function result schema rejects one invalid result before generation',
        'actual tool result resumes the program and feeds a constrained title/citation answer',
        'custom uses a real conversation and SSE; local code continues without an external call',
        'one-token code truncation dispatches nothing; final ordinary request stays healthy',
        'no retries, injected model restart, full performance or full quality matrix']
    plan['model_output_capacity'] = Stage.max_output_tokens
    save(path, plan)


def program_calls(response, *, custom=False, namespace='project', name=None):
    background.check_response(response, 'completed')
    programs = [x for x in response['output'] if x['type'] == 'program']
    calls = [x for x in response['output'] if x['type'] in ('function_call', 'custom_tool_call')]
    require(len(programs) == 1 and len(calls) == 1, 'wrong program/child count')
    program, call = programs[0], calls[0]
    require(program.get('code') and program.get('fingerprint') and
            'status' not in program and call.get('caller') ==
            dict(type='program', caller_id=program['call_id']), 'program authority/caller differs')
    require(call['type'] == ('custom_tool_call' if custom else 'function_call') and
            call['name'] == (name or ('raw' if custom else 'read_project_record')) and
            call.get('namespace') == namespace and
            ('status' not in call if custom else call.get('status') == 'completed'),
            'wrong authorized program child')
    require(response['output'].index(program) < response['output'].index(call) and
            not any(x['type'] == 'program_output' for x in response['output']),
            'program completed before receiving real results')
    return program, call


def stored_projection(response):
    expected = copy.deepcopy(response)
    for item in expected['output']:
        for part in item.get('content', []):
            part.pop('logprobs', None)
    return expected


def program_result(response, parent, expected):
    background.check_response(response, 'completed')
    outputs = [x for x in response['output'] if x['type'] == 'program_output']
    require(len(outputs) == 1 and outputs[0]['call_id'] == parent['call_id'] and
            outputs[0]['status'] == 'completed', 'program completion identity differs')
    require(json.loads(outputs[0]['result']) == expected, 'program did not use actual tool data')
    require(not any(x['type'] in ('program', 'function_call', 'custom_tool_call')
                    for x in response['output']), 'completed child dispatched again')


class Stage(identity.Stage):
    limits = LIMITS
    max_output_tokens = 256
    request_templates = staticmethod(requests)
    success_status = 'PASS_PROGRAMMATIC_TOOLS_BUSINESS'

    def business(self):
        capability = read(self.out / 'capabilities.json')['responses']['programmatic_tools']
        require(capability['profile'] == 'q4t-programmatic-tools-v1' and
                capability['authenticated_continuation'], 'program capability missing')
        rows = self.plan['requests']
        for custom in (False, True):
            name = 'custom' if custom else 'function'
            request = copy.deepcopy(rows[name])
            conversation = None
            if custom:
                conversation = self.sdk('conversation', 'conversation_create', {})['id']
                request['conversation'] = conversation
            response = self.sdk(name, 'create', request)
            parent, call = program_calls(response, custom=custom)
            result = self.result(name, call)
            follow = copy.deepcopy(rows['follow'])
            follow['conversation' if custom else 'previous_response_id'] = (
                conversation if custom else response['id'])
            follow['input'] = [dict(type=call['type'] + '_output',
                call_id=call['call_id'], caller=call['caller'], output='{"ok":false,"source":{}}')]
            if not custom:
                self.sdk('invalid-result', 'create', follow, expected_status=400)
                error = read(self.out / 'invalid-result.sdk.json')['error']
                require(error['code'] == 'invalid_tool_output', 'original output schema was lost')
            follow['input'][0]['output'] = json.dumps(result, ensure_ascii=False)
            follow['stream'] = custom
            if custom:
                follow['stream_options'] = dict(include_obfuscation=False)
            completed = self.answer(name + '-answer', follow, result)
            program_result(completed, parent, result)
            stored = self.sdk(name + '-get', 'retrieve', response_id=completed['id'])
            require(stored == stored_projection(completed),
                    'durable program completion differs')
        local = self.sdk('local-program', 'create', rows['local'])
        background.check_response(local, 'completed')
        programs = [x for x in local['output'] if x['type'] == 'program']
        require(len(programs) == 1, 'local program was not executed')
        results = [x for x in local['output'] if x['type'] == 'program_output']
        require(len(results) == 1 and results[0]['result'] == '42' and
                results[0]['status'] == 'completed' and
                foreground.text(local).strip() == '42', 'local program result did not reach model')
        require(not any(x['type'] in ('function_call', 'custom_tool_call')
                        for x in local['output']), 'local program invoked an external tool')
        truncated = self.sdk('truncated', 'create', rows['truncated'])
        background.check_response(truncated, 'incomplete')
        require(not any(x['type'] in ('program', 'program_output', 'function_call', 'custom_tool_call')
                        for x in truncated['output']), 'truncated code was executed/published')
        ordinary = self.sdk('ordinary-after-program', 'create', rows['plain'])
        background.check_response(ordinary, 'completed')
        require(foreground.text(ordinary).strip() == '42', 'ordinary service no longer healthy')
        self.drained('final')
        save(self.out / 'business-summary.json', dict(model_starts=1,
            create_requests=8, actual_read_executions=2, invalid_results_rejected=1,
            truncated_programs_not_dispatched=1, programs_completed=3,
            full_quality_or_performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18170)
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
