"""Native retrieval backend: 2 starts/9 requests/450 layer steps/900 seconds.

Actual official SDK, missing-asset isolation, request-driven recovery, exact
usage, quota rejection, original Batch cancellation/restart, and healthy Chat.
Reuse the immutable numerical corpus; no full performance or quality matrix.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import struct
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jsonschema import Draft202012Validator
from tools.native_embedding import acceptance as numerical
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import batches_acceptance as batches
from tools.serve_responses import embeddings_acceptance as generic
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.test_contract import errors
from tools.serve_grammar_reap.acceptance import parse_metrics
from tools.serve_service.package import read, require, save, sha

MODEL = 'qwen3-embedding-0.6b'
ORIGINAL = ROOT/'.q4t-work/native-embedding-20261111/numerical-02'
LIMITS = dict(starts=2, chats=9, controls=2000, seconds=900)
MAX_EMBEDDING_STEPS = 450
LIMITED_CLIENT = 'native-embedding-limited'


def corpus():
    return read(ORIGINAL/'plan.json')['cases']


def requests():
    common = dict(model=MODEL)
    cases = corpus()
    return dict(missing=dict(common, input='!'),
        default=dict(common, input=['!']+[row['text'] for row in cases[4:]]),
        ids=dict(common, input=[[0, 151643]], encoding_format='float'),
        reduced=dict(common, input=[0, 151643], dimensions=32,
                     encoding_format='base64'),
        quota=dict(common, input=[0]*17),
        batch=[dict(common, input='!', dimensions=32, encoding_format='base64')],
        cancel=[dict(common, input=[[0]*8192, [0]*8192], dimensions=32)],
        chat=dict(encrypted.foreground.requests()['chat'], max_completion_tokens=64))


def vectors(value, count, dimensions):
    require(not errors(value, 'embeddings_response'), 'embedding wire schema')
    require(value['model'] == MODEL, 'native canonical model absent')
    # Reuse existing tested FP32/ordering/L2 oracle, including default SDK decode.
    checked = dict(value, model=generic.foreground.MODEL)
    return generic.vectors(checked, count, dimensions)


def retrieval(rows):
    require(len(rows) == 11, 'real retrieval cardinality')
    results = []
    for index, query in enumerate(rows[6:]):
        scores = [math.fsum(a*b for a, b in zip(query, doc)) for doc in rows[1:6]]
        order = sorted(range(5), key=lambda i: scores[i], reverse=True)
        results.append(dict(query=numerical.QUERIES[index], expected_document=index,
                            ranking=order, cosine_scores=scores))
    require(all(row['ranking'][0] == row['expected_document'] for row in results),
            'actual project retrieval failed')
    return results


def reference_vectors(rows):
    results = []
    for current, index in zip(rows, [1]+list(range(4, 14))):
        raw = (ORIGINAL/'reference'/f'case-{index}'/'last.bf16').read_bytes()
        values = [struct.unpack('<f', struct.pack('<I', v << 16))[0]
                  for v in struct.unpack('<1024H', raw)]
        norm = math.sqrt(math.fsum(v*v for v in values))
        expected = [v/norm for v in values]
        cosine = math.fsum(a*b for a,b in zip(current, expected))
        absolute = max(abs(a-b) for a,b in zip(current, expected))
        require(cosine >= .999 and absolute <= .005,
                'HTTP final vector differs from original reference contract')
        results.append(dict(case=index, cosine=cosine,
                            max_normalized_absolute_error=absolute))
    return results


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    assets = args.assets.absolute()
    require(assets.is_relative_to(ROOT/'.q4t-work'), 'isolated optional assets only')
    plan['assets'] = str(assets)
    plan['asset_hashes'] = {name:sha(assets/name) for name in
                           ['config.json', 'tokenizer.json', 'model.safetensors']}
    require(plan['asset_hashes']['model.safetensors'] ==
            '0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd',
            'optional weight identity')
    for name in ['plan.json', 'comparison.json']:
        file = ORIGINAL/name
        plan['sources'][str(file)] = sha(file)
    for index in [1]+list(range(4, 14)):
        file = ORIGINAL/'reference'/f'case-{index}'/'last.bf16'
        plan['sources'][str(file)] = sha(file)
    for file in (ROOT/'tools/native_embedding').glob('*.py'):
        plan['sources'][str(file.relative_to(ROOT))] = sha(file)
    for row in read(ORIGINAL/'plan.json')['documents']:
        require(sha(ROOT/row['path']) == row['sha256'], 'original source excerpt changed')
        plan['sources'][row['path']] = row['sha256']
    isolated = path.parent/'optional-assets'
    isolated.mkdir(mode=0o700)
    for name in ['config.json', 'tokenizer.json']:
        os.link(assets/name, isolated/name)
    plan['planned'] = dict(starts=2, requests=9, embedding_steps_max=450,
        primary_model_starts=2, successful_native_weight_uploads=2,
        intentional_missing_weight_loads=1,
        chat_requests=2, chat_output_tokens_each_max=64, numerical_corpus='reused')
    plan['mutations'] = ['private optional asset directory initially lacks weights',
        'hardlink only own optional weight after observed failure; same PID retry',
        'cancel original Batch after lookup commits; retain original result',
        'graceful restart once; original IDs and bytes checked; own cleanup only']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_RETRIEVAL_BUSINESS'
    requests_per_minute = 24
    tokens_per_minute = 65536
    request = batches.Stage.request
    create = batches.Stage.create
    finish = batches.Stage.finish
    outputs = batches.Stage.outputs
    batch_idle = batches.Stage.batch_idle

    def client_policy(self):
        policy = super().client_policy()
        policy['clients'].append(dict(policy['clients'][0], id=LIMITED_CLIENT,
                                     tokens_per_minute=16))
        return policy

    def extra_model_args(self):
        return ['--embedding-model-dir', str(self.out/'optional-assets'),
                '--object-max-client-mib', '256', '--object-max-total-mib', '512']

    def observation(self, label, state):
        status, raw = self.http('/v1/config', label=label)
        require(status == 200, 'config unavailable')
        value = json.loads(raw)
        Draft202012Validator(read(ROOT/'docs/serve-config-v1.schema.json')).validate(value)
        backend = value['native_embedding']
        health = value['native_embedding_health']
        require(backend['enabled'] and backend['available'] and
                health['state'] == state and not health['core_readiness_dependency'],
                'independent native backend state differs')
        require(self.http('/readyz')[0] == 200, 'healthy primary readiness lost')
        self.same_model()
        return health

    def start(self):
        for name, expected in self.plan['asset_hashes'].items():
            require(sha(Path(self.plan['assets'])/name) == expected, 'asset changed')
        super().start()
        health = self.observation('cold-'+str(self.counts['starts']), 'configured')
        require(health['device_bytes'] == 0 and health['load_attempts'] == 0,
                'optional weights loaded eagerly')

    def embed(self, name, expected=None):
        before = self.drained(name+'-before')
        self.budget('chats')
        headers = dict(encrypted.HEADERS)
        if name == 'quota': headers['X-Q4T-Client-Id'] = LIMITED_CLIENT
        result = self.sdk(name, 'embeddings_create', self.plan['requests'][name],
                          headers=headers, expected_status=expected)
        after = self.drained(name+'-after')
        if name == 'missing':
            suffix = '{client="'+encrypted.CLIENT+'"}'
            require(after['q4t_client_rate_admitted_total'+suffix] -
                    before.get('q4t_client_rate_admitted_total'+suffix, 0) == 1 and
                    after['q4t_client_rate_reserved_tokens'+suffix] == 0 and
                    after.get('q4t_prompt_tokens_total', 0) ==
                    before.get('q4t_prompt_tokens_total', 0) and
                    after.get('q4t_execution_steps_total', 0) ==
                    before.get('q4t_execution_steps_total', 0), 'failed load work charged')
        else:
            tokens = generic.meter(before, after, admitted=0 if expected else 1,
                client=LIMITED_CLIENT if name == 'quota' else encrypted.CLIENT)
            if not expected:
                require(result['usage']['prompt_tokens'] == tokens, 'actual input usage mismatch')
        if expected:
            require(read(self.out/(name+'.sdk.json'))['error']['code'] ==
                    ('embedding_assets_invalid' if name == 'missing' else
                     'client_tpm_exhausted'), 'wrong isolated failure')
        return result

    def chat(self, label):
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', self.plan['requests']['chat'],
                                label=label)
        require(status == 200, 'healthy Chat failed')
        require(json.loads(raw)['choices'][0]['message']['content'].strip() == '42',
                'real Chat quality')
        self.same_model()
        return self.drained(label+'-drain')

    def business(self):
        self.embed('missing', 503)
        failed_at = time.monotonic()
        health = self.observation('missing-health', 'degraded')
        require(health['reason'] == 'embedding_assets_invalid' and
                health['load_failures'] == 1 and health['device_bytes'] == 0,
                'missing weight failure not isolated')
        first_chat = self.chat('chat-during-failure')
        before_steps = first_chat['q4t_execution_steps_total']
        os.link(Path(self.plan['assets'])/'model.safetensors',
                self.out/'optional-assets/model.safetensors')
        time.sleep(max(0, failed_at+5.1-time.monotonic()))
        value = self.embed('default')
        full = vectors(value, 11, 1024)
        expected_tokens = 2 + sum(len(row['ids']) for row in corpus()[4:])
        require(value['usage']['prompt_tokens'] == expected_tokens, 'trained EOS/token usage')
        recovered = self.observation('recovered-health', 'ready')
        require(recovered['load_failures'] == 1 and recovered['load_attempts'] == 3 and
                recovered['device_bytes'] > 0,
                'request driven recovery absent')
        save(self.out/'retrieval.json', dict(cases=retrieval(full), top1_correct=5,
            reference_final_vectors=reference_vectors(full), source='original actual corpus'))
        require(len({struct.pack('<1024f', *row) for row in full}) == 11,
                'native backend yielded identical vectors')
        ids = vectors(self.embed('ids'), 1, 1024)[0]
        require(struct.pack('<1024f', *full[0]) == struct.pack('<1024f', *ids),
                'native trained text/raw EOS differ')
        self.embed('quota', 429)
        models = json.loads(self.request('GET', '/v1/models', label='models'))
        require(MODEL in [row['id'] for row in models['data']], 'native model undiscoverable')
        model = json.loads(self.request('GET', '/v1/models/'+MODEL, label='model'))
        require(model['id'] == MODEL, 'native model detail mismatch')
        batch = self.finish('batch', self.create('batch', '/v1/embeddings', requests()['batch']))
        batches.check_batch(batch, 1, 1, 0)
        body = self.outputs('batch-output', batch, ['batch-0'])[0]['response']['body']
        reduced_batch = vectors(body, 1, 32)[0]
        require(batch['usage']['input_tokens'] == body['usage']['prompt_tokens'] == 2 and
                batch['usage']['output_tokens'] == 0, 'native Batch actual usage')
        before = self.drained('cancel-before')['q4t_execution_steps_total']
        pending = self.create('cancel', '/v1/embeddings', requests()['cancel'])
        end = time.monotonic()+90
        while time.monotonic() < end:
            status, raw = self.http('/metrics')
            require(status == 200, 'cancel progress metrics')
            if parse_metrics(raw)['q4t_execution_steps_total'] > before: break
            self.same_model()
            time.sleep(.005)
        else: raise TimeoutError('native lookup did not commit')
        self.request('POST', '/v1/batches/'+pending['id']+'/cancel', b'{}', label='cancel-request')
        cancelled = self.finish('cancel', pending)
        used = batches.check_batch(cancelled, 1, 0, 1, 'cancelled')
        require(used['input_tokens'] == 8192 and used['output_tokens'] == 0,
                'cancel did not retain exactly the committed first lookup')
        failure = self.outputs('cancel-error', cancelled, ['cancel-0'], 'error_file_id')[0]
        require(failure['response']['status_code'] in (409, 499) and
                'data' not in failure['response']['body'], 'partial cancelled vectors escaped')
        metric = self.batch_idle('before-restart')
        suffix = '{client="'+encrypted.CLIENT+'"}'
        require(metric['q4t_client_batch_rate_admitted_total'+suffix] == 2 and
                metric['q4t_client_batch_rate_reserved_tokens'+suffix] == 0 and
                metric['q4t_client_batch_rate_settled_tokens_total'+suffix] == 8194,
                'native Batch meter/refund')
        first_steps = metric['q4t_execution_steps_total']-before_steps
        retained = [(batch, 'output_file_id', self.out/'batch-output.wire'),
                    (cancelled, 'error_file_id', self.out/'cancel-error.wire')]
        self.cleanup()
        cleanup = read(self.out/'cleanup.json')
        save(self.out/'first-cleanup.json', cleanup)
        require(cleanup['model_exit'] == 0, 'first model did not stop normally')
        self.model = None
        self.start()
        for index, (original, key, file) in enumerate(retained):
            current = json.loads(self.request('GET', '/v1/batches/'+original['id'],
                                             label='retained-'+str(index)))
            require(current == original, 'original Batch changed after restart')
            raw = self.request('GET', '/v1/files/'+original[key]+'/content',
                               label='retained-file-'+str(index))
            require(raw == file.read_bytes(), 'original Batch bytes changed')
        reduced = vectors(self.embed('reduced'), 1, 32)[0]
        require(struct.pack('<32f', *reduced) == struct.pack('<32f', *reduced_batch),
                'online/Batch/restart native embedding differs')
        norm = math.sqrt(math.fsum(v*v for v in ids[:32]))
        require(max(abs(a-b/norm) for a,b in zip(reduced, ids[:32])) < 2e-7,
                'MRL prefix normalization differs')
        final = self.drained('embedding-final')
        steps = first_steps+final['q4t_execution_steps_total']
        require(steps <= MAX_EMBEDDING_STEPS, 'native step budget')
        self.chat('chat-after-restart')
        save(self.out/'acceptance.json', dict(status='ACCEPTED', embedding_steps=steps,
            cancelled_usage=used, same_pid_asset_recovery=True, chat_answers=['42','42'],
            batch_restart_original=True, retrieval_top1=5, default_unchanged=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    for name in ['binary', 'assets', 'output']:
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19067)
    p = sub.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        def timeout(_signal, _frame): raise TimeoutError('native business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()
