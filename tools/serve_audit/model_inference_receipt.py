"""Verify a single stock client's model discovery, completion stream and embeddings."""
import math
import struct

from tools.serve_responses.embeddings_acceptance import vectors


def verify(record, config, sdk):
    def require(condition, message):
        if not condition:
            raise ValueError(f'{sdk}: {message}')

    model = config['model']
    meta = record['model']
    require(record['sdk'] == sdk and meta['object'] == 'model' and meta['id'] == model
            and isinstance(meta['owned_by'], str) and meta['owned_by'].strip()
            and type(meta['created']) is int and meta['created'] >= 0,
            'retrieved model identity differs')
    matches = [m for m in record['catalogue'] if m['id'] == model]
    require(len(matches) == 1 and all(matches[0][k] == meta[k] for k in ('id', 'object', 'owned_by', 'created')),
            'model list and detail disagree')
    rows = record['requests']
    route = '/v1/models/'
    expected = [('GET','/v1/models',200), ('GET',route+model,200),
                ('GET',route+'q4t-missing-business-model',404),
                ('POST','/v1/completions',200), ('POST','/v1/completions',200),
                ('POST','/v1/embeddings',200), ('POST','/v1/embeddings',200)]
    require([(r['method'], r['path'], r['status']) for r in rows] == expected
            and all(r['query'] == '' and r['trace'] for r in rows), 'unexpected path, status or request ID')
    traces = {r['trace'] for r in rows}
    require(len(traces) == len(rows), 'repeated request ID')
    completion = record['completion']
    require(completion['object'] == 'text_completion' and completion['model'] == model
            and len(completion['choices']) == 1 and completion['id'], 'unexpected completion envelope')
    choice = completion['choices'][0]
    prompt = config['completion']['prompt']
    require(choice['index'] == 0 and choice['text'].startswith(prompt) and choice['text'][len(prompt):].strip()
            and choice['finish_reason'] in ('length','stop'), 'missing echoed prompt or actual completion')
    usage = completion['usage']
    require(usage['prompt_tokens'] > 0 and 0 < usage['completion_tokens'] <= config['completion']['max_tokens']
            and usage['total_tokens'] == usage['prompt_tokens']+usage['completion_tokens'],
            'invalid completion token usage')
    chunks = record['chunks']
    require(len(chunks) >= 2 and chunks[-1]['choices'] == [] and chunks[-1]['usage'] == usage
            and chunks[0]['id'] and chunks[0]['id'] != completion['id']
            and all((c['id'], c['object'], c['model'], c['created']) ==
                    (chunks[0]['id'], 'text_completion', model, chunks[0]['created']) for c in chunks),
            'invalid completion stream identity or final usage')
    pieces = [c for frame in chunks[:-1] for c in frame['choices']]
    require(all(not c.get('usage') and len(c['choices']) == 1 for c in chunks[:-1])
            and all(c['index'] == 0 for c in pieces)
            and not any(c.get('finish_reason') for c in pieces[:-1])
            and pieces[-1]['finish_reason'] == choice['finish_reason']
            and ''.join(c['text'] for c in pieces) == choice['text'], 'stream content or termination changed')
    logs = choice['logprobs']
    require(logs and len(logs['tokens']) == len(logs['text_offset']) == len(logs['token_logprobs'])
            == len(logs['top_logprobs']) and len(logs['tokens']) > 0, 'missing completion logprobs')
    offsets = logs['text_offset']
    require(offsets[0] == 0 and all(type(offset) is int and 0 <= offset <= len(choice['text'])
                                    for offset in offsets)
            and all(a <= b for a, b in zip(offsets, offsets[1:])), 'invalid character offsets')
    def log_probability(value):
        return type(value) in (float, int) and math.isfinite(value) and value <= 0

    require(all(isinstance(token, str) for token in logs['tokens'])
            and logs['token_logprobs'][0] is None and logs['top_logprobs'][0] is None,
            'invalid echoed first-token observation')
    require(all(log_probability(value) for value in logs['token_logprobs'][1:])
            and all(isinstance(row, dict) and all(log_probability(value) for value in row.values())
                    for row in logs['top_logprobs'][1:]), 'invalid token log probability')
    for key in ('tokens', 'text_offset', 'token_logprobs', 'top_logprobs'):
        combined = [value for c in pieces if c.get('logprobs') for value in c['logprobs'][key]]
        require(combined == logs[key], f'streamed {key} differs from JSON')
    tensors = []
    for encoding, expected_type in [('float', list), ('base64', str)]:
        response = record['embeddings'][encoding]
        require(all(isinstance(x['embedding'], expected_type) for x in response['data']),
                'embedding response uses the wrong encoding')
        rows = vectors(response, len(config['documents']), config['dimensions'])
        tensors.append([struct.pack('<'+'f'*len(row), *row) for row in rows])
    require(tensors[0] == tensors[1] and len(set(tensors[0])) == len(config['documents']),
            'embedding content or row correspondence differs between encodings')
    return traces
