"""Validate upstream captures and locate differing observed boundaries offline."""
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'moe_replay_real'))
from report import Record, require, sha, unique_object
from reference import analyze_dot

PROJECTIONS = ['attn_hc.down', 'attn_hc.up', 'gdn.qkv', 'gdn.z', 'gdn.a',
               'gdn.beta', 'gdn.out', 'mlp_hc.down', 'mlp_hc.up', 'moe.router']


# Frozen target-model dimensions, independently checked against capture shapes.
DIMS = {'attn_hc.down': (320, 10240), 'attn_hc.up': (10240, 320),
        'mlp_hc.down': (320, 10240), 'mlp_hc.up': (10240, 320),
        'gdn.qkv': (10240, 2560), 'gdn.z': (6144, 2560),
        'gdn.a': (48, 2560), 'gdn.beta': (48, 2560),
        'gdn.out': (2560, 6144), 'moe.router': (512, 2560),
        'ple.key': (10240, 2560), 'ple.value': (2560, 2560)}

def expected_elements(suffix, rows):
    if suffix.startswith('ssm.'):
        return 48 * 128 * 128
    if suffix.startswith('conv.'):
        return 10240 * 3
    if suffix.startswith('ple.conv.'):
        return 10240 * 9
    if suffix.startswith('trunk.'):
        return rows * 10240
    if suffix == 'ple.embeddings':
        return rows * 2560
    proj, kind = suffix.rsplit('.', 1)
    require(proj in DIMS and kind in ('x', 'y'), 'unknown boundary')
    return rows * DIMS[proj][1 if kind == 'x' else 0]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_events(directory, tokens):
    rows = [json.loads(line, object_pairs_hook=unique_object) for line in
            (directory / 'events.jsonl').read_text().splitlines()]
    result = {}
    for index, event in enumerate(rows):
        require(event['index'] == index, 'event sequence gap')
        name = event['file']
        require(Path(name).name == name and name not in result, 'event path/duplicate')
        p, t, layer = event['position'], event['rows'], event['layer']
        require(layer in (0, 1, 2) and ((t == 1 and 72 <= p <= 75) or (t == 4 and p == 72)),
                'unexpected event context')
        require(name.startswith(f'p{p}-t{t}-l{layer}.'), 'event name/context mismatch')
        require(event['prefix_sha256'] == sha(struct.pack('<' + 'i' * (p + t), *tokens[:p + t])),
                'upstream prefix mismatch')
        suffix = name.split('.', 1)[1]
        require(event['dtype'] == ('f32' if suffix.startswith('ssm.') else 'bf16'), 'event dtype')
        require(type(event['elements']) is int and event['elements'] == expected_elements(suffix, t), 'event shape')
        path = directory / name
        require(path.stat().st_size == event['elements'] * (2 if event['dtype'] == 'bf16' else 4), 'event bytes')
        require(digest(path) == event['sha256'], 'event hash')
        data = path.read_bytes()
        if event['dtype'] == 'bf16':
            require(all((v[0] & 0x7f80) != 0x7f80 for v in struct.iter_unpack('<H', data)), 'nonfinite event')
        else:
            require(all(math.isfinite(v[0]) for v in struct.iter_unpack('<f', data)), 'nonfinite state')

        result[name] = event
    expected = set()
    for p, t in [(72 + i, 1) for i in range(4)] + [(72, 4)]:
        for layer in range(3):
            prefix = f'p{p}-t{t}-l{layer}.'
            names = ['trunk.in', 'trunk.out', 'ssm.before', 'ssm.after',
                     'conv.before', 'conv.after']
            projections = PROJECTIONS + (['ple.key', 'ple.value'] if layer == 1 else [])
            names += [proj + suffix for proj in projections for suffix in ('.x', '.y')]
            if layer == 1:
                names += ['ple.embeddings', 'ple.conv.before', 'ple.conv.after']
            expected.update(prefix + name for name in names)
    require(set(result) == expected, f'capture inventory missing={expected-set(result)} extra={set(result)-expected}')
    return result


def analyze(capture, previous):
    tokens_raw = (capture / 'tokens.i32').read_bytes()
    require(tokens_raw == (previous / 'tokens.i32').read_bytes(), 'original token fixture changed')
    tokens = list(struct.unpack('<76i', tokens_raw))
    # New instrumentation must reproduce the original natural layer-2 records.
    for name in ['prefill-t1', 'prefill-t4', 't4'] + [f't1-row{i}' for i in range(4)]:
        a, b = Record(capture / name / 'record.json'), Record(previous / name / 'record.json')
        require(a.bits == b.bits and a.meta['tokens'] == b.meta['tokens'], 'observer changed layer2 boundary')
        for key in ('model', 'weights', 'tokenizer'):
            require(a.meta['identity'][key] == b.meta['identity'][key], 'original model identity mismatch')
    directory = capture / 'upstream'
    events = validate_events(directory, tokens)
    summaries = []
    weights_verified = set()
    for layer in range(3):
        # Both executions start from exactly identical complete recurrent state.
        for name in ['ssm.before', 'conv.before'] + (['ple.conv.before'] if layer == 1 else []):
            require((directory / f'p72-t1-l{layer}.{name}').read_bytes() ==
                    (directory / f'p72-t4-l{layer}.{name}').read_bytes(), 'prefill states differ')
        prefix = f'p72-t4-l{layer}.'
        # Keep actual executed boundary order, not filename sort order.
        for name, event in events.items():
            if not name.startswith(prefix) or event['dtype'] != 'bf16' or '.before' in name or '.after' in name:
                continue
            suffix = name[len(prefix):]
            raw = (directory / name).read_bytes()
            require(len(raw) % 8 == 0, 'batch row size')
            width = len(raw) // 8
            for row in range(4):
                left_name = f'p{72+row}-t1-l{layer}.{suffix}'
                left_raw = (directory / left_name).read_bytes()
                right_raw = raw[row*width*2:(row+1)*width*2]
                require(len(left_raw) == len(right_raw), 'aligned boundary shape')
                left = struct.unpack('<' + 'H' * width, left_raw)
                right = struct.unpack('<' + 'H' * width, right_raw)
                differing = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
                summary = dict(layer=layer, position=72+row, boundary=suffix,
                               elements=width, differences=len(differing), first_indices=differing[:16])
                if differing and suffix.endswith('.y'):
                    proj = suffix[:-2]
                    metadata = json.loads((directory / (prefix + proj + '.json')).read_text())
                    old = json.loads((directory / f'p{72+row}-t1-l{layer}.{proj}.json').read_text())
                    for key in ['n', 'k', 'alpha', 'beta', 'weight', 'weight_sha256']:
                        require(metadata[key] == old[key], 'projection metadata mismatch')
                    weight = directory / metadata['weight']
                    require(weight.parent == directory and weight.name == metadata['weight'], 'weight path')
                    if weight.name not in weights_verified:
                        require(digest(weight) == metadata['weight_sha256'], 'weight hash')
                        weights_verified.add(weight.name)
                    k = metadata['k']
                    x1 = (directory / f'p{72+row}-t1-l{layer}.{proj}.x').read_bytes()
                    xb = (directory / (prefix + proj + '.x')).read_bytes()[row*k*2:(row+1)*k*2]
                    summary.update(input_equal=x1 == xb, dispatch_t1=old['dispatch'], dispatch_t4=metadata['dispatch'])
                    # Bound the offline exact reference to the first 16 changed
                    # outputs of each equal-input projection; preserve all raw data.
                    if x1 == xb:
                        inputs = struct.unpack('<' + 'H' * k, x1)
                        references = []
                        with weight.open('rb') as stream:
                            for column in differing[:16]:
                                stream.seek(column*k*2)
                                w = struct.unpack('<' + 'H' * k, stream.read(k*2))
                                references.append(dict(column=column, **analyze_dot(inputs, w, [left[column], right[column]])))
                        summary['exact_references'] = references
                summaries.append(summary)
    return dict(status='CAPTURE_VALIDATED', events=len(events), boundaries=summaries,
                limits='First differing observed boundary, not yet proof of first internal arithmetic operation. '
                'Exact-dot reference alone does not certify an FP32 reduction. Earlier recurrent-state differences must be traced.')
