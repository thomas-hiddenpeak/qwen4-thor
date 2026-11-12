"""Count-only nullable defaults: <=1 start/1 generation/4 counts/300 seconds."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import input_tokens_acceptance as first
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import summary_acceptance as summary
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, chats=1, controls=2000, seconds=300)


def requests():
    row = first.foreground.requests()['plain']
    return dict(initial=dict(input=row['input'], model=first.foreground.MODEL,
        temperature=0, max_output_tokens=384, store=False))


def nullable():
    return {name: None for name in (
        'conversation', 'input', 'instructions', 'model', 'parallel_tool_calls',
        'previous_response_id', 'reasoning', 'text', 'tool_choice', 'tools')}


def prepare(args):
    first.background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['sources']['tools/serve_responses/input_tokens_null_acceptance.py'] = sha(Path(__file__))
    plan['planned'] = dict(starts=1, chats=1, model_steps=1, maximum_input_counts=4)
    plan['derived_requests'] = ['count all ten nullable SDK fields separately validated on CPU',
        'four actual count requests: empty/default, empty/null, task/null, task/default',
        'one actual arithmetic generation and count equals input usage; no count model steps',
        'prior business 1 start/4 generations/15 counts remains accepted for unchanged paths',
        'combined ceiling 2 starts/5 generations/20 counts/900 seconds; no full rerun']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_INPUT_TOKENS_NULL_BUSINESS'

    def business(self):
        empty = first.count_value(self.sdk('default-empty', 'input_tokens', {}))
        require(first.count_value(self.sdk('null-empty', 'input_tokens', nullable())) == empty,
                'nullable empty defaults differ')
        row = requests()['initial']
        task = dict(nullable(), input=row['input'])
        count = first.count_value(self.sdk('null-task', 'input_tokens', task))
        require(first.count_value(self.sdk('default-task', 'input_tokens',
                                           dict(input=row['input']))) == count,
                'nullable task defaults differ')
        value = self.sdk('initial', 'create', row)
        first.check_count_usage(count, value)
        require(first.foreground.text(value).strip() == '42', 'actual arithmetic failed')
        self.accounting('final-accounting', [value])
        log = (self.out/'model-1.log').read_text()
        summary.check_steps(log, value, 1)
        require(len(re.findall(r'^\[sampling\] request=', log, re.M)) == 1,
                'nullable count generated or repeated model work')
        save(self.out/'business-checks.json', dict(generations=1, model_steps=1,
            input_counts=4, count_matches_actual_usage=True,
            nullable_defaults_equal_omission=True, generation_rpm_tpm_unchanged=True,
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
        with first.foreground.base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
