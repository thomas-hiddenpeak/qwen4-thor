"""Actual native moderation: 1 start/10 requests/28 model steps/900 seconds."""
import argparse
import copy
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import acceptance as foreground
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import cache_acceptance as cache
from tools.serve_responses import compaction_acceptance as compact
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import reasoning_acceptance as pro
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=10, controls=2000, seconds=900)
STEPS = dict(score=3, input_block=1, output_block=3, combined=6,
             background=4, tool=3, follow=3, score_failure=2, prewarm=2, chat=1)
MAX_MODEL_STEPS = sum(STEPS.values())
LIMITED_CLIENT = 'moderation-limited'
CATEGORIES = ('harassment', 'harassment/threatening', 'hate', 'hate/threatening',
    'illicit', 'illicit/violent', 'self-harm', 'self-harm/instructions',
    'self-harm/intent', 'sexual', 'sexual/minors', 'violence', 'violence/graphic')


def policy(input='score', output='score'):
    return dict(model=foreground.MODEL,
                policy=dict(input=dict(mode=input), output=dict(mode=output)))


def requests():
    old = foreground.requests()
    common = dict(model=foreground.MODEL, store=True, temperature=0,
                  max_output_tokens=384, reasoning=dict(effort='none'))
    score = dict(common, input=old['plain']['input'], moderation=policy(),
        text=dict(format=dict(type='json_schema', name='sum', strict=True,
            schema=foreground.base.closed(dict(answer=dict(type='integer', const=42))))))
    blocked_input = dict(common, input='I am going to kill you tonight with a '
        'knife. I am waiting outside your home.', moderation=policy('block'),
        stream=True, stream_options=dict(include_obfuscation=False))
    # The schema is request metadata, deliberately excluded from input review.
    # Only the actual model-generated value can produce an output score.
    blocked_output = copy.deepcopy(score)
    blocked_output.update(input='Return the requested object.',
        moderation=policy(output='block'), stream=True,
        stream_options=dict(include_obfuscation=False))
    blocked_output['text']['format']['schema']['properties']['answer'] = dict(
        type='string', const='I am going to kill you tonight.')
    combined = dict(common, input=compact.history(), max_output_tokens=512,
        moderation=policy('block', 'block'), stream=True,
        stream_options=dict(include_obfuscation=False),
        reasoning=dict(mode='pro', effort='low', summary='concise'),
        context_management=[dict(type='compaction', compact_threshold=128)])
    stored = dict(score, background=True, reasoning=dict(mode='pro', effort='none'),
                  moderation=policy('block', 'block'),
                  include=['reasoning.encrypted_content'])
    tool = dict(old['tool'], **common, moderation=policy('block', 'block'))
    follow = dict(old['follow'], **common, moderation=policy('block', 'block'))
    prewarm = cache.requests()['prewarm']
    prewarm['moderation'] = policy('block', 'block')
    return dict(score=score, input_block=blocked_input, output_block=blocked_output,
        combined=combined, background=stored, tool=tool, follow=follow,
        score_failure=copy.deepcopy(score), prewarm=prewarm, chat=old['chat'])


def check_review(value, *, steps=2, blocked=None, output_error=None):
    background.check_response(value, 'incomplete' if blocked else 'completed')
    usage = value['q4t_moderation_usage']
    require(set(usage) == {'input_tokens', 'output_tokens', 'total_tokens', 'model_steps'}
            and usage['model_steps'] == steps and usage['input_tokens'] > 0
            and 0 < usage['output_tokens'] <= steps * 512
            and usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens'],
            'actual separate classifier accounting')
    require(value['usage']['output_tokens'] <= value['max_output_tokens'],
            'moderation refilled public output budget')
    for direction in ('input', 'output'):
        report = value['moderation'][direction]
        if (direction == 'output' and (blocked == 'input' or output_error)):
            require(report['type'] == 'error' and report['code'] ==
                    (output_error or 'moderation_not_executed'), 'missing actual review error')
            continue
        require(report['type'] == 'moderation_result' and report['model'] == foreground.MODEL,
                'actual model identity/result absent')
        require(all(set(report[key]) == set(CATEGORIES) for key in
                    ('categories', 'category_scores', 'category_applied_input_types')),
                'partial or fabricated category map')
        flags = []
        for name in CATEGORIES:
            score = report['category_scores'][name]
            require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
                    'invalid actual score')
            require(report['category_applied_input_types'][name] == ['text'], 'modality claim')
            require(report['categories'][name] == (score >= .5), 'threshold mismatch')
            flags.append(score >= .5)
        require(report['flagged'] == any(flags), 'inconsistent flagged decision')
        require(report['flagged'] == (direction == blocked),
                'bounded benign/threat fixture classification failed')
    if blocked:
        require(value['output'] == [] and
                value['incomplete_details']['reason'] == 'content_filter',
                'blocked output escaped or completed')
        if blocked == 'input':
            require(value['usage']['total_tokens'] == 0, 'blocked input generated a business answer')
        else:
            require(value['usage']['output_tokens'] > 0, 'output block did not inspect real output')


def check_blocked_events(value, events):
    require(events and events[-1]['type'] == 'response.incomplete' and
            events[-1]['response'] == value, 'filtered SSE terminal differs')
    require([event['sequence_number'] for event in events] == list(range(len(events))),
            'private events left public sequence gaps')
    require(not any(event['type'].endswith('.delta') or
                    event['type'].startswith('response.output_item.') for event in events),
            'unreviewed generation leaked through SSE')


def check_execution(log, values, chat):
    for name, value in values.items():
        summary.check_steps(log, value, STEPS[name])
    summary.check_steps(log, chat, 1)
    require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == MAX_MODEL_STEPS,
            'extra model step, replay, or missing classification')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve() / 'plan.json'
    plan = read(path)
    for name in ('tools/serve_responses/moderation_acceptance.py',
                 'tools/serve_responses/test_moderation_acceptance.py'):
        plan['sources'][name] = sha(ROOT / name)
    plan['planned'] = dict(starts=1, chats=10, maximum_model_steps=MAX_MODEL_STEPS,
                           actual_tool_executions=1, prefill_only=1)
    plan['mutations'] = ['one owned model and isolated two-client/object/cache state',
                         'no restart or changes to default/other services']
    plan['derived_requests'] = [
        'real docs/MODEL.md read, returned tool ID and result in followup',
        'background GET samples while output is held, original encrypted result afterwards',
        'limited client uses actual first classifier input usage and two native tokenizer '
        'preflights to select harmless padding near its fixed cumulative input quota; '
        'its output review must fail without losing the actual answer or restarting model',
        'prewarm executes only input review and prefill, no output classification',
        'all actual model tokens including classifier are compared to metrics and per-client settled TPM',
        'actual sampling step count must be 28; no model or tool replay, no performance claim']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_MODERATION_BUSINESS'
    requests_per_minute = 16
    tokens_per_minute = 131072

    def client_policy(self):
        value = super().client_policy()
        value['clients'].append(dict(value['clients'][0], id=LIMITED_CLIENT,
                                     max_prompt_tokens=4096))
        return value

    def limited_request(self, first):
        row = requests()['score_failure']
        # This only chooses a resource-boundary fixture. Actual classification
        # success/error and actual token accounting remain independent oracles.
        overhead = first['q4t_moderation_usage']['input_tokens'] // 2
        target = (4096 - 5 * overhead // 4) // 2
        require(1000 <= target <= 1800, 'unexpected classifier context; do not guess quota')
        padding = target
        for attempt in range(2):
            row['input'] = 'Neutral reference notes: ' + 'sample ' * padding + '\n' + first['input_fixture']
            request = dict(model=foreground.MODEL,
                messages=[dict(role='user', content=row['input'])],
                max_completion_tokens=384, enable_thinking=False,
                response_format=dict(type='json_schema', json_schema=row['text']['format']))
            request['response_format']['json_schema'] = {
                key: value for key, value in row['text']['format'].items() if key != 'type'}
            self.budget('controls')
            status, raw = self.http('/v1/chat/preflight', dict(request=request),
                                    label='limited-preflight-' + str(attempt))
            require(status == 200, 'limited fixture tokenizer preflight')
            measured = json.loads(raw)['prompt_tokens']
            if attempt == 0:
                padding += target - measured
                require(padding > 0, 'padding correction out of range')
            else:
                require(abs(measured - target) <= 8, 'unexpected tokenizer padding drift')
        save(self.out / 'limited-fixture.json', dict(target=target, measured=measured,
             source_classifier_input=first['q4t_moderation_usage']['input_tokens'],
             max_cumulative_prompt=4096))
        return row

    def accounting(self, values, chat):
        metrics = self.drained('final-accounting')
        totals = {}
        for name, value in values.items():
            client = LIMITED_CLIENT if name == 'score_failure' else encrypted.CLIENT
            row = totals.setdefault(client, dict(requests=0, input=0, output=0))
            row['requests'] += 1
            row['input'] += value['usage']['input_tokens'] + value['q4t_moderation_usage']['input_tokens']
            row['output'] += value['usage']['output_tokens'] + value['q4t_moderation_usage']['output_tokens']
        row = totals[encrypted.CLIENT]
        row['requests'] += 1
        row['input'] += chat['usage']['prompt_tokens']
        row['output'] += chat['usage']['completion_tokens']
        for client, row in totals.items():
            suffix = '{client="' + client + '"}'
            require(metrics['q4t_client_rate_admitted_total' + suffix] == row['requests'] and
                    metrics['q4t_client_rate_reserved_tokens' + suffix] == 0 and
                    metrics['q4t_client_rate_settled_tokens_total' + suffix] ==
                    row['input'] + row['output'], 'classifier/client TPM or RPM mismatch')
        require(metrics['q4t_requests_total'] == 10 and
                metrics['q4t_requests_success_total'] == 10 and
                metrics['q4t_requests_error_total'] == 0, 'request/error accounting')
        require(metrics['q4t_prompt_tokens_total'] == sum(v['input'] for v in totals.values()) and
                metrics['q4t_generation_tokens_total'] == sum(v['output'] for v in totals.values()),
                'global actual input/output accounting')
        save(self.out / 'accounting.json', totals)

    def business(self):
        generation = self.counts['starts']
        caps = read(self.out / f'capabilities-{generation}.json')['responses']['moderation']
        require(caps['classifier_available'] and caps['block_output_before_publication']
                and caps['same_client_tpm'], 'actual native moderation capability')
        rows, values = requests(), {}
        for name in ('score', 'input_block', 'output_block', 'combined'):
            value = self.sdk(name, 'create', rows[name])
            blocked = name.split('_')[0] if name.endswith('_block') else None
            check_review(value, steps=1 if blocked == 'input' else 2, blocked=blocked)
            if blocked:
                check_blocked_events(value, read(self.out / (name + '.sdk.json'))['chunks'])
            elif name == 'score':
                require(json.loads(foreground.text(value)) == dict(answer=42), 'actual JSON answer')
            else:
                pro.check_pro(value, answer='42')
                summary.check_summary(value, arithmetic=True)
                compact.compact_item(value)
            values[name] = value
        queued = self.sdk('background-create', 'create', rows['background'], drain=False)
        require(queued['status'] in ('queued', 'in_progress') and not queued['output'],
                'background did not expose a real pre-approval observation')
        terminal = background.Stage.wait_response(self, 'background-completed', queued['id'],
                          lambda v: v['status'] == 'completed')
        for sample in read(self.out / 'background-completed.poll.json'):
            if sample['output']:
                require(sample.get('moderation', {}).get('output', {}).get('flagged') is False,
                        'GET exposed output before approval')
        stored = self.sdk('background', 'retrieve', response_id=terminal['id'],
                          query=dict(include=['reasoning.encrypted_content']))
        check_review(stored)
        pro.check_pro(stored, answer=dict(answer=42))
        encrypted.encrypted_items(stored)
        values['background'] = stored
        tool = self.sdk('tool', 'create', rows['tool'])
        check_review(tool)
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == 'read_project_record', 'approved completed actual tool')
        arguments = json.loads(calls[0]['arguments'])
        require(arguments == dict(path=foreground.DOCUMENT, start_line=1, line_count=3),
                'actual document arguments')
        result = foreground.base.ProjectRecords(ROOT).read(arguments)
        require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources'][foreground.DOCUMENT], 'document source changed')
        save(self.out / 'tool-execution.json', dict(call=calls[0], result=result))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=json.dumps(result, ensure_ascii=False))])
        final = self.sdk('follow', 'create', rows['follow'])
        check_review(final)
        answer = json.loads(foreground.text(final))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and
                answer['citation'] == result['source']['citation'], 'actual tool business closure')
        values.update(tool=tool, follow=final)
        limited = self.limited_request(dict(values['score'], input_fixture=rows['score']['input']))
        failure = self.sdk('score-failure', 'create', limited, headers=dict(
            encrypted.HEADERS, **{'X-Q4T-Client-Id': LIMITED_CLIENT}))
        check_review(failure, steps=1, output_error='client_prompt_limit_exceeded')
        require(json.loads(foreground.text(failure)) == dict(answer=42),
                'score failure lost healthy business answer')
        values['score_failure'] = failure
        prewarm = self.sdk('prewarm', 'create', rows['prewarm'])
        check_review(prewarm, steps=1, output_error='moderation_not_executed')
        cache.cache_usage(prewarm, prewarm=True, mode='write')
        values['prewarm'] = prewarm
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['finish_reason'] == 'stop' and
                chat['choices'][0]['message']['content'].strip() == '42', 'same-PID Chat failed')
        self.accounting(values, chat)
        log = (self.out / f'model-{generation}.log').read_text()
        check_execution(log, values, chat)
        cache.check_writes(log, prewarm)
        require('[mtp-accept] request=' + chat['id'] in log, 'ordinary MTP path absent')
        save(self.out / 'business-checks.json', dict(external_requests=10,
            actual_model_steps=MAX_MODEL_STEPS, actual_tool_executions=1,
            real_classification_and_blocking=True, private_output_before_approval=True,
            score_error_same_model_continuation=True, original_answer_budget=True,
            separate_usage_and_exact_tpm=True, default_unchanged=True,
            performance_claim=False, calibrated_moderation_quality_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18993)
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
