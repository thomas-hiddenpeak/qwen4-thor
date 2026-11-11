"""Native embeddings: 2 starts/8 requests/18 embedding chunks/900 seconds.

One synthetic final-mixer numerical check precedes real model business under
the same strict GPU lease. No full performance/quality matrix or default change.
"""
import argparse
import base64
import json
import math
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import batches_acceptance as batches
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses.test_contract import errors
from tools.serve_grammar_reap.acceptance import parse_metrics
from tools.serve_service.package import read, require, save, sha

foreground = encrypted.foreground
LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
MAX_EMBEDDING_CHUNKS = 18
PRIMITIVE = 'model_head_hidden_matches_generation_final_mixer'
LIMITED_CLIENT = 'embeddings-limited'
DOCUMENTS = [('docs/ARCHITECTURE.md', 32, 45),
             ('docs/ARCHITECTURE.md', 133, 140),
             ('docs/EVALUATION.md', 8, 14)]


def project_documents():
    return ['\n'.join((ROOT/path).read_text().splitlines()[start-1:end])
            for path, start, end in DOCUMENTS]


def requests():
    common = dict(model=foreground.MODEL)
    return dict(default=dict(common, input=['!'] + project_documents() + [
        '是否必须为 q4t 安装或打包 nginx 才能提供服务？',
        '不涉及性能的功能修复需要运行完整性能矩阵吗？']),
        ids=dict(common, input=[[0]], encoding_format='float'),
        reduced=dict(common, input=[0], dimensions=32, encoding_format='base64'),
        invalid=dict(common, input=[-1]),
        quota=dict(common, input=[0]*17),
        batch=[dict(common, input=['!', project_documents()[0]], dimensions=32,
                    encoding_format='base64')],
        cancel=[dict(common, input=[[0]*8192, [0]*8192], dimensions=32)],
        chat=dict(foreground.requests()['chat'], max_completion_tokens=64))


def vectors(value, count, dimensions):
    require(not errors(value, 'embeddings_response'), 'embedding response schema')
    require(value['model'] == foreground.MODEL and len(value['data']) == count,
            'actual model/cardinality mismatch')
    usage = value['usage']
    require(type(usage['prompt_tokens']) is int and usage['prompt_tokens'] > 0 and
            usage['total_tokens'] == usage['prompt_tokens'], 'embedding token usage')
    result = []
    for index, row in enumerate(value['data']):
        require(row['index'] == index, 'embedding order')
        encoded = row['embedding']
        if isinstance(encoded, str):
            raw = base64.b64decode(encoded, validate=True)
            require(len(raw) == dimensions*4, 'base64 FP32 byte count')
            vector = struct.unpack('<' + str(dimensions) + 'f', raw)
        else:
            vector = encoded
        require(len(vector) == dimensions and all(math.isfinite(v) for v in vector),
                'embedding dimensions/nonfinite values')
        require(abs(math.fsum(v*v for v in vector)-1) < 2e-6,
                'embedding L2 normalization')
        result.append(list(vector))
    return result


def meter(before, after, *, admitted=1, client=encrypted.CLIENT):
    suffix = '{client="'+client+'"}'
    delta = lambda key: after.get(key, 0)-before.get(key, 0)
    require(delta('q4t_client_rate_admitted_total'+suffix) == admitted and
            after['q4t_client_rate_reserved_tokens'+suffix] == 0,
            'embedding RPM/refund mismatch')
    tokens = delta('q4t_prompt_tokens_total')
    require(delta('q4t_generation_tokens_total') == 0 and
            delta('q4t_client_rate_settled_tokens_total'+suffix) == tokens,
            'embedding actual input/output accounting')
    require(tokens > 0 if admitted else tokens == 0, 'embedding execution mismatch')
    return tokens


def retrieval_observation(rows):
    result = []
    for query, target in [(4, 1), (5, 2)]:
        scores = [math.fsum(a*b for a,b in zip(rows[query], rows[index]))
                  for index in range(1, 4)]
        order = sorted(range(3), key=lambda i: scores[i], reverse=True)
        result.append(dict(query=query, expected_document=target, ranking=order,
                           cosine_scores=scores, top1_correct=order[0] == target))
    return dict(documents=DOCUMENTS, cases=result,
                top1_correct=sum(row['top1_correct'] for row in result),
                total=2, trained_retrieval_quality_claim=False)


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name, _, _ in DOCUMENTS:
        plan['sources'][name] = sha(ROOT/name)
    tests = args.tests.resolve(strict=True)
    require(tests.parent == Path(plan['binary']).parent, 'GPU test build differs')
    required = path.parent/'gpu-required.txt'
    required.write_text(PRIMITIVE+'\n')
    tokenizer = read(Path(plan['model'])/'tokenizer.json')
    require(tokenizer['model']['vocab']['!'] == 0, 'raw token fixture changed')
    plan['gpu_test'] = dict(binary=str(tests), sha256=sha(tests),
                           required=str(required), required_sha256=sha(required))
    plan['planned'] = dict(starts=2, requests=8, embedding_chunks_max=18,
        primitive_cases=1, chat_output_tokens_max=64)
    plan['mutations'] = ['isolated state; cancel original Batch after committed prefill',
        'graceful restart once; original Batch/result bytes retained; own cleanup only']
    plan['derived_requests'] = ['actual project paragraphs; known raw ! token0',
        'default SDK base64 decode, explicit float, prefix normalization and base64',
        'real Batch input usage/cancel refund; no partial cancelled vectors',
        'read same original objects after restart; same current model serves Chat',
        'two retrieval observations separate from interface/numerical acceptance']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_EMBEDDINGS_BUSINESS'
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
        return ['--object-max-client-mib', '256', '--object-max-total-mib', '512']

    def start(self):
        if self.counts['starts'] == 0:
            test = self.plan['gpu_test']
            require(sha(test['binary']) == test['sha256'] and
                    sha(test['required']) == test['required_sha256'], 'GPU test identity')
            started = time.monotonic()
            with (self.out/'primitive.stdout').open('xb') as out, \
                 (self.out/'primitive.stderr').open('xb') as err:
                run = subprocess.run([test['binary'], '--required-list', test['required']],
                    cwd=ROOT, stdout=out, stderr=err, pass_fds=(self.lease,), timeout=60)
            save(self.out/'primitive.json', dict(exit_code=run.returncode,
                seconds=time.monotonic()-started, test=PRIMITIVE))
            require(run.returncode == 0, 'final-mixer primitive failed')
        super().start()

    def embed(self, name, expected=None):
        before = self.drained(name+'-before')
        self.budget('chats')
        headers = dict(encrypted.HEADERS)
        if name == 'quota': headers['X-Q4T-Client-Id'] = LIMITED_CLIENT
        result = self.sdk(name, 'embeddings_create', requests()[name],
                          headers=headers, expected_status=expected)
        tokens = meter(before, self.drained(name+'-after'),
            admitted=0 if expected else 1,
            client=LIMITED_CLIENT if name == 'quota' else encrypted.CLIENT)
        if expected:
            require(read(self.out/(name+'.sdk.json'))['error']['code'] ==
                    ('invalid_request' if name == 'invalid' else 'client_tpm_exhausted'),
                    'wrong embedding error code')
        else:
            require(result['usage']['prompt_tokens'] == tokens, 'wire/actual token mismatch')
        return result

    def business(self):
        full = vectors(self.embed('default'), 6, 2560)
        ids = vectors(self.embed('ids'), 1, 2560)[0]
        require(struct.pack('<2560f', *full[0]) == struct.pack('<2560f', *ids),
                'raw text/token ID embeddings differ')
        require(len({struct.pack('<2560f', *row) for row in full}) == 6,
                'different inputs yielded identical vectors')
        save(self.out/'retrieval-observation.json', retrieval_observation(full))
        self.embed('invalid', 400)
        self.embed('quota', 429)
        batch = self.finish('batch', self.create('batch', '/v1/embeddings', requests()['batch']))
        batches.check_batch(batch, 1, 1, 0)
        body = self.outputs('batch-output', batch, ['batch-0'])[0]['response']['body']
        batch_vectors = vectors(body, 2, 32)
        require(batch['usage']['input_tokens'] == body['usage']['prompt_tokens'] and
                batch['usage']['output_tokens'] == 0, 'Batch embedding usage')
        before = self.drained('cancel-before')['q4t_execution_steps_total']
        pending = self.create('cancel', '/v1/embeddings', requests()['cancel'])
        end = time.monotonic()+90
        while time.monotonic() < end:
            status, raw = self.http('/metrics')
            require(status == 200, 'cancel progress metrics')
            if parse_metrics(raw)['q4t_execution_steps_total'] > before: break
            self.same_model()
            time.sleep(.05)
        else: raise TimeoutError('embedding prefill did not commit')
        self.request('POST', '/v1/batches/'+pending['id']+'/cancel', b'{}', label='cancel-request')
        cancelled = self.finish('cancel', pending)
        used = batches.check_batch(cancelled, 1, 0, 1, 'cancelled')
        require(0 < used['input_tokens'] < 16384 and used['output_tokens'] == 0,
                'cancel did not preserve partial actual input work')
        failure = self.outputs('cancel-error', cancelled, ['cancel-0'], 'error_file_id')[0]
        require(failure['response']['status_code'] in (409, 499) and
                'data' not in failure['response']['body'], 'partial vectors escaped cancellation')
        metric = self.batch_idle('before-restart')
        suffix = '{client="'+encrypted.CLIENT+'"}'
        require(metric['q4t_client_batch_rate_admitted_total'+suffix] == 2 and
            metric['q4t_client_batch_rate_reserved_tokens'+suffix] == 0 and
            metric['q4t_client_batch_rate_settled_tokens_total'+suffix] ==
                batch['usage']['total_tokens']+used['total_tokens'], 'Batch meter/refund')
        first_chunks = metric['q4t_execution_steps_total']
        require(first_chunks < MAX_EMBEDDING_CHUNKS, 'embedding chunk bound')
        retained = [(batch, 'output_file_id', self.out/'batch-output.wire'),
                    (cancelled, 'error_file_id', self.out/'cancel-error.wire')]
        self.cleanup()
        cleanup = read(self.out/'cleanup.json')
        save(self.out/'first-cleanup.json', cleanup)
        require(cleanup['model_exit'] == 0, 'embedding process did not stop normally')
        self.model = None
        self.start()
        for index, (original, key, file) in enumerate(retained):
            current = json.loads(self.request('GET', '/v1/batches/'+original['id'],
                                             label='retained-'+str(index)))
            require(current == original, 'original Batch changed after restart')
            raw = self.request('GET', '/v1/files/'+original[key]+'/content',
                               label='retained-file-'+str(index))
            require(raw == file.read_bytes(), 'original vector bytes changed')
        reduced = vectors(self.embed('reduced'), 1, 32)[0]
        require(struct.pack('<32f', *reduced) == struct.pack('<32f', *batch_vectors[0]),
                'same raw input differs between online and Batch/restart')
        norm = math.sqrt(math.fsum(v*v for v in ids[:32]))
        require(max(abs(a-b/norm) for a,b in zip(reduced, ids[:32])) < 2e-7,
                'prefix then normalization differs')
        final = self.drained('embedding-final')
        chunks = first_chunks + final['q4t_execution_steps_total']
        require(chunks <= MAX_EMBEDDING_CHUNKS, 'total embedding chunk bound')
        self.budget('chats')
        status, raw = self.http('/v1/chat/completions', requests()['chat'], label='chat')
        require(status == 200, 'Chat after embeddings failed')
        chat = json.loads(raw)
        require(chat['choices'][0]['message']['content'].strip() == '42', 'actual Chat answer')
        self.drained('chat-final')
        save(self.out/'acceptance.json', dict(status='ACCEPTED', embedding_chunks=chunks,
            cancelled_usage=used, exact_text_token_id_match=True, batch_restart_original=True,
            chat_answer='42', retrieval_quality_separate=True, default_unchanged=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    p = commands.add_parser('prepare')
    for name in ('binary', 'tests', 'output'): p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19065)
    p = commands.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    else:
        def timeout(_signal, _frame): raise TimeoutError('embedding business deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with foreground.base.lifecycle.child_subreaper(): Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()
