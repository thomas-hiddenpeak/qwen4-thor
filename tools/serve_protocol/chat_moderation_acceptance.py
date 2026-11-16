"""Qualify real Chat review on the exact primary/light release capacities.

The existing schema request additionally performs input score/output block and
stores the reviewed response. The actual document tool, media and embedding
business stays in the same two starts. Internal classifier work is recorded
separately from the eleven external generation requests. The original failed
threat input must classify and block on primary before other business runs.
"""
import argparse
import copy
import json
from pathlib import Path
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import profile_acceptance as qualification
from tools.serve_service.package import read, require, save, sha

ORIGINAL_REQUESTS = qualification.requests
LIMITS = dict(starts=2, chats=11, controls=2000, seconds=540)


def requests(media=False):
    rows = ORIGINAL_REQUESTS(media)
    rows['schema']['moderation'] = dict(model=rows['schema']['model'],
        policy=dict(input=dict(mode='score'), output=dict(mode='block')))
    rows['schema']['store'] = True
    rows['blocked'] = dict(model=rows['schema']['model'], temperature=0,
        enable_thinking=False, max_completion_tokens=32, n=2, store=True,
        messages=[dict(role='user', content='I will kill you tonight.')],
        moderation=dict(model=rows['schema']['model'], policy=dict(
            input=dict(mode='block'), output=dict(mode='block'))))
    return rows


qualification.requests = requests
qualification.limits_for = lambda _profile: LIMITS


def reviewed(value):
    reports = value['moderation']
    require(set(reports) == {'input', 'output'}, 'missing review directions')
    for direction, wrapper in reports.items():
        require(wrapper['type'] == 'moderation_results' and
            wrapper['model'] == value['model'] and len(wrapper['results']) == 1,
            'actual classification unavailable: ' + direction)
        report = wrapper['results'][0]
        require(report['type'] == 'moderation_result' and
            report['model'] == value['model'] and
            type(report['flagged']) is bool and
            len(report['categories']) == 13 and
            set(report['categories']) == set(report['category_scores']) ==
                set(report['category_applied_input_types']),
            'classification shape differs')
        require(all(type(x) is bool for x in report['categories'].values()) and
            all(type(x) in (int, float) and 0 <= x <= 1
                for x in report['category_scores'].values()),
            'invalid model category values')
    require(not reports['output']['results'][0]['flagged'],
            'benign structured answer was blocked')
    usage = value['q4t_moderation_usage']
    require(usage['model_steps'] == 2 and usage['input_tokens'] > 0 and
        0 < usage['output_tokens'] <= 1024, 'missing actual classifier work')
    return copy.deepcopy(usage)


def blocked(value):
    require(len(value['choices']) == 2 and all(
        choice['finish_reason'] == 'content_filter' and
        choice['message'] == dict(role='assistant', content=None)
        for choice in value['choices']), 'original unsafe input did not block')
    require(all(value['usage'][key] == 0 for key in
        ('prompt_tokens', 'completion_tokens', 'total_tokens')),
        'blocked input executed primary generation')
    reports = value['moderation']
    require(reports['input']['type'] == 'moderation_results' and
        reports['input']['results'][0]['flagged'] is True and
        reports['output']['type'] == 'error' and
        reports['output']['code'] == 'moderation_not_executed',
        'missing actual flagged input or unexecuted output report')
    usage = value['q4t_moderation_usage']
    require(usage['model_steps'] == 1 and usage['input_tokens'] > 0 and
        0 < usage['output_tokens'] <= 512, 'missing actual blocked classifier work')
    return copy.deepcopy(usage)


class Stage(qualification.Stage):
    def __init__(self, plan):
        super().__init__(plan)
        self.created = {}
        self.classifier_usage = {}
        self.blocked_result = None

    def stored(self, name, identity, original):
        status, raw = self.http('/v1/chat/completions/' + identity,
            label=name + '-stored')
        require(status == 200, 'reviewed stored completion unavailable')
        value = json.loads(raw)
        for key in ('choices', 'moderation', 'q4t_moderation_usage', 'usage'):
            require(value[key] == original[key],
                    'stored review or approved answer changed: ' + key)
        if value['choices'][0]['finish_reason'] == 'content_filter':
            blocked(value)
        else:
            reviewed(value)

    def request(self, label, body, path='/v1/chat/completions'):
        result = super().request(label, body, path)
        if label == 'schema':
            name = self.profile_name + '-' + label
            self.classifier_usage[name] = reviewed(result)
            self.created[name] = result['id']
            self.stored(name, result['id'], result)
        return result

    def start(self):
        super().start()
        actual = read(self.out / (self.profile_name + '-capabilities.json'))
        expected = dict(modes=['score', 'block'], null='disabled',
            model='served_checkpoint_or_alias', input='actual_messages',
            output='all_choices_aggregate', steps_max=2, step_output_tokens=512,
            block_hold_bytes=33554432, score_errors='explicit',
            block_errors='503', blocked_finish='content_filter',
            storage_after_review=True)
        require(actual['chat']['moderation'] == expected,
                'actual Chat review capability differs')
        if self.profile_name == 'light':
            for name, identity in self.created.items():
                original = json.loads((self.out / (name + '.wire')).read_bytes())
                self.stored('restart-' + name, identity, original)
            require(self.blocked_result is not None, 'input block not completed')
            self.stored('restart-primary-blocked', self.blocked_result['id'],
                        self.blocked_result)

    def profile_business(self):
        if self.profile_name == 'primary':
            self.blocked_result = self.request('blocked', self.plan['requests']['blocked'])
            blocked(self.blocked_result)
            self.stored('primary-blocked', self.blocked_result['id'],
                        self.blocked_result)
        super().profile_business()

    def business(self):
        super().business()
        require(len(self.created) == 2, 'missing primary/light reviewed answers')
        save(self.out / 'moderation-business.json', dict(created=self.created,
            classifier_usage=self.classifier_usage,
            blocked_id=self.blocked_result['id'],
            blocked_usage=blocked(self.blocked_result),
            primary_to_light_restart=True, actual_document_tool_loop=True,
            original_generations_not_replayed=True))


def finalize(output):
    plan = read(output / 'plan.json')
    records = read(output / 'moderation-business.json')
    require(len(records['created']) == 2, 'missing actual review records')
    for name, identity in records['created'].items():
        request = read(output / (name + '.request.json'))
        require(request == plan['requests']['schema'], 'review request drift')
        result = json.loads((output / (name + '.wire')).read_bytes())
        require(result['id'] == identity and
            reviewed(result) == records['classifier_usage'][name],
            'actual review receipt differs')
        names = [name] + (['restart-' + name] if name.startswith('primary-') else [])
        for stored_name in names:
            meta = read(output / (stored_name + '-stored.http.json'))
            stored = json.loads((output / (stored_name + '-stored.wire')).read_bytes())
            require(meta['status'] == 200, 'stored HTTP failed')
            for key in ('choices', 'moderation', 'q4t_moderation_usage', 'usage'):
                require(stored[key] == result[key], 'stored review wire differs')
    result = json.loads((output / 'primary-blocked.wire').read_bytes())
    require(read(output / 'primary-blocked.request.json') == plan['requests']['blocked']
        and result['id'] == records['blocked_id'] and
        blocked(result) == records['blocked_usage'], 'original blocking input differs')
    for name in ('primary-blocked', 'restart-primary-blocked'):
        require(read(output / (name + '-stored.http.json'))['status'] == 200,
                'blocked stored response unavailable')
        stored = json.loads((output / (name + '-stored.wire')).read_bytes())
        for key in ('choices', 'moderation', 'q4t_moderation_usage', 'usage'):
            require(stored[key] == result[key], 'blocked stored response changed')
    qualification.finalize(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    prepare = modes.add_parser('prepare')
    for key in ('binary', 'candidate-build', 'package', 'output'):
        prepare.add_argument('--' + key, type=Path, required=True)
    prepare.add_argument('--port', type=int, default=18188)
    run = modes.add_parser('run')
    run.add_argument('--plan', type=Path, required=True)
    final = modes.add_parser('finalize')
    final.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        qualification.prepare(args)
        path = args.output.resolve() / 'plan.json'
        plan = read(path)
        plan['sources'][str(Path(__file__).resolve())] = sha(Path(__file__))
        plan['scope'] = __doc__
        plan['internal_model_work'] = 'two actual classifiers per schema request'
        save(path, plan)
    elif args.mode == 'finalize':
        finalize(args.output.resolve())
    else:
        def timeout(_signal, _frame):
            raise TimeoutError('Chat review qualification deadline')
        previous = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds'] - 40)
            with qualification.encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
