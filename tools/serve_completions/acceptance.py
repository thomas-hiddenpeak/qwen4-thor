"""A06 bounded native Completions/Models business acceptance; no perf claim."""
import argparse
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MODEL = 'qwen3.8-flash-next'


def raw_prompt(question):
    return ('<|im_start|>user\n'+question+'<|im_end|>\n'
            '<|im_start|>assistant\n<think>\n\n</think>\n\n')


def requests():
    common = dict(model=MODEL, temperature=0)
    prompts = [raw_prompt('Write a detailed sentence about an ancient forest.'),
               raw_prompt('Write a detailed sentence about a distant galaxy.')]
    return dict(echo=dict(common, prompt=('e\u0301中 literal <think>\n'*6)+'X',
            echo=True, max_tokens=0, logprobs=2),
        batch=dict(common, prompt=prompts, n=3, temperature=1, seed=57,
            max_tokens=4, logprobs=3, logit_bias={'248044': -100, '248046': -100}),
        suffix=dict(common, prompt='def add(a, b):\n    return ',
            suffix='\n\nassert add(2, 3) == 5\n', max_tokens=16, stop=['\n']),
        cancel=dict(common, prompt=raw_prompt('Write a long detailed essay of at least '
            '2000 words about the history of mathematics.'), n=2, max_tokens=1024,
            stream=True, stream_options=dict(include_usage=True),
            request_id='completion-cancel', cancel_token='c'*64),
        chat=dict(common, messages=[dict(role='user', content='Compute 17 + 25. '
            'Reply only with the decimal number.')], enable_thinking=False,
            max_completion_tokens=64))


def best_of_expected(value, n=3):
    choices = value['choices']
    require(len(choices) == 2*n, 'candidate count')
    result = []
    for group in range(2):
        candidates = choices[group*n:(group+1)*n]
        scores = []
        for i, row in enumerate(candidates):
            require(row['index'] == group*n+i and row['finish_reason'] == 'length',
                    'candidate must expose all generated token probabilities')
            probabilities = row['logprobs']['token_logprobs']
            require(len(probabilities) == 4 and all(type(p) in (int, float) and
                p <= 0 for p in probabilities), 'missing actual candidate scores')
            scores.append(sum(probabilities)/len(probabilities))
        winner = max(range(n), key=lambda i: scores[i])
        result.append(dict(index=group, candidate=candidates[winner], scores=scores))
    return result


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in subprocess.check_output(['git', 'ls-files', 'tools/serve_completions'],
                                         cwd=ROOT, text=True).splitlines():
        plan['sources'][name] = sha(ROOT/name)
    plan['planned'] = dict(starts=1, chats=7)
    plan['derived_requests'] = [
        'batch raw strings become exact IDs from production preflight; no generation',
        'SSE reuses JSON input/seed; best_of=3,n=1 reuses the same candidate seeds',
        'Models list/detail/delete are observation/control requests, not generation']
    save(path, plan)


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_COMPLETIONS_MODELS_BUSINESS'

    def sdk(self, label, action, request=None, *, model=MODEL, expected_status=None):
        self.budget('chats' if action == 'completion' else 'controls')
        command = dict(action=action, request=request, model=model,
            base_url=f"http://127.0.0.1:{self.plan['port']}/v1",
            sdk_directory=self.plan['sdk']['sdk_directory'], timeout_ms=120000)
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_completions/run_sdk.mjs')],
            input=json.dumps(command, ensure_ascii=False), capture_output=True,
            text=True, env=env, timeout=135)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'SDK bridge failure')
        result = json.loads(run.stdout)
        wire = base64.b64decode(result.pop('wire_base64'), validate=True)
        (self.out/(label+'.wire')).write_bytes(wire)
        save(self.out/(label+'.sdk.json'), result)
        require(len(result['exchanges']) == 1, 'SDK replayed request')
        if expected_status:
            require(not result['ok'] and result['error']['status'] == expected_status,
                    'expected model permission/not-found error')
        else:
            require(run.returncode == 0 and result['ok'], 'SDK request failed: '+label)
        self.same_model()
        self.drained(label+'-drain')
        return result.get('result')

    def cancel(self, body):
        self.budget('chats')
        save(self.out/'cancel.request.json', body)
        visible = threading.Event()
        observed = {}

        def consume():
            conn = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=90)
            try:
                conn.request('POST', '/v1/completions', json.dumps(body),
                             {'Content-Type': 'application/json'})
                response = conn.getresponse()
                observed['status'] = response.status
                with (self.out/'cancel.wire').open('xb') as wire:
                    total = 0
                    while line := response.readline():
                        total += len(line)
                        require(total < 4*1024*1024, 'cancel wire limit')
                        wire.write(line)
                        if line.startswith(b'data: {'):
                            event = json.loads(line[6:])
                            if any(row.get('text') for row in event.get('choices', [])):
                                visible.set()
            except BaseException:
                observed['error'] = traceback.format_exc()
            finally:
                conn.close()

        self.fault_thread = threading.Thread(target=consume, name='completion-cancel')
        self.fault_thread.start()
        require(visible.wait(60), 'no completion content before cancellation')
        self.budget('controls')
        status, _ = self.http('/v1/requests/cancel',
            {k: body[k] for k in ('request_id', 'cancel_token')}, label='cancel-control')
        require(status == 202, 'completion cancellation rejected')
        self.fault_thread.join(30)
        require(not self.fault_thread.is_alive() and not observed.get('error'),
                'completion cancellation did not finish')
        save(self.out/'cancel.result.json', observed)
        raw = (self.out/'cancel.wire').read_bytes()
        events = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith(b'data: {')]
        require(observed['status'] == 200 and raw.count(b'data: [DONE]') == 1 and
                events[-1]['error']['code'] == 'explicit_cancel', 'cancel terminal')
        require(not any(e.get('usage') for e in events) and not any(c.get('finish_reason')
            for e in events for c in e.get('choices', [])), 'cancel claimed success')
        self.drained('cancel-drain')

    def business(self):
        rows = self.plan['requests']
        listed = self.sdk('models', 'models')
        model = next(item for item in listed['data'] if item['id'] == MODEL)
        require(self.sdk('model-detail', 'retrieve') == model, 'model detail identity')
        require(self.sdk('model-detail-repeat', 'retrieve') == model, 'created changed')
        self.sdk('model-absent', 'retrieve', model='not-registered', expected_status=404)
        self.sdk('model-readonly', 'delete', expected_status=403)
        echo = self.sdk('echo', 'completion', rows['echo'])
        row = echo['choices'][0]
        require(row['text'] == rows['echo']['prompt'] and row['finish_reason'] == 'length'
            and echo['usage']['completion_tokens'] == 0, 'zero-output echo identity')
        lp = row['logprobs']
        require(len(lp['tokens']) == echo['usage']['prompt_tokens'] and
                lp['token_logprobs'][0] is None and lp['top_logprobs'][0] is None,
                'prompt probability rows')
        require(lp['text_offset'][0] == 0 and
                lp['text_offset'][-1] == len(rows['echo']['prompt'])-1 and
                all(a <= b for a, b in zip(lp['text_offset'], lp['text_offset'][1:])),
                'echo original Unicode offsets')
        body = copy.deepcopy(rows['batch'])
        ids = []
        for i, prompt in enumerate(body['prompt']):
            self.budget('controls')
            status, raw = self.http('/v1/chat/preflight', dict(request=dict(
                model=MODEL, prompt=prompt, temperature=0, max_tokens=4),
                include_token_ids=True), label='batch-tokenize-'+str(i))
            require(status == 200, 'raw prompt preflight')
            ids.append(json.loads(raw)['token_ids'])
        body['prompt'] = ids
        plain = self.sdk('batch-json', 'completion', body)
        require(plain['usage']['prompt_tokens'] == sum(map(len, ids)) and
                plain['usage']['completion_tokens'] == 24, 'batch token usage')
        streaming = self.sdk('batch-sse', 'completion', dict(body, stream=True,
            stream_options=dict(include_usage=True, include_obfuscation=False)))
        require(plain['choices'] == streaming['choices'] and
                plain['usage'] == streaming['usage'], 'JSON/SSE deterministic parity')
        selection = best_of_expected(plain)
        save(self.out/'best-of-expected.json', selection)
        best = self.sdk('best-of', 'completion', dict(body, n=1, best_of=3))
        require(best['usage'] == plain['usage'], 'unreturned candidates not charged')
        for expected, actual in zip(selection, best['choices']):
            require(actual == dict(expected['candidate'], index=expected['index']),
                    'best_of did not select the actual highest mean probability')
        suffix = self.sdk('suffix', 'completion', rows['suffix'])
        text = suffix['choices'][0]['text'].strip()
        # Compare permitted source text, never execute model-generated code.
        require(text.replace(' ', '') in ('a+b', 'b+a'), 'FIM business result: '+text)
        self.cancel(rows['cancel'])
        final = self.sdk_request('same-pid-chat', rows['chat'])
        require(final.finish_reason == 'stop' and final.message['content'].strip() == '42',
                'Chat did not continue after completion cancellation')
        final_metrics = self.drained('final')
        require(final_metrics['q4t_requests_total'] == 7 and
                final_metrics['q4t_requests_success_total'] == 6 and
                final_metrics['q4t_requests_error_total'] == 1, 'business accounting')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18164)
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
