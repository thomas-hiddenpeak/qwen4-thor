"""Bounded, inspectable compression for the added MCP evidence leaf only.

The complete qualification retains its existing 8 MiB file limit. Compression
is verified offline/at package validation, never used to hide a failed answer.
Imports stay local so optional compression is not a monitoring dependency.
"""
from tools.serve_recovery import ledger

FORMAT = 'q4t-mcp-compressed-witness-v1'
MAX_ENCODED = 128 << 10
MAX_PLAIN = 2 << 20


def pack(value):
    import base64
    import gzip
    raw = ledger.canonical(value)
    ledger.require(len(raw) <= MAX_PLAIN, 'MCP witness plaintext bound')
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii')
    ledger.require(len(encoded) <= MAX_ENCODED, 'MCP witness encoded bound')
    return dict(format=FORMAT, bytes=len(raw), sha256=ledger.digest(value), data=encoded)


def unpack(value):
    import base64
    import hashlib
    import json
    import zlib
    ledger.keys(value, ('format', 'bytes', 'sha256', 'data'))
    ledger.require(value['format'] == FORMAT and
        type(value['bytes']) is int and 0 < value['bytes'] <= MAX_PLAIN and
        type(value['data']) is str and len(value['data']) <= MAX_ENCODED and
        type(value['sha256']) is str and ledger.HASH.fullmatch(value['sha256']),
        'bounded MCP witness envelope')
    compressed = base64.b64decode(value['data'], validate=True)
    decoder = zlib.decompressobj(31)
    raw = decoder.decompress(compressed, value['bytes'] + 1)
    ledger.require(decoder.eof and not decoder.unconsumed_tail and
        not decoder.unused_data and len(raw) == value['bytes'] and
        hashlib.sha256(raw).hexdigest() == value['sha256'],
        'MCP compression truncation, trailing stream, bound or identity')
    decoded = json.loads(raw)
    ledger.require(ledger.canonical(decoded) == raw, 'canonical MCP witness required')
    return decoded
