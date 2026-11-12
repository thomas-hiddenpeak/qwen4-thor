"""Native vector store management wire contract, pinned official SDK shapes."""
import hashlib
from pathlib import Path

SDK_SOURCES = {
    'src/resources/vector-stores/vector-stores.ts':
        '0c00de16d61f78119003d7523c5c854c132c81bc08ef6b956f9f948e751721e9',
    'src/resources/vector-stores/files.ts':
        '66a73e8d347b3058eaeb1055eaa0cb9a7bf66e0414031039ecd583febf8ffc6e',
    'src/resources/vector-stores/file-batches.ts':
        '6bf7f8d32ecebb25dbf490e9ea193ae845240021aec4b977e3f333c7a63a8638',
}


def source(sdk):
    root = Path(sdk) / 'node_modules/openai'
    for name, expected in SDK_SOURCES.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != expected:
            raise ValueError('pinned vector store SDK source changed: '+name)
    return dict(sdk_version='7.47.0', sources_sha256=SDK_SOURCES)


def definitions():
    from tools.serve_responses.build_contract import obj, ref, nullable
    identity = dict(type='string', minLength=1, maxLength=128,
                    pattern='^[A-Za-z0-9_-]+$')
    file_id = dict(identity, pattern='^file_[A-Za-z0-9_-]+$')
    timestamp = dict(type='integer', minimum=0)
    text = dict(type='string', maxLength=4096)
    metadata = nullable(dict(type='object', maxProperties=16,
        propertyNames=dict(maxLength=64),
        additionalProperties=dict(type='string', maxLength=512)))
    attributes = nullable(dict(type='object', maxProperties=16,
        propertyNames=dict(type='string', maxLength=64),
        additionalProperties=dict(anyOf=[dict(type='string', maxLength=512),
                                        dict(type='number'), dict(type='boolean')])))
    policy = nullable(obj(dict(anchor=dict(const='last_active_at'),
        days=dict(type='integer', minimum=1, maximum=365)), ['anchor', 'days']))
    static = obj(dict(type=dict(const='static'), static=obj(dict(
        max_chunk_size_tokens=dict(type='integer', minimum=100, maximum=4096),
        chunk_overlap_tokens=dict(type='integer', minimum=0, maximum=2048)),
        ['max_chunk_size_tokens', 'chunk_overlap_tokens'])), ['type', 'static'])
    static['x-q4t-constraint'] = 'overlap <= floor(max_chunk_size_tokens/2)'
    chunking = nullable(dict(oneOf=[obj(dict(type=dict(const='auto')), ['type']), static]))
    create = obj(dict(name=text, description=text, metadata=metadata,
        expires_after=policy, file_ids=dict(type='array', maxItems=500,
        uniqueItems=True, items=file_id), chunking_strategy=chunking))
    update = obj(dict(name=nullable(text), metadata=metadata, expires_after=policy))
    attach = obj(dict(file_id=file_id, attributes=attributes,
                      chunking_strategy=chunking), ['file_id'])
    counts = obj({name:dict(type='integer', minimum=0, maximum=10000)
        for name in ['total', 'in_progress', 'completed', 'failed', 'cancelled']},
        ['total', 'in_progress', 'completed', 'failed', 'cancelled'])
    status = dict(enum=['in_progress', 'completed', 'failed', 'cancelled'])
    error = nullable(obj(dict(code=dict(enum=['server_error', 'unsupported_file',
        'invalid_file']), message=dict(type='string')), ['code', 'message']))
    store_fields = dict(id=identity, object=dict(const='vector_store'),
        created_at=timestamp, last_active_at=nullable(timestamp), name=nullable(text),
        description=text, metadata=metadata, expires_after=policy,
        expires_at=nullable(timestamp), file_counts=counts,
        status=dict(enum=['in_progress', 'completed', 'expired']),
        usage_bytes=dict(type='integer', minimum=0))
    file_fields = dict(id=file_id, object=dict(const='vector_store.file'),
        created_at=timestamp, vector_store_id=identity, status=status,
        usage_bytes=dict(type='integer', minimum=0), last_error=error,
        attributes=attributes, chunking_strategy=static)
    # The SDK literal is plural; some official page examples use singular.
    batch_fields = dict(id=identity, object=dict(const='vector_store.files_batch'),
        created_at=timestamp, vector_store_id=identity, status=status, file_counts=counts)
    query = obj(dict(after=identity, before=identity,
        limit=dict(type='integer', minimum=1, maximum=100, default=20),
        order=dict(enum=['asc', 'desc'], default='desc')))
    query['not'] = dict(required=['after', 'before'])
    file_query = dict(query, properties=dict(query['properties'], filter=status))
    batch_create = dict(oneOf=[
        obj(dict(file_ids=dict(type='array', minItems=1, maxItems=2000,
                              uniqueItems=True, items=file_id),
                 attributes=attributes, chunking_strategy=chunking), ['file_ids']),
        obj(dict(files=dict(type='array', minItems=1, maxItems=2000, items=attach),
                 attributes={}, chunking_strategy={}), ['files'])])
    def page(item):
        return obj(dict(object=dict(const='list'), data=dict(type='array',
            maxItems=100, items=ref(item)), first_id=nullable(identity),
            last_id=nullable(identity), has_more=dict(type='boolean')),
            ['object', 'data', 'first_id', 'last_id', 'has_more'])
    def deleted(name):
        return obj(dict(id=identity, object=dict(const=name+'.deleted'),
                        deleted=dict(const=True)), ['id', 'object', 'deleted'])
    scalar = dict(anyOf=[dict(type='string', maxLength=512),
                         dict(type='number'), dict(type='boolean')])
    comparison = obj(dict(type=dict(enum=['eq', 'ne', 'gt', 'gte', 'lt', 'lte'],
                                   default='eq'),
        key=dict(type='string', minLength=1, maxLength=64), value=scalar),
        ['key', 'value'])
    membership = obj(dict(type=dict(enum=['in', 'nin']),
        key=dict(type='string', minLength=1, maxLength=64),
        value=dict(type='array', maxItems=128, items=dict(anyOf=[
            dict(type='string', maxLength=512), dict(type='number')]))),
        ['type', 'key', 'value'])
    compound = obj(dict(type=dict(enum=['and', 'or']), filters=dict(type='array',
        minItems=1, maxItems=256, items=ref('vector_search_filter'))),
        ['type', 'filters'])
    filters = dict(oneOf=[comparison, membership, compound])
    filters['x-q4t-constraint'] = 'depth <=16, nodes <=256, operand bytes <=65536'
    search = obj(dict(query=dict(oneOf=[dict(type='string', minLength=1),
        dict(type='array', minItems=1, maxItems=16,
             items=dict(type='string', minLength=1))]),
        filters=nullable(ref('vector_search_filter')),
        max_num_results=dict(type='integer', minimum=1, maximum=50, default=10),
        rewrite_query=dict(type='boolean', default=False),
        ranking_options=obj(dict(ranker=dict(enum=['none', 'auto', 'default-2024-11-15']),
                                 score_threshold=dict(type='number', minimum=0, maximum=1)))),
        ['query'])
    search['x-q4t-constraint'] = 'aggregate query UTF-8 bytes <=65536; complete bounded corpus'
    hit = obj(dict(file_id=file_id, filename=dict(type='string'),
        score=dict(type='number', minimum=0, maximum=1), attributes=attributes,
        content=dict(type='array', minItems=1, items=obj(dict(type=dict(const='text'),
            text=dict(type='string')), ['type', 'text']))),
        ['file_id', 'filename', 'score', 'attributes', 'content'])
    return dict(vector_store_create=create, vector_store_update=update,
        vector_search_filter=filters, vector_search_request=search,
        vector_search_hit=hit, vector_search_page=obj(dict(
            object=dict(const='vector_store.search_results.page'),
            search_query=dict(type='array', minItems=1, maxItems=16,
                              items=dict(type='string', minLength=1)),
            data=dict(type='array', maxItems=50, items=ref('vector_search_hit')),
            has_more=dict(const=False), next_page=dict(type='null')),
            ['object', 'search_query', 'data', 'has_more', 'next_page']),
        vector_store_resource=obj(store_fields, store_fields),
        vector_store_query=query, vector_store_list=page('vector_store_resource'),
        vector_store_deleted=deleted('vector_store'), vector_file_create=attach,
        vector_file_update=obj(dict(attributes=attributes), ['attributes']),
        vector_file_resource=obj(file_fields, file_fields),
        vector_file_query=file_query, vector_file_list=page('vector_file_resource'),
        vector_file_deleted=deleted('vector_store.file'),
        vector_batch_create=batch_create, vector_batch_cancel=obj({}),
        vector_batch_resource=obj(batch_fields, batch_fields),
        vector_file_content=obj(dict(object=dict(const='vector_store.file_content.page'),
            data=dict(type='array', items=obj(dict(type=dict(const='text'),
                text=dict(type='string')), ['type', 'text'])),
            has_more=dict(const=False), next_page=dict(type='null')),
            ['object', 'data', 'has_more', 'next_page']))


def operations():
    root = '/v1/vector_stores'
    store = root+'/{vector_store_id}'
    files, batches = store+'/files', store+'/file_batches'
    return [
        (root, 'post', 'vector_store_create', None, 'vector_store_resource'),
        (root, 'get', None, 'vector_store_query', 'vector_store_list'),
        (store, 'get', None, None, 'vector_store_resource'),
        (store, 'post', 'vector_store_update', None, 'vector_store_resource'),
        (store, 'delete', None, None, 'vector_store_deleted'),
        (store+'/search', 'post', 'vector_search_request', None, 'vector_search_page'),
        (files, 'get', None, 'vector_file_query', 'vector_file_list'),
        (files, 'post', 'vector_file_create', None, 'vector_file_resource'),
        (files+'/{file_id}', 'get', None, None, 'vector_file_resource'),
        (files+'/{file_id}', 'post', 'vector_file_update', None, 'vector_file_resource'),
        (files+'/{file_id}', 'delete', None, None, 'vector_file_deleted'),
        (files+'/{file_id}/content', 'get', None, None, 'vector_file_content'),
        (batches, 'post', 'vector_batch_create', None, 'vector_batch_resource'),
        (batches+'/{batch_id}', 'get', None, None, 'vector_batch_resource'),
        (batches+'/{batch_id}/files', 'get', None, 'vector_file_query', 'vector_file_list'),
        (batches+'/{batch_id}/cancel', 'post', 'vector_batch_cancel', None, 'vector_batch_resource')]


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    for path, method, request, query, result in operations():
        parameters = [dict(name=part[1:-1], **{'in': 'path'}, required=True,
                           schema=dict(type='string', minLength=1))
                      for part in path.split('/') if part.startswith('{')]
        if query:
            parameters += [dict(name=name, **{'in': 'query'}, required=False, schema=value)
                for name, value in definitions()[query]['properties'].items()]
        operation = dict(operationId=method+'_'+path.strip('/').replace('/', '_')
            .replace('{', '').replace('}', ''), parameters=parameters,
            responses={'200': response(external(result)), 'default': response(error)},
            description='Native scoped durable vector management and source-bound indexed text; ingestion is asynchronous and cancellation completes after owned execution drains.')
        if request:
            operation['requestBody'] = dict(required=not path.endswith('/cancel'),
                content={'application/json': dict(schema=external(request))})
        if path.endswith('/search'):
            operation['description'] = ('Native scoped source-bound retrieval with actual query embedding, '
                'optional real model rewriting, exact shared quota and complete snapshot checks; '
                'auto ranking uses local cosine/BM25 RRF, not the private hosted ranker.')
        paths.setdefault(path, {})[method] = operation
