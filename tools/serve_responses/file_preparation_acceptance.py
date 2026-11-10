"""Real file preparation: 1 start, 4 creates, 6 model steps, 600 seconds.

CSV JSON, frozen previous/SSE, background CSV and ordinary same-model Chat.
Actual SDK, shared RPM/TPM and cleanup; no performance or full-model matrix.
"""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import signal
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read, require, save, sha

foreground = background.foreground
LIMITS = dict(starts=1, chats=4, controls=2000, seconds=600)
MODEL_STEPS = 6
CSV = b'name,value\nalpha,42\nbeta,7\n'


def requests():
    schema = foreground.base.closed(dict(value=dict(type='integer')))
    common = dict(model=foreground.MODEL, temperature=0,
                  reasoning=dict(effort='none'), max_output_tokens=128,
                  text=dict(format=dict(type='json_schema', name='file_value',
                      strict=True, schema=schema)))
    question = 'Read the attached table. Return alpha\'s value in the JSON value field.'
    initial = dict(common, store=True, input=[dict(role='user', content=[
        dict(type='input_file', filename='values.csv',
             file_data=base64.b64encode(CSV).decode()),
        dict(type='input_text', text=question)])])
    follow = dict(common, store=True, stream=True,
                  stream_options=dict(include_obfuscation=False),
                  input='From the previous table, return beta\'s value in the JSON value field.')
    return dict(initial=initial, follow=follow,
                background=dict(copy.deepcopy(initial), background=True),
                chat=foreground.requests()['chat'])


def check_answer(value, expected):
    background.check_response(value, 'completed')
    require(json.loads(foreground.text(value)) == dict(value=expected),
            'actual table answer differs from original bytes')


def model_steps(log):
    return len(re.findall(r'^\[sampling\] request=', log, re.M))


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in ('file_preparation_acceptance.py', 'test_file_preparation_acceptance.py'):
        relative = 'tools/serve_responses/'+name
        plan['sources'][relative] = sha(ROOT/relative)
    plan['planned'] = dict(starts=1, chats=4, model_steps=MODEL_STEPS)
    plan['mutations'] = ['isolated model, private object store/client policy; graceful owned cleanup']
    plan['derived_requests'] = [
        'first CSV bytes are fixed original rows; actual summary plus primary answer',
        'previous ID comes from actual first response; frozen summary reused, one model step',
        'background CSV owns an actual summary and primary step, no caller loopback request',
        'ordinary Chat remains on the same model PID after preparation',
        'exact four RPM and all six model steps in actual aggregate usage; no automatic retries']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_FILE_PREPARATION_BUSINESS'
    requests_per_minute = 4

    def step_count(self):
        return model_steps((self.out/'model-1.log').read_text())

    def business(self):
        rows = self.plan['requests']
        initial = self.sdk('initial', 'create', rows['initial'])
        check_answer(initial, 42)
        require(self.step_count() == 2, 'CSV did not execute one preparation and one answer')
        follow = dict(rows['follow'], previous_response_id=initial['id'])
        second = self.sdk('follow', 'create', follow)
        check_answer(second, 7)
        require(self.step_count() == 3, 'previous replay regenerated its frozen summary')
        events = read(self.out/'follow.sdk.json')['chunks']
        require([e['sequence_number'] for e in events] == list(range(len(events))) and
                events[0]['type'] == 'response.created' and
                events[-1]['type'] == 'response.completed', 'SSE lifecycle differs')
        queued = self.sdk('background', 'create', rows['background'], drain=False)
        third = background.Stage.wait_response(
            self, 'background-completed', queued['id'],
            lambda value: value['status'] == 'completed', seconds=90)
        check_answer(third, 42)
        self.drained('background-drain')
        require(self.step_count() == 5, 'background preparation was omitted or replayed')
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', rows['chat'], label='same-pid-chat')
        chat = json.loads(raw)
        require(status == 200 and chat['choices'][0]['message']['content'].strip() == '42',
                'ordinary same-model Chat failed')
        self.accounting('final-accounting', [initial, second, third], chat=chat)
        require(self.step_count() == MODEL_STEPS, 'unexpected model execution count')
        self.same_model()
        save(self.out/'business-checks.json', dict(generations=4, model_steps=6,
            original_csv_sha256=hashlib.sha256(CSV).hexdigest(),
            actual_model_preparation=True, frozen_previous_summary_reused=True,
            actual_background_preparation=True, sse_lifecycle=True,
            exact_generation_rpm_tpm=True, same_model_ordinary_chat=True,
            default_unchanged=True, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=19031)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def timeout(_signal, _frame):
            raise TimeoutError('file preparation business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__':
    main()
