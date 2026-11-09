"""Standard input counting: 1 start, 4 generations, <=16 counts, 900 seconds."""
import argparse
import copy
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=4, controls=2000, seconds=900)
MAX_COUNTS = 16
FIELDS = ('conversation', 'input', 'instructions', 'model', 'parallel_tool_calls',
          'personality', 'previous_response_id', 'reasoning', 'text',
          'tool_choice', 'tools', 'truncation')
OTHER = 'tokens-other'


def requests():
    old = foreground.requests()
    return dict(
        initial=dict(old['plain'], store=True, stream=True,
            stream_options=dict(include_obfuscation=False),
            personality='friendly', reasoning=dict(effort='low'),
            include=['reasoning.encrypted_content'], max_output_tokens=192),
        tool=dict(old['tool'], store=True),
        follow=dict(old['follow'], store=True, personality='pragmatic'),
        chat=old['chat'])


def count_request(request):
    return {name: copy.deepcopy(request[name]) for name in FIELDS if name in request}


def count_value(value):
    require(not errors(value, 'input_tokens_response') and set(value) ==
            {'object', 'input_tokens'} and type(value['input_tokens']) is int and
            value['input_tokens'] > 0, 'invalid actual input count')
    return value['input_tokens']


def check_count_usage(count, response):
    background.check_response(response, 'completed')
    require(count == response['usage']['input_tokens'],
            'count does not match actual generation prompt tokens')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in ('tools/serve_responses/input_tokens_acceptance.py',
                 'tools/serve_responses/test_input_tokens_acceptance.py'):
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=1, chats=4, model_steps=4,
        maximum_input_counts=MAX_COUNTS, actual_tool_executions=1)
    plan['derived_requests'] = [
        'count SDK projection uses only standard count fields from each actual generation',
        'previous/opaque/manual histories use actual generated records and encrypted output',
        'tool follow uses actual tool ID and real docs/MODEL.md lines1-3',
        'conversation contains real current request input; count reads must not append items',
        'count errors/empty bodies never run generation or consume generation RPM/TPM',
        'all own client/state/artifacts isolated; default package remains protected']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_INPUT_TOKENS_BUSINESS'

    def client_policy(self):
        value = super().client_policy()
        value['clients'].append(dict(value['clients'][0], id=OTHER))
        return value

    def count(self, label, request=None, *, headers=None, expected_status=None,
              omit_request=False):
        self.input_counts += 1
        require(self.input_counts <= MAX_COUNTS, 'input count budget exhausted')
        value = self.sdk(label, 'input_tokens', request, headers=headers,
            expected_status=expected_status, omit_request=omit_request)
        if expected_status is not None:
            return None
        return count_value(value)

    def business(self):
        self.input_counts = 0
        rows, values = requests(), []
        caps = read(self.out/'capabilities-1.json')['responses']['input_tokens']
        require(caps['enabled'] and not caps['model_execution'] and
                not caps['state_mutation'] and not caps['generation_rpm_tpm'],
                'actual count capability differs')
        empty = self.count('empty-omitted', omit_request=True)
        require(self.count('empty-null') == empty and
                self.count('empty-object', {}) == empty, 'empty SDK body parity')

        initial_count = self.count('initial-count', count_request(rows['initial']))
        first = self.sdk('initial', 'create', rows['initial'])
        check_count_usage(initial_count, first)
        require(foreground.text(first).strip() == '42' and
                first['personality'] == 'friendly', 'actual styled SSE arithmetic')
        encrypted.encrypted_items(first)
        values.append(first)
        follow_count = dict(model=foreground.MODEL, reasoning=dict(effort='none'),
                            input='Repeat the previous answer.', previous_response_id=first['id'])
        stored_count = self.count('stored-count', follow_count)
        opaque = dict(follow_count)
        opaque.pop('previous_response_id')
        opaque['input'] = encrypted.opaque_output(first) + [
            dict(role='user', content=follow_count['input'])]
        require(self.count('opaque-count', opaque) == stored_count,
                'encrypted restored context differs from stored history')
        self.count('foreign-previous', follow_count, expected_status=404,
            headers=dict(encrypted.HEADERS, **{'X-Q4T-Client-Id': OTHER}))

        tool_count = self.count('tool-count', count_request(rows['tool']))
        tool = self.sdk('tool', 'create', rows['tool'])
        check_count_usage(tool_count, tool)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'completed real tool required')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual tool arguments differ')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT],
                'real project record source changed')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final_count = self.count('follow-count', count_request(rows['follow']))
        manual = count_request(rows['follow'])
        manual.pop('previous_response_id')
        manual['input'] = [dict(role='user', content=rows['tool']['input'])] + \
            copy.deepcopy(tool['output']) + copy.deepcopy(rows['follow']['input'])
        require(self.count('manual-count', manual) == final_count,
                'manual and stored actual tool history differ')
        final = self.sdk('follow', 'create', rows['follow'])
        check_count_usage(final_count, final)
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'real document closure')
        values += [tool, final]

        # Only this explicit API creates a conversation; count itself is read-only.
        self.budget('controls')
        items = [dict(role='user', content=rows['initial']['input'])]
        status, raw = self.http('/v1/conversations', dict(items=items), label='conversation-create')
        require(status == 200, 'conversation creation failed')
        conversation = json.loads(raw)['id']
        status, before = self.http('/v1/conversations/'+conversation+'/items', label='conversation-before')
        require(status == 200, 'conversation input snapshot unavailable')
        request = dict(model=foreground.MODEL, conversation=conversation,
                       input='Continue this calculation.', reasoning=dict(effort='none'))
        conversation_count = self.count('conversation-count', request)
        request.pop('conversation')
        request['input'] = items + [dict(role='user', content=request['input'])]
        require(self.count('conversation-manual-count', request) == conversation_count,
                'conversation context omitted')
        status, after = self.http('/v1/conversations/'+conversation+'/items', label='conversation-after')
        require(status == 200 and before == after, 'count mutated conversation')
        require(self.count('null-model-count', dict(count_request(rows['initial']), model=None))
                == initial_count, 'null model default changes count')
        self.count('bad-field', dict(input='hi', store=True), expected_status=400)
        self.count('unknown-model', dict(model='not-served', input='hi'), expected_status=404)
        # Method dispatch must not confuse the static endpoint with a response ID.
        status, _ = self.http('/v1/responses/input_tokens', label='get-count-rejected')
        require(status == 405, 'count endpoint routed as stored response identity')

        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary same-model Chat failed')
        self.accounting('final-accounting', values, chat=chat)
        log = (self.out/'model-1.log').read_text()
        for value in [*values, chat]:
            summary.check_steps(log, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == 4,
                'count executed model work or generation repeated')
        require('[mtp-accept] request='+chat['id'] in log, 'ordinary MTP path absent')
        save(self.out/'business-checks.json', dict(generations=4, model_steps=4,
            input_counts=self.input_counts, real_tool_executions=1,
            count_matches_actual_usage=True, exact_encrypted_history=True,
            count_mutations=False, generation_rpm_tpm_unchanged_by_counts=True,
            default_unchanged=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=19003)
    command = sub.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
