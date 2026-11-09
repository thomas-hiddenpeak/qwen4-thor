"""Request fields: <=1 start/2 generations/2 counts/10 rejections/600 seconds."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import input_tokens_acceptance as count
from tools.serve_responses import input_tokens_null_acceptance as nullable
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=2, controls=2000, seconds=600)


def requests():
    plain = count.foreground.requests()['plain']
    instruction = dict(plain, service_tier='ultrafast', stream=True,
                       stream_options=dict(include_obfuscation=False))
    instruction['instructions'] = instruction.pop('input')
    return dict(fast=dict(plain, service_tier='fast'), instructions=instruction)


def invalid_requests():
    base = dict(input='Compute 17+25.', instructions='Answer only the number.')
    rows = [(name, dict(base, **{name: None})) for name in (
        'access_programs', 'input', 'model', 'prompt_cache_options',
        'text', 'tool_choice', 'tools', 'user')]
    rows += [('reasoning.mode', dict(base, reasoning=dict(mode=None),
                                     prompt=dict(id='prompt_missing'))),
             ('input', dict(base, input=True, prompt=dict(id='prompt_missing')))]
    return rows


def check_generation(value):
    count.background.check_response(value, 'completed')
    require(value['service_tier'] == 'default', 'claimed a tier not actually used')
    require(count.foreground.text(value).strip() == '42', 'arithmetic failed')


def prepare(args):
    count.background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/request_fields_acceptance.py'] = sha(Path(__file__))
    plan['planned'] = dict(starts=1, chats=2, model_steps=2, input_counts=2,
                           invalid_requests=10)
    plan['invalid_requests'] = invalid_requests()
    plan['mutations'] = ['only private model/object/client state; graceful own cleanup']
    plan['derived_requests'] = ['two actual standard SDK JSON/SSE responses; no generation retry',
        'all ten invalid create requests fail with exact param before model or quota',
        'omitted input with instructions generates; explicit null input rejects',
        'count ten nullable fields equals empty defaults without inference',
        'actual default service tier, precise two-request RPM/TPM and model steps']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_REQUEST_FIELDS_BUSINESS'

    def business(self):
        require(self.plan['invalid_requests'] == [list(r) for r in invalid_requests()],
                'invalid request plan drift')
        for index, (param, request) in enumerate(invalid_requests()):
            self.budget('controls')
            label = 'invalid-' + str(index)
            save(self.out/(label+'.request.json'), request)
            status, raw = self.http('/v1/responses', request, label=label)
            value = json.loads(raw)
            require(status == 400 and value['error']['param'] == param,
                    'invalid input was not rejected before state/model: '+label)
            self.same_model()
        empty = count.count_value(self.sdk('count-empty', 'input_tokens', {}))
        require(count.count_value(self.sdk('count-null', 'input_tokens',
                                           nullable.nullable())) == empty,
                'create validation leaked into count default handling')
        values = []
        for name, request in requests().items():
            value = self.sdk(name, 'create', request)
            check_generation(value)
            values.append(value)
        self.accounting('final-accounting', values, rejected=10)
        log = (self.out/'model-1.log').read_text()
        for value in values:
            summary.check_steps(log, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == 2,
                'rejected/count request performed model work')
        save(self.out/'business-checks.json', dict(generations=2, model_steps=2,
            input_counts=2, invalid_requests=10, exact_error_params=True,
            instructions_only_generated=True, actual_service_tier='default',
            count_nullable_defaults_preserved=True, exact_generation_rpm_tpm=True,
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
        with count.foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
