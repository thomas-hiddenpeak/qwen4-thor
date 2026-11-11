"""Native final-hidden embeddings, standard wire shape and pinned SDK sources."""
import hashlib
from pathlib import Path

SDK_SOURCES = {
    'src/resources/embeddings.ts':
        '5c63eb78df7c5d1870a83df3fefd77f4cb33d26e0ca55e0eb68dba275a675959',
    'src/lib/embeddings.ts':
        '7a41271270066c9021c83e8d53887c7b692700ce159bcbbfebc2869df84745cc',
}


def definitions():
    from tools.serve_responses.build_contract import obj, ref
    text = dict(type='string', minLength=1)
    tokens = dict(type='array', minItems=1, maxItems=8192,
                  items=dict(type='integer', minimum=0))
    request = obj(dict(model=dict(type='string', minLength=1),
        input=dict(anyOf=[text, tokens,
            dict(type='array', minItems=1, maxItems=2048, items=text),
            dict(type='array', minItems=1, maxItems=2048, items=tokens)]),
        dimensions=dict(type='integer', minimum=1, maximum=2560),
        encoding_format=dict(enum=['float', 'base64']),
        user=dict(type='string', maxLength=4096)), ['model', 'input'])
    vector = obj(dict(object=dict(const='embedding'),
        index=dict(type='integer', minimum=0, maximum=2047),
        embedding=dict(anyOf=[dict(type='array', minItems=1, maxItems=2560,
            items=dict(type='number')), dict(type='string', minLength=8,
            maxLength=13656, pattern=r'^[A-Za-z0-9+/]*={0,2}$')])),
        ['object', 'index', 'embedding'])
    usage = obj(dict(prompt_tokens=dict(type='integer', minimum=1, maximum=300000),
        total_tokens=dict(type='integer', minimum=1, maximum=300000)),
        ['prompt_tokens', 'total_tokens'])
    response = obj(dict(object=dict(const='list'), model=dict(type='string', minLength=1),
        data=dict(type='array', minItems=1, maxItems=2048, items=ref('embedding')),
        usage=usage), ['object', 'model', 'data', 'usage'])
    return dict(embeddings_create=request, embeddings_response=response,
                embedding=vector)


def source(sdk):
    paths = []
    for name, sha in SDK_SOURCES.items():
        path = Path(sdk)/'node_modules/openai'/name
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('embeddings SDK source drift: '+name)
        paths.append(path)
    return paths


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    paths['/v1/embeddings'] = dict(post=dict(operationId='v1_embeddings',
        requestBody=dict(required=True, content={'application/json':dict(
            schema=external('embeddings_create'))}),
        responses={'200':response(external('embeddings_response')),
                   'default':response(error, 'Finite native embedding error')},
        **{'x-q4t':dict(executor='actual_final_mixer_hidden', pooling='last_input_token',
            dimensions=2560, dimension_selection='prefix_then_l2',
            quality='generative hidden states; no retrieval or Matryoshka training claim',
            input='raw text or token IDs; no chat template or truncation',
            max_total_input_tokens=300000, max_response_bytes=100663296,
            base64='little-endian IEEE754 float32',
            quota='one RPM, cumulative committed input tokens, no output tokens',
            batch=True, stream=False)}))
