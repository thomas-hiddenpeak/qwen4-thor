"""Uploads' actual JSON/multipart shapes, pinned to official SDK 7.47.0."""
import hashlib
from pathlib import Path

SOURCES = {
    'src/resources/uploads/uploads.ts': '53450b5df300f270948be9d9a4b395518da3bc9d1767c1d740ac5cbfa9d7aaf8',
    'src/resources/uploads/parts.ts': 'c32650dd5cae8a377915533d12dcbab8835e47e875b6162107664a8887ab3136',
}
UPLOAD_BYTES = 8 * 1024**3
PART_BYTES = 64 * 1024**2
MAX_PARTS = 8192


def definitions():
    from tools.serve_responses.build_contract import obj, ref, nullable
    from tools.serve_responses.files_contract import PURPOSES
    identity = dict(type='string', minLength=1, maxLength=128, pattern='^[A-Za-z0-9_-]+$')
    time = dict(type='integer', minimum=0)
    resource = obj(dict(id=identity, object=dict(const='upload'), bytes=dict(type='integer', minimum=0, maximum=UPLOAD_BYTES),
        created_at=time, expires_at=time, filename=dict(type='string', minLength=1), purpose=dict(type='string'),
        status=dict(enum=['pending', 'completed', 'cancelled', 'expired']), file=nullable(ref('file_resource'))),
        ['id', 'object', 'bytes', 'created_at', 'expires_at', 'filename', 'purpose', 'status'])
    part = obj(dict(id=identity, object=dict(const='upload.part'), created_at=time, upload_id=identity),
               ['id', 'object', 'created_at', 'upload_id'])
    create = obj(dict(bytes=dict(type='integer', minimum=0, maximum=UPLOAD_BYTES),
        filename=dict(type='string', minLength=1, maxLength=1024),
        mime_type=dict(type='string', minLength=1, maxLength=255), purpose=dict(enum=PURPOSES),
        expires_after=obj(dict(anchor=dict(const='created_at'),
            seconds=dict(type='integer', minimum=3600, maximum=2592000)), ['anchor', 'seconds'])),
        ['bytes', 'filename', 'mime_type', 'purpose'])
    complete = obj(dict(part_ids=dict(type='array', maxItems=MAX_PARTS, items=identity),
                        md5=dict(type='string', pattern='^[0-9a-fA-F]{32}$')), ['part_ids'])
    return dict(upload_resource=resource, upload_part_resource=part, upload_create=create,
                upload_complete=complete, upload_cancel=obj({}),
                upload_part_create=obj(dict(data=dict(type='string', format='binary')), ['data']))


def source(sdk):
    for name, expected in SOURCES.items():
        actual = hashlib.sha256((Path(sdk)/'node_modules/openai'/name).read_bytes()).hexdigest()
        if actual != expected:raise ValueError('pinned Uploads SDK source changed: '+name)
    return dict(sdk_version='7.47.0', sources_sha256=SOURCES)


def add_openapi(paths, response, error):
    ref = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    for suffix, request, result in [('', 'upload_create', 'upload_resource'),
        ('/{upload_id}/parts', 'upload_part_create', 'upload_part_resource'),
        ('/{upload_id}/complete', 'upload_complete', 'upload_resource'),
        ('/{upload_id}/cancel', 'upload_cancel', 'upload_resource')]:
        multipart = suffix.endswith('/parts')
        operation = dict(operationId='post_v1_uploads'+suffix.replace('/', '_').replace('{', '').replace('}', ''),
            parameters=[] if not suffix else [dict(name='upload_id', **{'in': 'path'}, required=True,
                                                    schema=dict(type='string', minLength=1))],
            requestBody=dict(required=not suffix.endswith('/cancel'), content={
                'multipart/form-data' if multipart else 'application/json': dict(schema=ref(request))}),
            responses={'200': response(ref(result)), 'default': response(error)},
            description='Client-scoped durable Upload; parts retain original IDs, completion verifies actual bytes and atomically publishes File.',
            **{'x-q4t-limits': dict(upload_bytes=UPLOAD_BYTES, part_bytes=PART_BYTES,
                 part_wire_bytes=PART_BYTES+65536, parts_per_upload=MAX_PARTS, manifest_chunks=16384,
                 upload_lifetime_seconds=3600, receipt_grace_seconds=86400,
                 json_wire_bytes=2*1024**2 if suffix.endswith('/complete') else 16384,
                 storage_quota_separate=True),
               'x-q4t-recovery': 'Unknown receipt retains original IDs. Matching complete parameters reconcile the original File without replay. Cancel/expiry release unfinished parts; completed File retains its own lifetime.'})
        paths['/v1/uploads'+suffix] = dict(post=operation)
