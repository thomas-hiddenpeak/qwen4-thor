"""Native access programs: 1 start, 8 generations, 5 denials, 900 seconds."""
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
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=13, controls=2000, seconds=900)
MAX_MODEL_STEPS = 8
OTHER = 'access-standard'
LABEL = 'local_review_57'


def policy():
    return dict(version=1, authority='q4t-local', model=foreground.MODEL,
        cyber_tier='blue', minimum_grant='red', default_selection='model_tier',
        standard_allowed=True, program_instructions=
        'Use this local authorized defensive review profile. The local review '
        'policy label is '+LABEL+'. When asked for that label, return its exact '
        'value. For project questions use actual tool evidence and exact citations. '
        'Do not claim cloud authorization or any unperformed operation.',
        clients=[dict(id=encrypted.CLIENT, grant='red'), dict(id=OTHER, grant='blue')])


def requests():
    old = foreground.requests()
    implicit = dict(model=foreground.MODEL, store=True, temperature=0,
        max_output_tokens=192, reasoning=dict(effort='none'),
        input='Return a JSON object with the local review policy label from '
        'your system instructions, using the key label.',
        text=dict(format=dict(type='json_schema', name='policy_label', strict=True,
            schema=foreground.base.closed(dict(label=dict(type='string'))))))
    explicit = dict(implicit, access_programs=dict(cyber='daybreak_blue'),
        stream=True, stream_options=dict(include_obfuscation=False))
    return dict(implicit=implicit, explicit=explicit,
        standard=dict(old['plain'], store=True, access_programs=dict(cyber='standard')),
        fallback=dict(old['plain'], store=True), tool=dict(old['tool'], store=True),
        follow=dict(old['follow'], store=True),
        background=dict(explicit, background=True), chat=old['chat'])


def negatives():
    row = requests()['standard']
    return [
        ('denied', dict(row, access_programs=dict(cyber='daybreak_blue')), OTHER,
         403, 'access_program_not_enabled'),
        ('wrong-tier', dict(row, access_programs=dict(cyber='daybreak_red')),
         encrypted.CLIENT, 400, 'invalid_access_program'),
        ('null', dict(row, access_programs=None), encrypted.CLIENT, 400, 'invalid_request'),
        ('unknown', dict(row, access_programs=dict(cyber='standard', grant='red')),
         encrypted.CLIENT, 400, 'invalid_request'),
        ('forged', dict(row, access_programs=dict(cyber='daybreak_blue'),
                        metadata=dict(client=encrypted.CLIENT, grant='red')),
         OTHER, 403, 'access_program_not_enabled')]


def check_selection(value, digest, selected, *, label=False):
    background.check_response(value, 'completed')
    require(value['access_programs'] == (dict(cyber=selected) if selected else None),
            'resolved selection differs')
    require(value['q4t_access_program'] == dict(authority='q4t-local',
        policy_sha256=digest, backend_model=foreground.MODEL,
        treatment='operator_system_instructions' if selected == 'daybreak_blue'
                  else 'standard'), 'native policy identity differs')
    if label:
        require(json.loads(foreground.text(value)) == dict(label=LABEL),
                'model did not use actual trusted policy context')


def check_steps(log, values, chat):
    for value in [*values.values(), chat]:
        summary.check_steps(log, value, 1)
    require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == MAX_MODEL_STEPS,
            'denied request executed, missing step, or generation replayed')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in ('tools/serve_responses/access_program_acceptance.py',
                 'tools/serve_responses/test_access_program_acceptance.py'):
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=1, chats=13, positive_generations=8,
        negative_creates=5, maximum_model_steps=8, actual_tool_executions=1)
    plan['access_program_policy'] = policy()
    plan['negative_requests'] = negatives()
    plan['mutations'] = ['one owned model, two isolated test clients and object state',
        'remove only own startup policy copy after native loading, retain source evidence',
        'no default changes, no restart or interference with other services']
    plan['derived_requests'] = ['actual tool ID and docs/MODEL.md lines1-3 in continuation',
        'exact original stored background SSE replay, no second generation',
        'policy label only in trusted configuration, never in request/schema',
        '5 negative creates must consume zero model steps and zero admitted RPM/TPM',
        'sum actual per-client and global usage; ordinary MTP Chat on same PID']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_ACCESS_PROGRAM_BUSINESS'
    requests_per_minute = 16
    tokens_per_minute = 65536

    def client_policy(self):
        value = super().client_policy()
        value['clients'].append(dict(value['clients'][0], id=OTHER))
        return value

    def extra_model_args(self):
        path = self.out/'access-program-policy.json'
        save(path, self.plan['access_program_policy'])
        self.policy_digest = sha(path)
        save(self.out/'access-program-policy-source.json', self.plan['access_program_policy'])
        return ['--access-program-policy-file', str(path)]

    def accounting(self, values, chat):
        metrics = self.drained('final-accounting')
        totals = {}
        for name, value in values.items():
            client = OTHER if name == 'fallback' else encrypted.CLIENT
            row = totals.setdefault(client, dict(requests=0, input=0, output=0))
            row['requests'] += 1
            row['input'] += value['usage']['input_tokens']
            row['output'] += value['usage']['output_tokens']
        row = totals[encrypted.CLIENT]
        row['requests'] += 1
        row['input'] += chat['usage']['prompt_tokens']
        row['output'] += chat['usage']['completion_tokens']
        for client, row in totals.items():
            suffix = '{client="'+client+'"}'
            require(metrics['q4t_client_rate_admitted_total'+suffix] == row['requests'] and
                    metrics['q4t_client_rate_reserved_tokens'+suffix] == 0 and
                    metrics['q4t_client_rate_settled_tokens_total'+suffix] ==
                    row['input']+row['output'], 'per-client actual policy/RPM/TPM accounting')
        require(metrics['q4t_requests_total'] == 13 and
                metrics['q4t_requests_success_total'] == 8 and
                metrics['q4t_requests_error_total'] == 5, 'request accounting differs')
        require(metrics['q4t_prompt_tokens_total'] == sum(v['input'] for v in totals.values())
                and metrics['q4t_generation_tokens_total'] == sum(v['output'] for v in totals.values()),
                'actual policy token accounting differs')
        save(self.out/'accounting.json', totals)

    def business(self):
        generation = self.counts['starts']
        caps = read(self.out/f'capabilities-{generation}.json')['responses']['access_programs']
        require(caps['policy_configured'] and caps['policy_sha256'] == self.policy_digest
                and caps['authority'] == 'q4t-local' and not caps['cloud_approval'],
                'actual native policy capability differs')
        # The policy is already an immutable process snapshot; this own test
        # file is not a service-health dependency after startup.
        (self.out/'access-program-policy.json').unlink()
        for name, row, client, status, code in self.plan['negative_requests']:
            self.sdk(name, 'create', row, expected_status=status,
                headers=dict(encrypted.HEADERS, **{'X-Q4T-Client-Id': client}))
            result = read(self.out/(name+'.sdk.json'))
            require(result['error']['code'] == code, 'wrong access rejection code')
        rows, values = requests(), {}
        for name in ('implicit', 'explicit', 'standard', 'fallback', 'background'):
            headers = dict(encrypted.HEADERS, **{'X-Q4T-Client-Id': OTHER}) if name == 'fallback' else None
            value = self.sdk(name, 'create', rows[name], headers=headers)
            selected = None if name == 'fallback' else 'standard' if name == 'standard' else 'daybreak_blue'
            check_selection(value, self.policy_digest, selected,
                            label=name in ('implicit', 'explicit', 'background'))
            if name in ('standard', 'fallback'):
                require(foreground.text(value).strip() == '42', 'standard actual business failed')
            values[name] = value
        replay = self.sdk('background-replay', 'retrieve',
            response_id=values['background']['id'], query=dict(stream=True, include_obfuscation=False))
        require(replay == values['background'] and
                read(self.out/'background-replay.sdk.json')['chunks'] ==
                read(self.out/'background.sdk.json')['chunks'], 'original replay changed')
        original = self.sdk('original-input', 'input_items', response_id=values['implicit']['id'])
        require(LABEL not in json.dumps(original), 'trusted policy contaminated original input')
        tool = self.sdk('tool', 'create', rows['tool'])
        check_selection(tool, self.policy_digest, 'daybreak_blue')
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'actual completed tool required')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual tool arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT],
                'actual document changed')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', rows['follow'])
        check_selection(final, self.policy_digest, 'daybreak_blue')
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'real document closure')
        values.update(tool=tool, follow=final)
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary same-model Chat failed')
        self.accounting(values, chat)
        log = (self.out/f'model-{generation}.log').read_text()
        check_steps(log, values, chat)
        require('[mtp-accept] request='+chat['id'] in log, 'ordinary MTP path absent')
        save(self.out/'business-checks.json', dict(generations=8, negative_creates=5,
            actual_model_steps=8, real_tool_executions=1, actual_trusted_context=True,
            removed_policy_file_does_not_stop_model=True, original_replay=True,
            separate_model_and_client_grants=True, precise_client_accounting=True,
            default_unchanged=True, cloud_approval_claim=False, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18993)
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
