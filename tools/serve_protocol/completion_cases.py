"""A06 frozen Completions input cases for schema/native differential checks."""
import json


def cases():
    rows = []

    def add(label, request, accepted=True, *, semantic=False, raw=None, **config):
        rows.append(dict(id='completion-'+label, expected=accepted,
            shape_expected=None if semantic else accepted, field=None,
            fixture=dict(kind='completion', raw=json.dumps(request,
                ensure_ascii=False) if raw is None else raw, **config)))

    base = dict(model='qwen3.8-flash-next')
    add('defaults', base)
    for label, prompt in [('null', None), ('empty', ''), ('text', '<think>原文'),
        ('ids', [10, 11]), ('empty-ids', []), ('strings', ['甲', '乙']),
        ('batches', [[1, 2], [], [3]])]:
        add('prompt-'+label, dict(base, prompt=prompt))
    add('all-options', dict(base, prompt='e\u0301', best_of=3, n=2,
        echo=True, suffix='尾', max_tokens=0, logprobs=5, temperature=0.75,
        top_p=0.9, seed=-9223372036854775808, frequency_penalty=0.2,
        presence_penalty=-1, logit_bias={'12': 100}, stop=['end'], user='u',
        request_id='completion-case', cancel_token='a'*64,
        request_timeout_ms=500))
    add('nullable-options', dict(base, **{k: None for k in (
        'prompt', 'best_of', 'n', 'echo', 'suffix', 'max_tokens', 'logprobs',
        'stream', 'stream_options', 'temperature', 'top_p', 'seed',
        'frequency_penalty', 'presence_penalty', 'logit_bias', 'stop', 'user')}))
    add('stream', dict(base, stream=True, n=2, best_of=2,
        stream_options=dict(include_usage=True, include_obfuscation=False)))
    add('alias', dict(base, model='org/alias'), model_aliases=['org/alias'])
    add('model-unknown', dict(base, model='missing'), False, semantic=True)
    add('model-missing', {}, False)
    for label, value in [('null', None), ('empty', ''), ('space', 'has space'),
                         ('oversize', '中'*100)]:
        add('model-'+label, dict(base, model=value), False)
    for key, value in [('max_tokens', -1), ('max_tokens', 1.5), ('logprobs', 6),
        ('logprobs', True), ('n', 0), ('best_of', 129), ('echo', 1),
        ('stream', 'yes'), ('suffix', []), ('seed', 9223372036854775808),
        ('temperature', 2.1), ('top_p', -0.1), ('frequency_penalty', 3),
        ('presence_penalty', -3), ('logit_bias', {'1': 101}), ('stop', [''] ),
        ('user', {}), ('stream_options', {'wrong': True}),
        ('request_timeout_ms', 0), ('unknown', True)]:
        add('invalid-'+key+'-'+str(len(rows)), dict(base, **{key: value}), False)
    for label, prompt, semantic in [('negative', [-1], False),
        ('fractional', [1.2], False), ('bool', [True], False),
        ('mixed', ['x', [1]], False), ('dict', {}, False),
        ('vocab', [10], True), ('batch-candidates', ['a', 'b'], True)]:
        config = dict(token_vocab=10) if label == 'vocab' else {}
        request = dict(base, prompt=prompt)
        if label == 'batch-candidates': request['best_of'] = 128
        add('prompt-invalid-'+label, request, False, semantic=semantic, **config)
    add('best-of-less-n', dict(base, n=2, best_of=1), False, semantic=True)
    add('best-of-stream', dict(base, best_of=2, stream=True), False, semantic=True)
    add('options-without-stream', dict(base, stream_options={}), False)
    add('orphan-id', dict(base, request_id='x'), False)
    add('reserved-id', dict(base, request_id='cmpl-auto-x', cancel_token='a'*64), False)
    add('duplicate', None, False, raw='{"model":"qwen3.8-flash-next","n":1,"n":2}')
    return rows
