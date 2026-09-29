#!/usr/bin/env python3
"""Per-layer top-N for a 90% selection-coverage goal, calibration -> holdout.

Expert identity is (layer_id, expert_id); per-layer lists, no cross-layer
sharing. For each (stage, layer): rank experts by calibration frequency
(desc, ID asc tie-break), then find the smallest N whose holdout selection
coverage reaches the target. Also reports the uniform-N (same N for every
layer) needed to hit the target on every layer, and the slot/byte budget.

Reads request-*.bin (read-only). Descriptive; not a runtime cache proof.
"""
import json
import struct
import sys
import zlib
from array import array
from pathlib import Path

import numpy as np

MAGIC = b'Q4TRTE01'
L, E, K = 48, 512, 10
SLOT_BYTES = 2765056  # per-expert slot incl. scale/alignment (replay contract)
REPEATS = {17: 12, 18: 7, 19: 16, 20: 14}
UNIQUE = [r for r in range(1, 21) if r not in REPEATS]
DOMAINS = {
    1: 'python', 2: 'math', 3: 'chinese', 4: 'chinese', 5: 'systems',
    6: 'math', 7: 'python', 8: 'python', 9: 'python', 10: 'systems',
    11: 'systems', 12: 'systems', 13: 'chinese', 14: 'chinese', 15: 'math',
    16: 'math', 17: 'systems', 18: 'python', 19: 'math', 20: 'chinese',
}
# Stratified split: exactly one request per (domain, length) cell on each
# side. Cells: python-1K {1,7}, python-8K {8,9}, math-1K {2,16},
# math-8K {6,15}, chinese-8K {3,13}, chinese-1K {4,14}, systems-1K {5,12},
# systems-8K {10,11}. Request 7 has no decode (EOS at prefill boundary),
# so decode is 8 calib / 7 holdout; prefill is 8/8.
CALIB = [1, 8, 2, 6, 3, 4, 5, 10]
HOLDOUT = [7, 9, 16, 15, 13, 14, 12, 11]


def frames(path):
    with path.open('rb') as f:
        if f.read(8) != MAGIC:
            raise ValueError('bad trace magic')
        while True:
            prefix = f.read(8)
            if not prefix:
                break
            size, crc = struct.unpack('<II', prefix)
            payload = f.read(size)
            if len(payload) != size or zlib.crc32(payload) != crc:
                raise ValueError('corrupt frame')
            yield payload


def load(path, manifest):
    events = frames(path)
    header = next(events)
    dims = struct.unpack('<IIIII', header[:20])
    if dims != (1, L, E, K, manifest['max_rows']):
        raise ValueError(f'{path.name}: dims mismatch')
    forwards = []
    fwd = None
    for payload in events:
        kind, _ = struct.unpack('<IQ', payload[:12])
        data = payload[12:]
        if kind == 2:
            fid, stage, position, rows = struct.unpack('<QIQI', data)
            fwd = dict(stage=stage, layers={})
            forwards.append(fwd)
        elif kind == 3:
            layer = struct.unpack('<I', data[:4])[0]
            ids = array('H')
            ids.frombytes(data[4:])
            if sys.byteorder != 'little':
                ids.byteswap()
            fwd['layers'][layer] = np.asarray(ids, np.uint16).reshape(-1, K)
    return forwards


def main():
    trace = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((trace / 'manifest.json').read_text())
    paths = sorted(trace.glob('request-*.bin'),
                   key=lambda p: int(p.stem.split('-')[-1]))
    reqs = {int(p.stem.split('-')[-1]): load(p, manifest) for p in paths}

    def stage_matrix(rids, stage):
        mats = []
        for r in rids:
            fs = [f for f in reqs[r] if f['stage'] == stage]
            if not fs:
                continue
            mats.append(np.concatenate(
                [np.stack([f['layers'][l] for l in range(L)], axis=1)
                 for f in fs]))
        return np.concatenate(mats)  # [rows, L, K]

    calib_pre = stage_matrix(CALIB, 1)
    hold_pre = stage_matrix(HOLDOUT, 1)
    calib_dec = stage_matrix(CALIB, 2)
    hold_dec = stage_matrix(HOLDOUT, 2)

    results = {
        'expert_identity': '(layer_id, expert_id); per-layer lists',
        'split': {'calibration': CALIB, 'holdout': HOLDOUT,
                  'note': 'stratified by (domain, length); request 7 '
                          '(0 decode) in calibration, prefill only'},
        'rows': {'calib_prefill': int(calib_pre.shape[0]),
                 'hold_prefill': int(hold_pre.shape[0]),
                 'calib_decode': int(calib_dec.shape[0]),
                 'hold_decode': int(hold_dec.shape[0])}}

    def freqs(mat):
        f = np.zeros((L, E), dtype=np.int64)
        for l in range(L):
            f[l] = np.bincount(mat[:, l].ravel().astype(np.int64),
                               minlength=E)
        return f

    def order_of(f):
        return np.argsort(-f, axis=1, kind='stable')  # desc, ID asc tie

    rows_out = []
    for stage, calib, hold in (('prefill', calib_pre, hold_pre),
                               ('decode', calib_dec, hold_dec)):
        cf = freqs(calib)
        hf = freqs(hold)
        order = order_of(cf)
        # holdout coverage of the calibration top-N list, per layer
        cov = np.zeros((L, E), dtype=np.float64)
        for l in range(L):
            cov[l] = np.cumsum(hf[l][order[l]]) / hf[l].sum()
        same = np.zeros((L, E), dtype=np.float64)
        for l in range(L):
            same[l] = np.cumsum(cf[l][order[l]]) / cf[l].sum()
        for l in range(L):
            row = dict(stage=stage, layer=l,
                       calib_routes=int(cf[l].sum()),
                       hold_routes=int(hf[l].sum()))
            for t in (0.80, 0.90, 0.95):
                idx = int(np.searchsorted(cov[l], t, side='left')) + 1
                row[f'hold_{int(t*100)}_min_n'] = idx
                row[f'hold_{int(t*100)}_cov_at_n'] = round(float(cov[l][idx-1]), 4)
                sidx = int(np.searchsorted(same[l], t, side='left')) + 1
                row[f'same_{int(t*100)}_min_n'] = sidx
            rows_out.append(row)

    # Uniform N: same N for all layers, per stage.
    uniform = {}
    for stage in ('prefill', 'decode'):
        sub = [r for r in rows_out if r['stage'] == stage]
        for t in (0.80, 0.90, 0.95):
            key = f'hold_{int(t*100)}_min_n'
            u = max(r[key] for r in sub)
            uniform[f'{stage}_uniform_{int(t*100)}'] = u
    results['per_layer'] = rows_out
    results['uniform_n'] = uniform
    results['budget'] = {
        'slot_bytes': SLOT_BYTES,
        'decode_sum_min_n_90': sum(r['hold_90_min_n']
                                   for r in rows_out if r['stage'] == 'decode'),
        'prefill_sum_min_n_90': sum(r['hold_90_min_n']
                                    for r in rows_out if r['stage'] == 'prefill'),
        'uniform_90_both_stages_slots':
            max(uniform['prefill_uniform_90'], uniform['decode_uniform_90']) * L,
        'note': 'shared experts, KV, states and workspace are outside the '
                'routed-expert budget'}

    with (out / 'per_layer_topn.csv').open('w') as f:
        import csv
        w = csv.DictWriter(f, fieldnames=list(rows_out[0]))
        w.writeheader()
        w.writerows(rows_out)

    # Concrete per-layer expert-ID lists: ranked by calibration frequency,
    # sized to the holdout 90% minimum N. Directly usable as a resident set.
    lists = {'note': 'ranked by calibration frequency (desc, ID asc tie); '
                     'N = holdout 90% selection-coverage minimum',
             'split': results['split']}
    for stage in ('prefill', 'decode'):
        lists[stage] = {}
        sub = [r for r in rows_out if r['stage'] == stage]
        # recompute order for this stage
        calib = calib_pre if stage == 'prefill' else calib_dec
        cf = freqs(calib)
        order = order_of(cf)
        for r in sub:
            l = r['layer']
            n = r['hold_90_min_n']
            lists[stage][str(l)] = dict(
                n=n, hold_cov_90=r['hold_90_cov_at_n'],
                experts=[int(x) for x in order[l][:n]])
    (out / 'expert_lists_90.json').write_text(
        json.dumps(lists, indent=1))
    (out / 'results.json').write_text(json.dumps(results, indent=2))

    # Compact summary table.
    print(f"{'stage':8} {'layer':>5} {'calib':>9} {'hold':>9} "
          f"{'N80':>4} {'N90':>4} {'N95':>4} | {'sameN90':>7}")
    for r in rows_out:
        print(f"{r['stage']:8} {r['layer']:>5} {r['calib_routes']:>9} "
              f"{r['hold_routes']:>9} {r['hold_80_min_n']:>4} "
              f"{r['hold_90_min_n']:>4} {r['hold_95_min_n']:>4} | "
              f"{r['same_90_min_n']:>7}")
    print('uniform N:', json.dumps(uniform))
    print('budget:', json.dumps(results['budget']))


if __name__ == '__main__':
    main()
