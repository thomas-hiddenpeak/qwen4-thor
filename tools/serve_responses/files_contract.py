"""Files wire shapes, pinned to the same official SDK as application contracts.

Multipart is represented explicitly; it is not a JSON upload or a runtime
dependency on SDK/TypeScript. The upstream evals output-enum omission is kept
as a documented wire correction, never changed into a rejected input.
"""
import hashlib
from pathlib import Path

PURPOSES = ['assistants', 'batch', 'fine-tune', 'vision', 'user_data', 'evals']
FILE_BYTES = 512*1024**2
WIRE_BYTES = FILE_BYTES+65536
SDK_FILES_SHA256 = 'ca78d34f73bef9a683b5fb2bbeb57af219478027b54ee243b611a1d4a12e04d7'


def definitions():
    from tools.serve_responses.build_contract import obj, ref, nullable
    identity = dict(type='string', minLength=1)
    expiry = obj(dict(anchor=dict(const='created_at'),
                      seconds=dict(type='integer', minimum=3600, maximum=2592000)),
                 ['anchor', 'seconds'])
    resource = obj(dict(id=identity, object=dict(const='file'),
        bytes=dict(type='integer', minimum=0), created_at=dict(type='integer', minimum=0),
        filename=dict(type='string', minLength=1), purpose=dict(enum=PURPOSES),
        status=dict(enum=['uploaded', 'processed', 'error']),
        expires_at=dict(type='integer', minimum=0), status_details=dict(type='string')),
        ['id', 'object', 'bytes', 'created_at', 'filename', 'purpose', 'status'])
    upload = obj(dict(file=dict(type='string', format='binary'),
                      purpose=dict(enum=PURPOSES), expires_after=expiry), ['file', 'purpose'])
    multipart = obj({'file': dict(type='string', format='binary'),
        'purpose': dict(enum=PURPOSES),
        'expires_after[anchor]': dict(const='created_at'),
        'expires_after[seconds]': dict(type='string', pattern='^[0-9]+$')}, ['file', 'purpose'])
    multipart['dependentRequired'] = {
        'expires_after[anchor]': ['expires_after[seconds]'],
        'expires_after[seconds]': ['expires_after[anchor]']}
    multipart['x-q4t-expiry-seconds'] = dict(minimum=3600, maximum=2592000)
    query = obj(dict(purpose=dict(type='string'),
                     after=identity, limit=dict(type='integer', minimum=1,
                                               maximum=10000, default=10000),
                     order=dict(enum=['asc', 'desc'], default='desc')))
    listing = obj(dict(object=dict(const='list'),
        data=dict(type='array', maxItems=10000, items=ref('file_resource')),
        has_more=dict(type='boolean'), first_id=nullable(identity), last_id=nullable(identity)),
        ['object', 'data', 'has_more', 'first_id', 'last_id'])
    deleted = obj(dict(id=identity, object=dict(const='file'), deleted=dict(const=True)),
                  ['id', 'object', 'deleted'])
    return dict(file_resource=resource, file_create=upload, file_multipart=multipart,
                file_query=query, file_list=listing, file_deleted=deleted)


def source(sdk):
    path = Path(sdk)/'node_modules/openai/src/resources/files.ts'
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != SDK_FILES_SHA256:raise ValueError('pinned Files SDK source changed')
    return dict(sdk_version='7.47.0', source='src/resources/files.ts', sha256=actual,
                output_enum_correction='FileObject.purpose includes accepted FilePurpose evals')


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    for path, method, result in [('/v1/files', 'post', 'file_resource'),
        ('/v1/files', 'get', 'file_list'),
        ('/v1/files/{file_id}', 'get', 'file_resource'),
        ('/v1/files/{file_id}', 'delete', 'file_deleted'),
        ('/v1/files/{file_id}/content', 'get', None)]:
        parameters = ([dict(name='file_id', **{'in': 'path'}, required=True,
                            schema=dict(type='string', minLength=1))]
                      if '{file_id}' in path else [])
        if path == '/v1/files' and method == 'get':
            parameters += [dict(name=name, **{'in': 'query'}, required=False, schema=value)
                           for name, value in definitions()['file_query']['properties'].items()]
        success = response(external(result)) if result else dict(
            description='Original binary bytes, bounded streaming with Content-Length',
            headers={'Content-Length': dict(schema=dict(type='integer', minimum=0))},
            content={'application/octet-stream': dict(schema=dict(type='string', format='binary'))})
        operation = dict(operationId=method+'_'+path.strip('/').replace('/', '_').replace('{','').replace('}',''),
            parameters=parameters, responses={'200': success, 'default': response(error)},
            description='Authenticated client-scoped durable file; unrelated clients receive 404.',
            **{'x-q4t-storage': 'Private bounded chunks, isolated filesystem worker, atomic publication; no automatic upload replay'})
        if method == 'post':
            operation['requestBody'] = dict(required=True, content={
                'multipart/form-data': dict(schema=external('file_multipart'))})
            operation['x-q4t-sdk-request-shape'] = external('file_create')
            operation['x-q4t-limits'] = dict(file_bytes=FILE_BYTES, wire_bytes=WIRE_BYTES,
                batch_bytes=200*1024**2, filename_utf8_bytes=1024,
                client_field='max_file_body_bytes', native_ingress_field='file_body_bytes',
                storage_quota='Separately configured; actual JSON/base64 overhead charged')
        paths.setdefault(path, {})[method] = operation
