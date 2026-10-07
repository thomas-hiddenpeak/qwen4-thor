#!/usr/bin/env python3
"""Independent raw-routing analysis on the main study (router-study-20260928).

EXPERT IDENTITY: experts are per-layer weight INSTANCES. The identity is
(layer_id, expert_id); layer 0 expert 5 and layer 17 expert 5 are different
weights. Total instance space = 48 * 512 = 24576. All set/working-set metrics
are therefore computed per layer and aggregated across layers, never by OR-ing
bitmasks across layers (that collides expert IDs).

Angles beyond the existing distribution/top-N reports:
  1. determinism of the 4 exact-repeat requests
  2. per-layer concentration (normalized entropy, Gini, top-N coverage)
  3. temporal locality decay (decode, lag 1..64) vs random-pair baseline
  4. cross-layer local-ID independence (Jaccard vs random-ID baseline)
  5. prefill vs decode top-64 divergence per layer
  6. decode position drift (first 16 vs last 16 tokens), per layer
  7. domain separation, per layer
  8. per-layer working-set size across decode windows (instances, /24576)

Reads request-*.bin directly (read-only). No cache or throughput claims.
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
TOTAL_INSTANCES = L * E  # 24576
LAGS = (1, 2, 4, 8, 16, 32, 64)
DOMAINS = {
    1: 'python', 2: 'math', 3: 'chinese', 4: 'chinese', 5: 'systems',
    6: 'math', 7: 'python', 8: 'python', 9: 'python', 10: 'systems',
    11: 'systems', 12: 'systems', 13: 'chinese', 14: 'chinese', 15: 'math',
    16: 'math', 17: 'systems', 18: 'python', 19: 'math', 20: 'chinese',
}
REPEATS = {17: 12, 18: 7, 19: 16, 20: 14}
UNIQUE = [r for r in range(1, 21) if r not in REPEATS]

PC16 = np.array([bin(i).count('1') for i in range(65536)], dtype=np.uint8)


def popcount64(x):
    x = x.astype(np.uint64)
    return (PC16[x & 0xFFFF] + PC16[(x >> 16) & 0xFFFF]
            + PC16[(x >> 32) & 0xFFFF] + PC16[(x >> 48) & 0xFFFF])


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
            fwd = dict(forward_id=fid, stage=stage, position=position,
                       rows=rows, layers={})
            forwards.append(fwd)
        elif kind == 3:
            layer = struct.unpack('<I', data[:4])[0]
            ids = array('H')
            ids.frombytes(data[4:])
            if sys.byteorder != 'little':
                ids.byteswap()
            fwd['layers'][layer] = np.asarray(ids, np.uint16).reshape(-1, K)
    return forwards


def to_masks(ids):
    """ids [n, K] uint16 -> [n, 8] uint64 bitmasks (per-layer set semantics)."""
    n = ids.shape[0]
    masks = np.zeros((n, 8), dtype=np.uint64)
    e = ids.astype(np.int64)
    bit = (1 << (e % 64)).astype(np.uint64)
    masks[np.arange(n)[:, None], (e // 64).astype(np.int64)] |= bit
    return masks


def concentration(freq):
    total = int(freq.sum())
    active = int((freq > 0).sum())
    p = freq[freq > 0] / total
    H = float(-(p * np.log(p)).sum() / np.log(E))
    s = np.sort(freq.astype(np.float64))
    n = s.size
    gini = float((2 * np.sum(np.arange(1, n + 1) * s) / (n * s.sum()))
                 - (n + 1) / n)
    top = np.sort(freq)[::-1].astype(np.float64)
    cov = {str(n_): float(top[:n_].sum() / total)
           for n_ in (10, 32, 64, 128, 256, 512)}
    return dict(active=active, entropy_norm=round(H, 4),
                gini=round(gini, 4), top_cov=cov)


def topset(freq, n):
    return set(np.argsort(freq)[::-1][:n].tolist())


def jaccard(a, b):
    return len(a & b) / len(a | b)


def pct(x):
    x = np.asarray(x, dtype=np.float64)
    return dict(mean=round(float(x.mean()), 1),
                p50=round(float(np.percentile(x, 50)), 1),
                p90=round(float(np.percentile(x, 90)), 1),
                max=int(x.max()))


def main():
    trace = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((trace / 'manifest.json').read_text())
    paths = sorted(trace.glob('request-*.bin'),
                   key=lambda p: int(p.stem.split('-')[-1]))
    reqs = {int(p.stem.split('-')[-1]): load(p, manifest) for p in paths}

    results = {'expert_identity': '(layer_id, expert_id); per-layer instances',
               'total_expert_instances': TOTAL_INSTANCES,
               'manifest': {k: manifest[k] for k in
                            ('layers', 'experts', 'top_k', 'binary_sha256')}}

    # 1. determinism of exact repeats
    det = {}
    for rep, orig in sorted(REPEATS.items()):
        a, b = reqs[rep], reqs[orig]
        same = len(a) == len(b)
        for fa, fb in zip(a, b):
            same &= (fa['stage'], fa['position'], fa['rows']) == \
                    (fb['stage'], fb['position'], fb['rows'])
            for lay in range(L):
                same &= np.array_equal(fa['layers'][lay], fb['layers'][lay])
        det[f'{rep}=={orig}'] = bool(same)
    results['determinism'] = det

    # Assemble decode [n_dec, L, K] and prefill [n_rows, L, K] per request.
    dec, pre = {}, {}
    for rid, forwards in reqs.items():
        d = sorted((f for f in forwards if f['stage'] == 2),
                   key=lambda f: f['position'])
        p = sorted((f for f in forwards if f['stage'] == 1),
                   key=lambda f: f['position'])
        dec[rid] = (np.stack([np.stack([f['layers'][l] for l in range(L)],
                                       axis=1) for f in d]).reshape(-1, L, K)
                    if d else np.zeros((0, L, K), np.uint16))
        pre[rid] = np.concatenate(
            [np.stack([f['layers'][l] for l in range(L)], axis=1)
             for f in p])
    n_dec = {r: a.shape[0] for r, a in dec.items()}
    results['decode_tokens_per_request'] = n_dec
    decode_reqs = [r for r in UNIQUE if n_dec[r] > 0]
    results['zero_decode_requests'] = sorted(set(UNIQUE) - set(decode_reqs))

    # 2. per-layer concentration (decode). Authoritative; pooled-across-layers
    #    is NOT reported because expert IDs are per-layer instances.
    layer_dec = []
    for l in range(L):
        freq = np.zeros(E, dtype=np.int64)
        for r in decode_reqs:
            freq += np.bincount(dec[r][:, l].ravel().astype(np.int64),
                                minlength=E)
        c = concentration(freq)
        c['layer'] = l
        layer_dec.append(c)
    results['layer_concentration_decode'] = layer_dec
    results['layer_concentration_summary'] = {
        'active': [int(np.mean([c['active'] for c in layer_dec])),
                   min(c['active'] for c in layer_dec),
                   max(c['active'] for c in layer_dec)],
        'entropy_norm': [round(float(np.mean([c['entropy_norm']
                                              for c in layer_dec])), 4),
                         min(c['entropy_norm'] for c in layer_dec),
                         max(c['entropy_norm'] for c in layer_dec)],
        'top128': [round(float(np.mean([c['top_cov']['128']
                                        for c in layer_dec])), 4),
                   min(c['top_cov']['128'] for c in layer_dec),
                   max(c['top_cov']['128'] for c in layer_dec)]}
    with (out / 'layer_concentration.csv').open('w') as f:
        f.write('layer,active,entropy_norm,gini,top10,top32,top64,'
                'top128,top256\n')
        for c in layer_dec:
            f.write(f"{c['layer']},{c['active']},{c['entropy_norm']},"
                    f"{c['gini']}," +
                    ','.join(f"{c['top_cov'][n]:.4f}"
                             for n in ('10', '32', '64', '128', '256')) +
                    '\n')

    # Per-layer decode masks [n, L, 8] (per-layer bitmasks, no cross-layer OR).
    masks = {r: to_masks(a.reshape(-1, K)).reshape(a.shape[0], L, 8)
             for r, a in dec.items() if a.shape[0] > 0}

    # 3. temporal locality decay (decode, per-layer, averaged over layers)
    decay = {str(lag): [] for lag in LAGS}
    for rid in decode_reqs:
        m = masks[rid]
        n = m.shape[0]
        for lag in LAGS:
            ov = popcount64(m[:-lag] & m[lag:]).sum(-1)  # [n-lag, L]
            decay[str(lag)].append(float(ov.mean() / K))
    rng = np.random.default_rng(0)
    base = []
    for rid in decode_reqs:
        m = masks[rid]
        n = m.shape[0]
        i = rng.integers(0, n, (4096, L))
        j = rng.integers(0, n, (4096, L))
        a = m[i, np.arange(L)[None, :], :]
        b = m[j, np.arange(L)[None, :], :]
        base.append(float(popcount64(a & b).sum(-1).mean() / K))
    results['locality_decay'] = {
        'mean_overlap_per_lag': {k: round(float(np.mean(v)), 4)
                                 for k, v in decay.items()},
        'random_pair_baseline': round(float(np.mean(base)), 4)}
    with (out / 'locality_decay.csv').open('w') as f:
        f.write('lag,mean_top10_overlap,random_baseline\n')
        for lag in LAGS:
            f.write(f"{lag},{np.mean(decay[str(lag)]):.4f},"
                    f"{np.mean(base):.4f}\n")

    # 4. cross-layer local-ID independence. Experts are per-layer instances,
    #    so there is NOTHING shared across layers; this measures whether the
    #    LOCAL expert-ID usage pattern is independent across layers.
    dsets = []
    for l in range(L):
        f = np.bincount(np.concatenate(
            [dec[r][:, l].ravel() for r in decode_reqs]).astype(np.int64),
            minlength=E)
        dsets.append(topset(f, 32))
    jac = np.zeros((L, L))
    for i in range(L):
        for j in range(L):
            jac[i, j] = jaccard(dsets[i], dsets[j])
    off = (jac.sum() - np.trace(jac)) / (L * (L - 1))
    # Random baseline: two random 32-subsets of a 512-set.
    rng2 = np.random.default_rng(1)
    rand_jac = []
    for _ in range(2000):
        a = set(rng2.integers(0, E, 32).tolist())
        b = set(rng2.integers(0, E, 32).tolist())
        rand_jac.append(jaccard(a, b))
    results['cross_layer_local_id'] = {
        'note': 'experts are per-layer instances; nothing is shared across '
                'layers. This is local-ID-label overlap only.',
        'mean_offdiag_jaccard': round(float(off), 4),
        'random_id_baseline': round(float(np.mean(rand_jac)), 4)}

    # 5. prefill vs decode top-64 divergence (per layer, averaged)
    pd = []
    for l in range(L):
        pf = np.bincount(np.concatenate(
            [pre[r][:, l].ravel() for r in UNIQUE]).astype(np.int64),
            minlength=E)
        df = np.bincount(np.concatenate(
            [dec[r][:, l].ravel() for r in decode_reqs]).astype(np.int64),
            minlength=E)
        pd.append(jaccard(topset(pf, 64), topset(df, 64)))
    results['prefill_vs_decode_top64'] = {
        'mean_jaccard': round(float(np.mean(pd)), 4),
        'min': [int(np.argmin(pd)), round(float(np.min(pd)), 4)],
        'max': [int(np.argmax(pd)), round(float(np.max(pd)), 4)]}

    # 6. decode position drift, per layer (first 16 vs last 16 tokens)
    drift_jac = []
    for l in range(L):
        early = np.bincount(np.concatenate(
            [dec[r][:16, l].ravel() for r in decode_reqs]).astype(np.int64),
            minlength=E)
        late = np.bincount(np.concatenate(
            [dec[r][-16:, l].ravel() for r in decode_reqs]).astype(np.int64),
            minlength=E)
        drift_jac.append(jaccard(topset(early, 32), topset(late, 32)))
    ov = []
    for r in decode_reqs:
        a = masks[r][:16]
        b = masks[r][-16:]
        o = popcount64(a[:, None, :] & b[None, :, :]).sum(-1)  # [16,16,L]
        ov.append(float(o.mean() / K))
    results['position_drift'] = {
        'mean_per_layer_top32_jaccard_first16_vs_last16':
            round(float(np.mean(drift_jac)), 4),
        'mean_per_token_overlap': round(float(np.mean(ov)), 4)}

    # 7. domain separation, per layer (averaged over layers)
    doms = {}
    for r in decode_reqs:
        doms.setdefault(DOMAINS[r], []).append(r)
    dom_layer_sets = {}
    for d, rs in doms.items():
        per_layer = []
        for l in range(L):
            f = np.bincount(np.concatenate(
                [dec[r][:, l].ravel() for r in rs]).astype(np.int64),
                minlength=E)
            per_layer.append(topset(f, 32))
        dom_layer_sets[d] = per_layer
    names = sorted(dom_layer_sets)
    djac = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            per = [jaccard(dom_layer_sets[names[i]][l],
                           dom_layer_sets[names[j]][l]) for l in range(L)]
            djac[f'{names[i]}|{names[j]}'] = round(float(np.mean(per)), 4)
    # per-token overlap with the GLOBAL per-layer top-32
    global_layer32 = [topset(np.bincount(np.concatenate(
        [dec[r][:, l].ravel() for r in decode_reqs]).astype(np.int64),
        minlength=E), 32) for l in range(L)]
    gmask = []
    for l in range(L):
        g = np.zeros(8, dtype=np.uint64)
        for e_ in global_layer32[l]:
            g[e_ // 64] = int(g[e_ // 64]) | (1 << (e_ % 64))
        gmask.append(g)
    dom_overlap = {}
    for d in names:
        tot, cnt = 0.0, 0
        for r in doms[d]:
            m = masks[r]
            for l in range(L):
                tot += float(popcount64(m[:, l] & gmask[l][None, :]).sum(-1)
                             .mean() / K)
                cnt += 1
        dom_overlap[d] = round(tot / cnt, 4)
    results['domain'] = {'per_layer_top32_jaccard': djac,
                         'mean_per_token_overlap_with_global_per_layer_top32':
                         dom_overlap}

    # 8. per-layer working-set size across decode windows.
    #    For a window of w tokens, per-layer distinct experts, summed over
    #    layers -> total distinct INSTANCES (out of 24576).
    ws = {}
    for w in (1, 4, 8, 16, 32):
        totals = []
        perlayer = []
        for r in decode_reqs:
            a = dec[r]
            n = a.shape[0]
            for t in range(n - w + 1):
                layer_counts = [int(np.unique(a[t:t + w, l].ravel()).size)
                                for l in range(L)]
                totals.append(sum(layer_counts))
                perlayer.extend(layer_counts)
        ws[f'window_{w}'] = dict(
            total_instances=pct(totals),
            total_instances_space=TOTAL_INSTANCES,
            per_layer=pct(perlayer),
            per_layer_space=E)
    results['working_set'] = ws

    (out / 'results.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
