#!/usr/bin/env python3
"""Validate real MoE boundary records and compare aligned logical token rows."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

IDENTITIES = {'model', 'weights', 'binary', 'tokenizer', 'source'}
LAYOUTS = {'hidden': ('bf16', 'hidden_size'), 'logits': ('bf16', 'experts'),
           'ids': ('i32', 'top_k'), 'weights': ('f32', 'top_k'),
           'output': ('bf16', 'hidden_size')}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, minimum, maximum, label):
    require(type(value) is int and minimum <= value <= maximum,
            f'invalid {label}')
    return value


def digest(value):
    require(isinstance(value, str) and len(value) == 64 and
            all(c in '0123456789abcdef' for c in value) and value != '0' * 64,
            'invalid or missing digest')


class Record:
    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        require(len(raw) <= 8 * 1024 * 1024, 'manifest exceeds limit')
        self.manifest_sha256 = sha(raw)
        self.meta = json.loads(raw, object_pairs_hook=unique_object)
        m = self.meta
        require(type(m) is dict and set(m) == {
            'version', 'role', 'layer', 'position', 'rows', 'experts',
            'top_k', 'hidden_size', 'vocab', 'tokens', 'identity', 'files'},
            'unknown or missing manifest fields')
        require(type(m['version']) is int and m['version'] == 1 and
                m['role'] == 'main', 'unsupported version or role')
        integer(m['layer'], 0, 47, 'layer')
        integer(m['position'], 0, 208896, 'position')
        integer(m['rows'], 1, 256, 'rows')
        integer(m['experts'], 1, 512, 'experts')
        integer(m['top_k'], 1, min(16, m['experts']), 'top_k')
        integer(m['hidden_size'], 1, 16384, 'hidden_size')
        integer(m['vocab'], 1, 1048576, 'vocab')
        require(type(m['tokens']) is list and
                len(m['tokens']) == m['position'] + m['rows'],
                'incomplete token prefix')
        for token in m['tokens']:
            integer(token, 0, m['vocab'] - 1, 'token')
        require(type(m['identity']) is dict and
                set(m['identity']) == IDENTITIES, 'incomplete identity')
        for value in m['identity'].values():
            digest(value)
        require(type(m['files']) is dict and set(m['files']) == set(LAYOUTS),
                'missing or extra payload')
        self.bits, self.values = {}, {}
        for name, (dtype, dimension) in LAYOUTS.items():
            item = m['files'][name]
            require(type(item) is dict and set(item) == {'path', 'sha256'},
                    f'invalid payload descriptor: {name}')
            require(isinstance(item['path'], str), 'invalid payload path')
            relative = Path(item['path'])
            require(not relative.is_absolute() and '..' not in relative.parts,
                    'payload path escapes record')
            payload = (self.path.parent / relative).resolve()
            require(payload.is_relative_to(self.path.parent),
                    'payload symlink escapes record')
            digest(item['sha256'])
            size = m['rows'] * m[dimension] * (2 if dtype == 'bf16' else 4)
            require(payload.stat().st_size == size, f'payload size: {name}')
            data = payload.read_bytes()
            require(len(data) == size and sha(data) == item['sha256'],
                    f'payload checksum: {name}')
            code = 'H' if dtype == 'bf16' else ('i' if dtype == 'i32' else 'I')
            bits = list(struct.unpack('<' + code * (size // struct.calcsize(code)), data))
            if dtype == 'bf16':
                values = [struct.unpack('<f', struct.pack('<I', v << 16))[0]
                          for v in bits]
            elif dtype == 'f32':
                values = list(struct.unpack('<' + 'f' * (size // 4), data))
            else:
                values = bits
            require(all(math.isfinite(v) for v in values), f'nonfinite {name}')
            self.bits[name], self.values[name] = bits, values
        for row in range(m['rows']):
            ids = self.row('ids', row)
            require(len(set(ids)) == m['top_k'] and
                    all(0 <= v < m['experts'] for v in ids),
                    'duplicate or out-of-range expert ID')
            weights = self.row('weights', row)
            require(all(0 <= v <= 1 for v in weights) and
                    abs(sum(weights) - 1) <= 2e-6, 'invalid routing weights')

    def row(self, name, index, bits=False):
        size = self.meta[LAYOUTS[name][1]]
        values = self.bits[name] if bits else self.values[name]
        return values[index * size:(index + 1) * size]


def statistics(a, b, a_bits, b_bits):
    require(len(a) == len(b) == len(a_bits) == len(b_bits), 'comparison shape')
    differences = [x - y for x, y in zip(a, b)]
    l2 = math.sqrt(math.fsum(v * v for v in differences))
    reference_l2 = math.sqrt(math.fsum(v * v for v in b))
    return {'elements': len(a),
            'bitwise_differences': sum(x != y for x, y in zip(a_bits, b_bits)),
            'max_abs': max((abs(v) for v in differences), default=0),
            'l2': l2, 'reference_l2': reference_l2,
            'relative_l2': l2 / reference_l2 if reference_l2 else None}


def compare(left, right):
    a, b = left.meta, right.meta
    for key in ['role', 'layer', 'experts', 'top_k', 'hidden_size', 'vocab']:
        require(a[key] == b[key], f'incompatible {key}')
    for key in ['model', 'weights', 'tokenizer']:
        require(a['identity'][key] == b['identity'][key], f'identity mismatch: {key}')
    begin = max(a['position'], b['position'])
    end = min(a['position'] + a['rows'], b['position'] + b['rows'])
    require(begin < end, 'no overlapping logical rows')
    rows = []
    for position in range(begin, end):
        require(a['tokens'][:position + 1] == b['tokens'][:position + 1],
                f'token prefix mismatch at {position}')
        ar, br = position - a['position'], position - b['position']
        def measure(name):
            return statistics(left.row(name, ar), right.row(name, br),
                              left.row(name, ar, True), right.row(name, br, True))
        ai, bi = left.row('ids', ar), right.row('ids', br)
        common = sorted(set(ai) & set(bi))
        aligned = {}
        for name in ['logits', 'weights']:
            apos = common if name == 'logits' else [ai.index(e) for e in common]
            bpos = common if name == 'logits' else [bi.index(e) for e in common]
            aligned[name] = statistics(
                [left.row(name, ar)[i] for i in apos],
                [right.row(name, br)[i] for i in bpos],
                [left.row(name, ar, True)[i] for i in apos],
                [right.row(name, br, True)[i] for i in bpos])
        hidden, output = measure('hidden'), measure('output')
        rows.append({'position': position,
                     'prefix_sha256': sha(struct.pack('<' + 'I' * (position + 1),
                                                     *a['tokens'][:position + 1])),
                     'input_hidden': hidden, 'all_logits': measure('logits'),
                     'selection': {'ordered_slot_differences': sum(x != y for x, y in zip(ai, bi)),
                                   'left_ids': ai, 'right_ids': bi,
                                   'common_experts': common,
                                   'left_only': sorted(set(ai) - set(bi)),
                                   'right_only': sorted(set(bi) - set(ai))},
                     'aligned_common_experts': aligned, 'moe_output': output,
                     'interpretation': (
                         'Input hidden differs: upstream differences are present; '
                         'this comparison does not isolate MoE shape effects.'
                         if hidden['bitwise_differences'] else
                         'Identical boundary input: observed differences are within '
                         'this MoE boundary, but selection, current weights, routed '
                         'and shared expert arithmetic are not causal ablations.')})
    return {'version': 1, 'left': {'manifest_sha256': left.manifest_sha256,
                                  'identity': a['identity'], 'rows': a['rows']},
            'right': {'manifest_sha256': right.manifest_sha256,
                      'identity': b['identity'], 'rows': b['rows']},
            'rows': rows,
            'limits': 'Common-expert weights are expert-aligned, not slot-aligned. '
                      'Missing experts are selection differences, not zero weights. '
                      'No expert-only causal attribution or full-model quality claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('left', type=Path)
    parser.add_argument('right', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output.resolve()
    require(any(output.is_relative_to(root / part) for part in
                ('build', '.q4t-work')), 'output must be under build/ or .q4t-work/')
    result = compare(Record(args.left), Record(args.right))
    # Do not overwrite an earlier report or allow JSON NaNs.
    with output.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
