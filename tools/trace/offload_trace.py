"""Validated streaming, token-ordered input for counterfactual offload replay.

The recorded binary is verified against the capture, not against today's binary.
These are router decisions, not observed cache transactions or storage timings.
Validate every selected request before consuming it; publish results only after
the iterator is exhausted. At most one committed forward is buffered.
"""
from pathlib import Path
from array import array
import json
import struct
import subprocess
import sys

from analyze import frames, sha


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _stat(path):
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _package(path, source_binary):
    directory = path.parent
    manifest = json.loads((directory / 'manifest.json').read_text())
    _require(manifest.get('schema') == 1 and
             manifest.get('complete') is True and
             manifest.get('failure') == 'none', 'incomplete capture manifest')
    _require(not list(directory.glob('request-*.partial')),
             'partial request in capture')
    paths = list(directory.glob('request-*.bin'))
    _require(paths and len(paths) == manifest['requests_started'] ==
             manifest['requests_published'], 'empty or missing request files')
    _require(path in paths, 'selected request is absent from capture')
    for sibling in paths:
        _require(sibling.with_suffix('.tokens').is_file() and
                 sibling.with_suffix('.json').is_file(),
                 'missing request sidecar')
    sources = {'binary_sha256': source_binary,
               'model_index_sha256': directory / 'model-index.json',
               'model_config_sha256': directory / 'model-config.json',
               'workload_sha256': directory / 'workload.json',
               'command_sha256': directory / 'command.bin',
               'environment_sha256': directory / 'environment.bin'}
    for field, source in sources.items():
        _require(sha(source) == manifest[field], field + ' mismatch')
    dims = tuple(manifest[k] for k in ('layers', 'experts', 'top_k', 'max_rows'))
    _require(dims == (48, 512, 10, 8192),
             'offload replay requires 48 layers, 512 experts, top10, max_rows8192')
    return manifest


def _layers(path, manifest, metadata, summary):
    events = frames(path)
    header = next(events, b'')
    expected = struct.pack('<5I', 1, manifest['layers'], manifest['experts'],
                           manifest['top_k'], manifest['max_rows'])
    expected += bytes.fromhex(manifest['binary_sha256'] +
                              manifest['model_index_sha256'] +
                              manifest['workload_sha256'])
    _require(header == expected, 'trace header identity/dimensions mismatch')
    tokens = path.with_suffix('.tokens')
    token_sha = sha(tokens)
    request_id = None
    forward = None
    pending = []
    ended = run_ended = decode_started = False
    previous_forward = position = 0
    summary.update(forwards=0, prefill_rows=0, decode_rows=0, route_ids=0)
    for sequence, payload in enumerate(events):
        _require(len(payload) >= 12 and not run_ended, 'invalid/trailing event')
        kind, event_sequence = struct.unpack('<IQ', payload[:12])
        _require(sequence == event_sequence, 'event sequence gap or duplicate')
        body = payload[12:]
        if kind == 1:
            _require(request_id is None and len(body) == 48,
                     'duplicate or malformed request begin')
            request_id, prompt_rows = struct.unpack('<QQ', body[:16])
            _require(request_id > 0 and request_id == metadata['request_id'] and
                     prompt_rows == metadata['prompt_tokens'] and prompt_rows > 0,
                     'request metadata mismatch')
            _require(tokens.stat().st_size == prompt_rows * 4 and
                     body[16:].hex() == token_sha, 'input token SHA/size mismatch')
        elif kind == 2:
            _require(request_id is not None and not ended and forward is None and
                     len(body) == 24, 'invalid forward begin')
            fid, stage, start, rows = struct.unpack('<QIQI', body)
            _require(fid > previous_forward and start == position and
                     0 < rows <= manifest['max_rows'],
                     'forward identity, position or row count invalid')
            if stage == 1:
                _require(not decode_started and start + rows <= prompt_rows,
                         'prefill outside prompt or after decode')
            else:
                _require(stage == 2 and start >= prompt_rows and rows == 1,
                         'invalid decode forward')
                decode_started = True
            previous_forward = fid
            forward = dict(request_id=request_id, forward_id=fid, stage=stage,
                           position=start, rows=rows)
            pending = []
        elif kind == 3:
            _require(forward is not None and len(body) >= 4,
                     'layer outside forward')
            layer = struct.unpack('<I', body[:4])[0]
            rows, top_k = forward['rows'], manifest['top_k']
            _require(layer == len(pending) and layer < manifest['layers'] and
                     len(body) == 4 + rows * top_k * 2,
                     'missing/duplicate layer or wrong ID count')
            ids = array('H')
            ids.frombytes(body[4:])
            if sys.byteorder != 'little':
                ids.byteswap()
            _require(max(ids) < manifest['experts'], 'illegal expert ID')
            _require(all(len(set(ids[i:i + top_k])) == top_k
                         for i in range(0, len(ids), top_k)),
                     'duplicate expert in token topk')
            pending.append(dict(**forward, layer=layer, top_k=top_k,
                                topk=top_k, topk_ids=ids,
                                phase='prefill' if forward['stage'] == 1 else 'decode',
                                token_order=None))
        elif kind == 4:
            _require(forward is not None and body == b'\1\1\1' and
                     len(pending) == manifest['layers'],
                     'uncommitted forward or missing layer')
            summary['forwards'] += 1
            key = 'prefill_rows' if forward['stage'] == 1 else 'decode_rows'
            summary[key] += forward['rows']
            summary['route_ids'] += (forward['rows'] * manifest['layers'] *
                                     manifest['top_k'])
            position += forward['rows']
            yield from pending
            pending = []
            forward = None
        elif kind == 5:
            _require(request_id is not None and not ended and forward is None and
                     len(body) == 12, 'invalid request end')
            outcome, output_tokens = struct.unpack('<IQ', body)
            _require(outcome == 1 and position >= prompt_rows,
                     'failed/cancelled request or incomplete prefill')
            summary['output_tokens'] = output_tokens
            ended = True
        elif kind == 6:
            _require(ended and not body, 'invalid run end')
            run_ended = True
        else:
            raise ValueError('unknown event kind')
    _require(run_ended and ended and forward is None, 'incomplete trace')


def validate_request(path, *, checker, source_binary, expected_sha256=None):
    """Validate one complete request and its package; return JSON-safe identity.

Other requests are checked for presence only, not replayed or aggregated. The
selected request is validated by the original C++ checker and this independent
token-order parser. Missing archived source binaries are an explicit error.
"""
    path, checker, source_binary = (Path(p).resolve()
                                    for p in (path, checker, source_binary))
    manifest = _package(path, source_binary)
    digest = sha(path)
    _require(expected_sha256 is None or digest == expected_sha256,
             'selected trace SHA mismatch')
    metadata = json.loads(path.with_suffix('.json').read_text())
    checked = subprocess.run([str(checker), str(path)], capture_output=True,
                             text=True, timeout=120)
    _require(checked.returncode == 0,
             'C++ checker rejected selected request: ' + checked.stderr.strip())
    verified = json.loads(checked.stdout)
    _require(verified['structurally_complete'] is True and
             verified['requests'] == verified['successful_requests'] == 1 and
             verified['cancelled_requests'] == verified['failed_requests'] == 0,
             'checker did not verify a single successful request')
    summary = {}
    for _ in _layers(path, manifest, metadata, summary):
        pass
    for ours, theirs in [('prefill_rows', 'committed_prefill_rows'),
                         ('decode_rows', 'committed_decode_rows'),
                         ('route_ids', 'route_ids'), ('output_tokens', 'output_tokens')]:
        _require(summary[ours] == verified[theirs], 'parser/checker count mismatch')
    _require(sha(path) == digest, 'trace changed during validation')
    bindings = {str(p): dict(sha256=sha(p), stat=_stat(p)) for p in
                [path, path.with_suffix('.json'), path.with_suffix('.tokens'),
                 path.parent / 'manifest.json']}
    return dict(schema=1, path=str(path), trace_sha256=digest,
                source_binary=str(source_binary),
                source_binary_sha256=manifest['binary_sha256'],
                checker=str(checker), checker_sha256=sha(checker),
                manifest=manifest, metadata=metadata, summary=summary,
                bindings=bindings,
                route_scope='recorded source-binary routes; counterfactual for current runtime',
                current_runtime_route_equivalence='NOT_VERIFIED',
                wall_time_and_actual_storage_reads='NOT_RECORDED')


def iter_layers(path, validation):
    """Yield flat array('H') row-major IDs in request/forward/layer order.

All input bindings are rechecked before yielding. The iterator must be fully
consumed: a later corruption remains fatal and never licenses partial output.
No token, forward, prefill or decode window is truncated.
"""
    path = Path(path).resolve()
    _require(str(path) == validation['path'], 'validation belongs to another trace')
    for name, binding in validation['bindings'].items():
        p = Path(name)
        _require(_stat(p) == binding['stat'] and sha(p) == binding['sha256'],
                 'source changed after validation: ' + str(p))
    summary = {}
    yield from _layers(path, validation['manifest'], validation['metadata'], summary)
    _require(summary == validation['summary'], 'replay parse differs from validation')
