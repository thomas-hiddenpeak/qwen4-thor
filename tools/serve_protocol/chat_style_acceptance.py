"""Chat style and streaming: one model, six requests, actual project tool loop."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, require, save, sha
from chat_client import MultiCompletion

LIMITS = dict(starts=1, chats=6, controls=2000, seconds=900)
DOCUMENT = 'docs/MODEL.md'
QUESTION = 'Compute 17 + 25. Reply only with the decimal number.'


def tagged(label, body):
    return dict(body, request_id='chat-style-'+label,
                cancel_token=hashlib.sha256(label.encode()).hexdigest())


def requests():
    common = dict(model='qwen3.8-flash-next', temperature=0,
        max_completion_tokens=192, parse_response=True,
        thinking_token_budget=64, request_timeout_ms=120000,
        messages=[dict(role='user', content=QUESTION)])
    tool = dict(common, stream=True, stream_options=dict(include_usage=True),
        reasoning_effort='none', verbosity='low', max_completion_tokens=256,
        messages=[dict(role='user', content='Read the first 16 lines of docs/MODEL.md '
            'with read_project_record. Then use the actual result to return its '
            'first-line title and exact source citation.')],
        tools=[dict(type='function', function=dict(name='read_project_record',
            description='Read actual project documentation with its citation.', strict=True,
            parameters=base.closed(dict(path=dict(type='string', const=DOCUMENT),
                start_line=dict(type='integer', const=1),
                line_count=dict(type='integer', const=16)))))],
        tool_choice='required', parallel_tool_calls=False)
    follow = dict(tool, reasoning_effort='minimal', verbosity='high', tool_choice='none',
        stream_options=dict(include_usage=True, include_obfuscation=False),
        response_format=dict(type='json_schema', json_schema=dict(name='project_record',
            strict=True, schema=base.closed(dict(title=dict(type='string'),
                                                 citation=dict(type='string'))))))
    rows = dict(default=dict(common),
        max=dict(common, reasoning_effort='max', verbosity='low', stream=True,
            stream_options=dict(include_usage=True, include_obfuscation=False)),
        choices=dict(common, reasoning_effort='none', verbosity=None, n=2,
            stream=True, stream_options=dict(include_usage=True), logprobs=True, top_logprobs=2),
        tool=tool, follow=follow,
        ordinary=dict(common, reasoning_effort=None, verbosity=None))
    return {label: tagged(label, body) for label, body in rows.items()}


def check_padding(raw, enabled, count=1):
    require(raw.count(b'data: [DONE]') == 1 and raw.rstrip().endswith(b'data: [DONE]'),
            'exactly one complete DONE required')
    values = []
    for line in raw.splitlines():
        if not line.startswith(b'data: {'):
            continue
        payload = line[6:]
        value = json.loads(payload)
        require('error' not in value, 'stream error')
        choices = value['choices']
        if choices:
            require(('obfuscation' in value) == enabled, 'padding option not honored')
            if enabled:
                pad = value['obfuscation']
                require(isinstance(pad, str) and 1 <= len(pad) <= 256 and
                        not set(pad) - set('0123456789abcdef') and len(payload) % 128 == 0,
                        'invalid actual transport padding')
            require(value.get('usage') is None, 'delta included final usage')
        else:
            require('obfuscation' not in value and isinstance(value.get('usage'), dict),
                    'usage-only terminal altered')
        values.append(value)
    require(values and values[-1]['choices'] == [] and
            sum(not v['choices'] for v in values) == 1, 'usage terminal order/count')
    finals = [c['index'] for v in values for c in v['choices']
              if c['finish_reason'] is not None]
    require(sorted(finals) == list(range(count)), 'missing/duplicate choice terminal')
    return dict(padding=enabled, choice_count=count, delta_events=len(values)-1,
                usage=values[-1]['usage'])


def check_answer(value):
    choices = value.choices if isinstance(value, MultiCompletion) else [dict(
        message=value.message, finish_reason=value.finish_reason)]
    for choice in choices:
        require(choice['finish_reason'] == 'stop' and
                choice['message'].get('content', '').strip() == '42' and
                not choice['message'].get('tool_calls'), 'ordinary answer/terminal differs')


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['mutations'] = ['one own model only; graceful cleanup; no fault/restart/retry']
    plan['derived_requests'] = ['follow messages include actual successful tool call and one real read result',
        'no expected document title or citation supplied before actual tool execution']
    save(path, plan)


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_CHAT_STYLE_STREAM_BUSINESS'

    def start(self):
        base.lifecycle.Stage.start(self)
        caps = read(self.out/'capabilities.json')
        require(caps['template']['reasoning_effort_default'] == 'medium' and
                caps['chat']['stream_include_obfuscation'] and
                caps['chat']['stream_obfuscation_default'], 'new capability missing')
        self.client.protocol_sha256 = caps['protocol_source_sha256']

    def prompt(self, label):
        return json.loads((self.out/(label+'-preflight.wire')).read_bytes())['prompt']

    def business(self):
        rows = copy.deepcopy(self.plan['requests'])
        wire = {}
        for label in ('default', 'max', 'choices'):
            value = self.sdk_request(label, rows[label])
            check_answer(value)
            prompt = self.prompt(label)
            if label == 'default':
                require('Reasoning effort is set to xhigh' not in prompt and
                        prompt.endswith('<think>\n'), 'default medium prompt differs')
            elif label == 'max':
                require('Use maximum reasoning effort within the output budget:' in prompt and
                        'Keep the final answer concise' in prompt, 'style not applied')
            else:
                require(prompt.endswith('<think>\n\n</think>\n\n'), 'none did not close thinking')
                require(isinstance(value, MultiCompletion) and len(value.choices) == 2 and
                        all(c.get('logprobs', {}).get('content') for c in value.choices),
                        'multi-choice actual probabilities missing')
            if rows[label].get('stream'):
                wire[label] = check_padding((self.out/(label+'.wire')).read_bytes(),
                    label != 'max', rows[label].get('n', 1))
        tool = self.sdk_request('tool', rows['tool'])
        wire['tool'] = check_padding((self.out/'tool.wire').read_bytes(), True)
        calls = base.prepare_tool_calls(tool, base.VALIDATORS)
        require(len(calls) == 1 and calls[0].arguments == dict(path=DOCUMENT,
            start_line=1, line_count=16), 'tool argument identity')
        result = base.ProjectRecords(ROOT).execute(calls[0])
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][DOCUMENT],
                'actual project source changed')
        save(self.out/'tool-execution.json', dict(executions=1, result=result))
        follow = rows['follow']
        follow['messages'] = base.append_tool_results(rows['tool']['messages'], tool,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        final = self.sdk_request('follow', follow)
        require(final.finish_reason == 'stop' and not final.message.get('tool_calls'),
                'tool continuation terminal')
        actual = json.loads(final.message['content'])
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(actual['title'] in (title, title.lstrip('# ')) and
                actual['citation'] == result['source']['citation'], 'actual document closure')
        prompt = self.prompt('follow')
        require('Use minimal reasoning:' in prompt and 'Give a detailed final answer' in prompt,
                'follow style not applied')
        wire['follow'] = check_padding((self.out/'follow.wire').read_bytes(), False)
        check_answer(self.sdk_request('ordinary', rows['ordinary']))
        require('Reasoning effort is set to xhigh' not in self.prompt('ordinary'),
                'null did not select medium')
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 6 and
                metrics['q4t_requests_success_total'] == 6 and
                metrics['q4t_requests_error_total'] == 0, 'unexpected request replay/error')
        save(self.out/'business-summary.json', dict(starts=1, creates=6,
            wire=wire, actual_document=actual, tool_executions=1, source_sha256=result['source']['sha256'],
            same_model_pid=True, final_ordinary='42', performance_or_full_quality_claim=False))

    def cleanup(self):
        super().cleanup()
        for name, digest in self.plan['protected'].items():
            require(sha(name) == digest, 'default changed during short acceptance')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18186)
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
