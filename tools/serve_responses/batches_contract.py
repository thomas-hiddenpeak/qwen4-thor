"""Native Batch envelopes and HTTP routes; pinned SDK input contract."""
import hashlib
from pathlib import Path

SDK_BATCHES_SHA256 = '71e29b0995aab78190942e05642fbd37c082a3fcadd5005f3eb57b97d417e164'
ENDPOINTS = ['/v1/responses', '/v1/chat/completions', '/v1/embeddings',
             '/v1/completions', '/v1/moderations', '/v1/images/generations',
             '/v1/images/edits', '/v1/videos']


def definitions():
    from tools.serve_responses.build_contract import obj, ref, nullable
    identity = dict(type='string', minLength=1, maxLength=128,
                    pattern='^[A-Za-z0-9_-]+$')
    timestamp = dict(type='integer', minimum=0)
    metadata = nullable(dict(type='object', maxProperties=16,
        propertyNames=dict(minLength=1, maxLength=64),
        additionalProperties=dict(type='string', maxLength=512)))
    create = obj(dict(input_file_id=identity, endpoint=dict(enum=ENDPOINTS),
        completion_window=dict(const='24h'), metadata=metadata,
        output_expires_after=obj(dict(anchor=dict(const='created_at'),
            seconds=dict(type='integer', minimum=3600, maximum=2592000)),
            ['anchor', 'seconds'])), ['input_file_id', 'endpoint', 'completion_window'])
    counts = obj({name:dict(type='integer', minimum=0, maximum=50000)
                  for name in ['total', 'completed', 'failed']},
                  ['total', 'completed', 'failed'])
    error = obj(dict(code=dict(type='string'), message=dict(type='string'),
        line=nullable(dict(type='integer', minimum=1)), param=nullable(dict(type='string'))))
    usage = obj(dict(input_tokens=dict(type='integer', minimum=0),
        output_tokens=dict(type='integer', minimum=0), total_tokens=dict(type='integer', minimum=0),
        input_tokens_details=obj(dict(cached_tokens=dict(type='integer', minimum=0)), ['cached_tokens']),
        output_tokens_details=obj(dict(reasoning_tokens=dict(type='integer', minimum=0)), ['reasoning_tokens'])),
        ['input_tokens', 'output_tokens', 'total_tokens', 'input_tokens_details', 'output_tokens_details'])
    properties = dict(id=identity, object=dict(const='batch'), endpoint=dict(enum=ENDPOINTS),
        input_file_id=identity, completion_window=dict(const='24h'), created_at=timestamp,
        status=dict(enum=['validating', 'failed', 'in_progress', 'finalizing',
                          'completed', 'expired', 'cancelling', 'cancelled']),
        metadata=metadata, model=dict(type='string'), request_counts=counts, usage=usage,
        output_file_id=nullable(identity), error_file_id=nullable(identity),
        errors=nullable(obj(dict(object=dict(const='list'), data=dict(type='array', items=error)),
                            ['object', 'data'])))
    properties.update({name+'_at':timestamp for name in ['cancelled', 'cancelling',
        'completed', 'expired', 'expires', 'failed', 'finalizing', 'in_progress']})
    resource = obj(properties, ['id', 'object', 'endpoint', 'input_file_id',
        'completion_window', 'created_at', 'status'])
    query = obj(dict(after=identity, limit=dict(type='integer', minimum=1, maximum=100, default=20)))
    listing = obj(dict(object=dict(const='list'), data=dict(type='array', maxItems=100,
        items=ref('batch_resource')), first_id=nullable(dict(type='string')),
        last_id=nullable(dict(type='string')), has_more=dict(type='boolean')),
        ['object', 'data', 'first_id', 'last_id', 'has_more'])
    return dict(batch_create=create, batch_resource=resource, batch_query=query,
                batch_list=listing, batch_cancel=obj({}))


def source(sdk):
    path = Path(sdk)/'node_modules/openai/src/resources/batches.ts'
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != SDK_BATCHES_SHA256:raise ValueError('pinned Batch SDK source changed')
    return dict(sdk_version='7.47.0', source='src/resources/batches.ts', sha256=actual)


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    for suffix, method, request, result in [('', 'post', 'batch_create', 'batch_resource'),
        ('', 'get', None, 'batch_list'), ('/{batch_id}', 'get', None, 'batch_resource'),
        ('/{batch_id}/cancel', 'post', 'batch_cancel', 'batch_resource')]:
        path = '/v1/batches'+suffix
        parameters = ([dict(name='batch_id', **{'in':'path'}, required=True,
                            schema=dict(type='string', minLength=1))] if suffix else [])
        if method == 'get' and not suffix:
            parameters += [dict(name=name, **{'in':'query'}, required=False, schema=value)
                           for name, value in definitions()['batch_query']['properties'].items()]
        operation = dict(operationId=method+'_'+path.strip('/').replace('/', '_').replace('{','').replace('}',''),
            parameters=parameters, responses={'200':response(external(result)), 'default':response(error)},
            description='Native client-scoped asynchronous task; input frozen before execution, independent Batch minute pool; original deadline and no started-row replay.',
            **{'x-q4t-limits':dict(input_bytes=200000000, requests=50000, window_seconds=86400,
                queue_tokens='configured per model and per authenticated client/model', storage_quota_separate=True)})
        if request:
            operation['requestBody'] = dict(required=not suffix.endswith('/cancel'),
                content={'application/json':dict(schema=external(request))})
        paths.setdefault(path,{})[method] = operation
