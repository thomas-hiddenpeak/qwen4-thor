"""Standalone moderation: 2 starts/8 requests/at most9 steps/900 seconds.

Text MTP and ordinary vision profiles share original Batch state. No model
restart on errors, no default changes, no performance/full-quality matrix.
"""
import argparse
import base64
import copy
import json
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import batches_acceptance as batches
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import moderations_contract as contract
from tools.serve_responses.file_preparation_acceptance import model_steps
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read, require, save, sha

foreground = encrypted.foreground
LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MAX_MODEL_STEPS = 9
LIMITED_CLIENT = 'moderation-api-limited'
IMAGE = 'tests/fixtures/file_images/original.png'


def requests():
    benign = 'Have a good day. The park has green trees and a quiet walking path.'
    threat = 'I am going to kill you tonight with a knife. I am waiting outside your home.'
    image = dict(type='image_url', image_url=dict(url='data:image/png;base64,'+
        base64.b64encode((ROOT/IMAGE).read_bytes()).decode()))
    return dict(text=dict(input=[benign, threat]),
        multimodal=dict(model=foreground.MODEL,
            input=[dict(type='text', text='Two plain color rectangles.'), image]),
        image_only=dict(input=[copy.deepcopy(image)]),
        invalid=dict(model='model-that-is-not-served', input=benign),
        quota=dict(input=benign), batch=[dict(input=[benign, threat])],
        cancel=[dict(input=[benign]*8)],
        chat=foreground.requests()['chat'])


def check_response(value, flags, *, text=True, image=False):
    require(not errors(value, 'moderations_response'), 'standard moderation schema')
    require(value['model'] == foreground.MODEL and
            len(value['results']) == len(flags), 'actual model/result cardinality')
    for report, flag in zip(value['results'], flags):
        actual = []
        for name in contract.CATEGORIES:
            types = (['text'] if text else []) + (
                ['image'] if image and name in contract.IMAGE_CATEGORIES else [])
            require(report['category_applied_input_types'][name] == types,
                    'classification input modality differs')
            score = report['category_scores'][name]
            require(types or score == 0, 'inapplicable category got a score')
            decision = bool(types) and score >= .5
            require(report['categories'][name] == decision, 'threshold mismatch')
            actual.append(decision)
        require(report['flagged'] == any(actual) and report['flagged'] == flag,
                'bounded benign/threat classification differs')


def check_meter(before, after, *, admitted=1, client=encrypted.CLIENT):
    suffix = '{client="'+client+'"}'
    delta = lambda key: after.get(key, 0)-before.get(key, 0)
    require(delta('q4t_client_rate_admitted_total'+suffix) == admitted and
            after['q4t_client_rate_reserved_tokens'+suffix] == 0, 'RPM/refund mismatch')
    inputs = delta('q4t_prompt_tokens_total')
    outputs = delta('q4t_generation_tokens_total')
    require(delta('q4t_client_rate_settled_tokens_total'+suffix) == inputs+outputs,
            'actual model input/output not included in TPM')
    require((inputs > 0 and outputs > 0) if admitted else inputs == outputs == 0,
            'missing actual classification or rejected request ran model')
    return dict(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs+outputs)


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in (IMAGE, 'tools/serve_responses/moderations_acceptance.py',
                 'tools/serve_responses/test_moderations_acceptance.py'):
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=2, requests=8, maximum_model_steps=MAX_MODEL_STEPS,
        profiles=['default_text_mtp', 'ordinary_decode_vision'],
        positive_online=4, negative_online=2, batch_rows=2)
    plan['mutations'] = ['isolated object/client state; own Batch cancel after first model step starts',
        'explicit graceful text->vision transition after all owners drain',
        'no model replay, automatic recovery, changes to defaults or other services']
    plan['derived_requests'] = ['only native returned Batch/File IDs; no synthetic model outputs',
        'read same original completed/cancelled Batch files after profile transition',
        'actual standard image bytes and all13 score maps; no cloud calibration claim',
        'text array uses one RPM and both classification steps in actual TPM',
        'errors, cancelled work, independent Batch quota and resource cleanup are checked']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_MODERATIONS_BUSINESS'
    requests_per_minute = 24
    tokens_per_minute = 65536
    request = batches.Stage.request
    create = batches.Stage.create
    finish = batches.Stage.finish
    outputs = batches.Stage.outputs
    batch_idle = batches.Stage.batch_idle

    def client_policy(self):
        policy = super().client_policy()
        policy['clients'][0]['max_output_tokens'] = 64
        policy['clients'].append(dict(policy['clients'][0], id=LIMITED_CLIENT,
                                     tokens_per_minute=16))
        return policy

    def extra_model_args(self):
        args = ['--object-max-client-mib', '256', '--object-max-total-mib', '512']
        if self.counts['starts'] >= 1: args += ['--no-mtp', '--allow-media']
        return args

    def moderate(self, name, flags=None, *, text=True, image=False, expected=None):
        before = self.drained(name+'-before')
        self.budget('chats')
        headers = dict(encrypted.HEADERS)
        if name == 'quota': headers['X-Q4T-Client-Id'] = LIMITED_CLIENT
        value = self.sdk(name, 'moderations_create', requests()[name],
            headers=headers, expected_status=expected)
        after = self.drained(name+'-after')
        if expected is None:
            check_response(value, flags, text=text, image=image)
        else:
            error = read(self.out/(name+'.sdk.json'))['error']
            require(error['code'] == ('model_not_found' if name == 'invalid'
                                     else 'client_tpm_exhausted'), 'wrong native error')
        save(self.out/(name+'-actual-usage.json'), check_meter(before, after,
            admitted=0 if expected else 1,
            client=LIMITED_CLIENT if name == 'quota' else encrypted.CLIENT))
        return value

    def steps(self):
        return sum(model_steps(p.read_text()) for p in self.out.glob('model-*.log'))

    def business(self):
        rows = requests()
        self.moderate('text', [False, True])
        self.moderate('invalid', expected=404)
        self.moderate('quota', expected=429)
        batch = self.finish('batch', self.create('batch', '/v1/moderations', rows['batch']))
        batches.check_batch(batch, 1, 1, 0)
        result = self.outputs('batch-output', batch, ['batch-0'])[0]
        check_response(result['response']['body'], [False, True])
        before_cancel = self.steps()
        pending = self.create('cancel', '/v1/moderations', rows['cancel'])
        end = time.monotonic()+30
        while time.monotonic() < end:
            self.same_model()
            if self.steps() > before_cancel: break
            time.sleep(.025)
        else: raise TimeoutError('original moderation cancellation step never began')
        # The sampling record precedes model admission. Allow a bounded interval
        # for actual work, then cancel the same owner without creating another row.
        time.sleep(.15)
        self.request('POST', '/v1/batches/'+pending['id']+'/cancel', b'{}', label='cancel-request')
        cancelled = self.finish('cancel', pending)
        batches.check_batch(cancelled, 1, 0, 1, 'cancelled')
        failed = self.outputs('cancel-error', cancelled, ['cancel-0'], 'error_file_id')[0]
        require(failed['response']['status_code'] in (409, 499) and
                'results' not in failed['response']['body'], 'cancel published partial results')
        metrics = self.batch_idle('batch-final')
        suffix = '{client="'+encrypted.CLIENT+'"}'
        require(metrics['q4t_client_batch_rate_admitted_total'+suffix] == 2 and
            metrics['q4t_client_batch_rate_reserved_tokens'+suffix] == 0 and
            metrics['q4t_client_batch_rate_settled_tokens_total'+suffix] ==
                batch['usage']['total_tokens']+cancelled['usage']['total_tokens'],
            'cancelled Batch actual work/refund differs')
        self.budget('chats')
        before = self.drained('chat-before')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='chat')
        require(status == 200, 'same-process Chat failed')
        chat = json.loads(raw)
        require(chat['choices'][0]['message']['content'].strip() == '42', 'Chat answer')
        check_meter(before, self.drained('chat-after'))
        retained = [(batch, 'output_file_id', self.out/'batch-output.wire'),
                    (cancelled, 'error_file_id', self.out/'cancel-error.wire')]
        first_steps = self.steps()
        require(6 <= first_steps <= 7, 'unexpected text/cancel model steps')
        self.cleanup()
        cleanup = read(self.out/'cleanup.json')
        save(self.out/'text-cleanup.json', cleanup)
        require(cleanup['model_exit'] == 0, 'text profile did not stop normally')
        self.model = None
        self.start()
        config = read(self.out/'config-2.json')
        require(not config['runtime']['mtp_enabled'], 'wrong vision profile')
        for i, (original, key, file) in enumerate(retained):
            current = json.loads(self.request('GET', '/v1/batches/'+original['id'],
                                             label='retained-'+str(i)))
            require(current == original, 'original Batch changed across profile transition')
            raw = self.request('GET', '/v1/files/'+original[key]+'/content',
                               label='retained-file-'+str(i))
            require(raw == file.read_bytes(), 'original moderation result bytes changed')
        self.moderate('multimodal', [False], image=True)
        self.moderate('image_only', [False], text=False, image=True)
        steps = self.steps()
        require(steps == first_steps+2 and steps <= MAX_MODEL_STEPS,
                'extra classification, model replay or missing vision')
        require(self.counts['starts'] == 2 and self.counts['chats'] == 8, 'request budget drift')
        save(self.out/'acceptance.json', dict(status='ACCEPTED', model_steps=steps,
            text_mtp=True, image_native=True, standard_sdk=True, batch_actual_usage=True,
            running_cancel=True, original_state_preserved=True, default_unchanged=True,
            cloud_calibration_claim=False, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    for name in ('binary', 'output'): command.add_argument('--'+name, type=Path, required=True)
    command.add_argument('--port', type=int, default=19061)
    command = modes.add_parser('run'); command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        def timeout(_signal, _frame): raise TimeoutError('moderation business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with foreground.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()
