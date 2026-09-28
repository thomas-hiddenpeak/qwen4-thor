"""Describe per-layer MoE ID distributions, without any cache simulation.

Requires NumPy. Full committed prefill/decode; source and repeats stay visible.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess

import numpy as np
from analyze import frames, sha
from shadow import manifest_identity

ROOT = Path(__file__).resolve().parents[2]
TOP = [10, 32, 64, 128, 256]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def histogram(raw):
    ids = np.frombuffer(raw, dtype='<u2')
    if len(ids) % 10 or (len(ids) and int(ids.max()) >= 512):
        raise ValueError('invalid expert IDs')
    return np.bincount(ids, minlength=512).astype(np.int64)


def distribution(values):
    total = float(np.sum(values))
    if not total:
        return dict(active=0, entropy_bits=None, effective_experts=None,
                    **{'top' + str(n): None for n in TOP})
    p = np.asarray(values, dtype=float) / total
    entropy = float(-np.sum(p[p > 0] * np.log2(p[p > 0])))
    ordered = np.sort(p)[::-1]
    return dict(active=int(np.count_nonzero(p)), entropy_bits=entropy,
                effective_experts=2**entropy,
                **{'top' + str(n): float(ordered[:n].sum()) for n in TOP})


def top_set(values, n):
    # Include only observed IDs; zero-mass ties must not imply stability.
    return set(sorted(np.flatnonzero(values), key=lambda e: (-values[e], e))[:n])


def overlap(a, b, n=64):
    x, y = top_set(a, n), top_set(b, n)
    return len(x & y) / n if len(x) == len(y) == n else None


def read_request(path, manifest, checker, previous):
    digest = sha(path)
    check = subprocess.run([str(checker), str(path)], capture_output=True, text=True, timeout=120)
    if check.returncode not in (0, 3):
        raise ValueError('checker rejected ' + str(path) + check.stderr)
    verified = json.loads(check.stdout)
    assert verified['requests'] == 1
    metadata = json.loads(path.with_suffix('.json').read_text())
    expected_rows = [verified['committed_prefill_rows'], verified['committed_decode_rows']]
    count = np.zeros((2, 48, 512), dtype=np.int64)
    blocks = np.zeros_like(count)
    halves = np.zeros((2, 2, 48, 512), dtype=np.int64)
    observed = [0, 0]
    groups = [0, 0]
    route_hash = hashlib.sha256()
    events = frames(path)
    header = next(events)
    assert struct.unpack('<5I', header[:20]) == (1, 48, 512, 10, manifest['max_rows'])
    assert header[20:].hex() == ''.join(manifest[k] for k in ['binary_sha256', 'model_index_sha256', 'workload_sha256'])
    tokens = path.with_suffix('.tokens')
    token_digest = sha(tokens)
    pending = []
    for payload in events:
        kind = struct.unpack('<I', payload[:4])[0]
        body = payload[12:]
        if kind == 1:
            rid, rows = struct.unpack('<QQ', body[:16])
            assert rid == metadata['request_id'] and rows == metadata['prompt_tokens']
            assert tokens.stat().st_size == rows * 4 and body[16:].hex() == token_digest
        elif kind == 2:
            fid, stage, position, rows = struct.unpack('<QIQI', body)
            assert fid > previous[0]
            previous[0] = fid
            pending = []
        elif kind == 3:
            layer = struct.unpack('<I', body[:4])[0]
            raw = body[4:]
            assert len(raw) == rows * 10 * 2
            pending.append((layer, raw))
        elif kind == 4:
            if body == b'\1\1\1':
                assert len(pending) == 48
                s = stage - 1
                split = max(0, min(rows, expected_rows[s] // 2 - observed[s]))
                route_hash.update(struct.pack('<II', stage, rows))
                for layer, raw in pending:
                    h = histogram(raw)
                    count[s, layer] += h
                    blocks[s, layer] += h > 0
                    halves[s, 0, layer] += histogram(raw[:split*20])
                    halves[s, 1, layer] += histogram(raw[split*20:])
                    route_hash.update(struct.pack('<I', layer) + raw)
                observed[s] += rows
                groups[s] += 1
            pending = []
    assert observed == expected_rows
    for s in range(2):
        assert np.all(count[s].sum(axis=1) == expected_rows[s] * 10)
    # The checker also counts IDs from uncommitted forwards.
    assert int(count.sum()) == sum(expected_rows) * 48 * 10
    assert int(count.sum()) <= verified['route_ids']
    assert np.array_equal(halves.sum(axis=1), count)
    assert sha(path) == digest
    return metadata, verified, token_digest, route_hash.hexdigest(), count, blocks, halves, groups, digest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    output = args.output.resolve()
    if not any(output.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    output.mkdir(parents=True, exist_ok=False)
    plan = json.loads(args.plan.read_text())
    save(output / 'plan.json', plan)
    shutil.copy2(__file__, output / 'distribution.py')
    records, counts, appearances, halves_list = [], [], [], []
    seen = {}
    source_bindings = {}
    for batch in plan['sources']:
        directory = ROOT / batch['path']
        m = manifest_identity(directory, ROOT / plan['binary'])
        assert m['complete'] and m['failure'] == 'none'
        paths = sorted(directory.glob('request-*.bin'), key=lambda p: int(p.stem.split('-')[-1]))
        assert paths and len(paths) == m['requests_started'] == m['requests_published']
        assert not list(directory.glob('*.partial'))
        workload = json.loads((directory / 'workload.json').read_text())
        previous = [0]
        for rid, path in enumerate(paths, 1):
            meta, checked, token_hash, route_hash, count, blocks, halves, groups, digest = read_request(path, m, ROOT / plan['checker'], previous)
            assert meta['request_id'] == rid
            if batch['family'] in ['authored', 'pilot']:
                cases = workload if isinstance(workload, list) else workload.get('requests', workload.get('cases'))
                case = cases[rid - 1]
                scenario = case.get('domain', case.get('scenario', case.get('name', 'unknown')))
            else:
                scenario = batch['family']
            # Full token input equality within family, not claimed semantic dedup.
            key = (batch['family'], m['model_index_sha256'], m['model_config_sha256'], m['binary_sha256'], token_hash)
            duplicate = seen.get(key)
            successful = checked['successful_requests'] == 1
            index = len(records)
            if successful and duplicate is None:
                seen[key] = index
            records.append(dict(index=index, batch=batch['name'], family=batch['family'],
                scenario=scenario, request_id=rid, http_id=meta['http_id'],
                prompt_tokens=meta['prompt_tokens'], output_tokens=checked['output_tokens'],
                success=successful, input_sha256=token_hash, route_sha256=route_hash,
                duplicate_of=duplicate, repeated_route_equal=None if duplicate is None else route_hash == records[duplicate]['route_sha256'],
                phase_rows=[checked['committed_prefill_rows'],checked['committed_decode_rows']],
                phase_groups=groups, trace=str(path.relative_to(ROOT)), trace_sha256=digest))
            counts.append(count); appearances.append(blocks); halves_list.append(halves)
        for p in directory.iterdir():
            if p.is_file(): source_bindings[str(p.relative_to(ROOT))] = sha(p)
        print(batch['name'], len(paths), 'validated', flush=True)
    counts, appearances, halves = map(np.stack, [counts, appearances, halves_list])
    save(output / 'requests.json', records)
    save(output / 'source-bindings.json', source_bindings)
    np.savez_compressed(output / 'request-counts.npz', counts=counts, block_presence=appearances, halves=halves)
    populations = {}
    for batch in plan['sources']:
        populations['batch:' + batch['name']] = [r['index'] for r in records if r['batch'] == batch['name'] and r['success']]
    for family in sorted({r['family'] for r in records}):
        for view in ['all', 'unique_input']:
            populations[family + ':' + view] = [r['index'] for r in records if r['family'] == family and r['success'] and (view == 'all' or r['duplicate_of'] is None)]
    for scenario in sorted({r['scenario'] for r in records if r['family']=='authored'}):
        populations['scenario:' + scenario] = [r['index'] for r in records if r['family']=='authored' and r['scenario']==scenario and r['success'] and r['duplicate_of'] is None]
    for lower, upper in [(0,1024),(1024,8192),(8192,32768),(32768,100000),(100000,1000000)]:
        populations[f'authored_length:{lower}-{upper}'] = [r['index'] for r in records if r['family']=='authored' and r['success'] and r['duplicate_of'] is None and lower<=r['prompt_tokens']<upper]
    populations = {k:v for k,v in populations.items() if v}
    summary, stage_overlap, arrays = [], [], {}
    for name, indices in populations.items():
        c = counts[indices]
        pooled = c.sum(axis=0)
        presence = (c > 0).sum(axis=0)
        denom = c.sum(axis=3, keepdims=True)
        valid = denom > 0
        equal = np.divide(c, denom, out=np.zeros(c.shape, dtype=float), where=valid).sum(axis=0)
        equal = np.divide(equal, valid.sum(axis=0), out=np.zeros_like(equal), where=valid.sum(axis=0)>0)
        arrays[name + ':counts'] = pooled
        arrays[name + ':request_presence'] = presence
        arrays[name + ':block_presence'] = appearances[indices].sum(axis=0)
        arrays[name + ':equal_request'] = equal
        for s, stage in enumerate(['prefill', 'decode']):
            for layer in range(48):
                for weighting, values in [('route',pooled[s,layer]),('equal_request',equal[s,layer])]:
                    summary.append(dict(population=name, requests=len(indices), phase=stage, layer=layer,
                        weighting=weighting, phase_requests=int(valid[:,s,layer,0].sum()),
                        routes=int(pooled[s,layer].sum()), **distribution(values)))
        for layer in range(48):
            stage_overlap.append(dict(population=name,layer=layer,
                top64_overlap=overlap(pooled[0,layer],pooled[1,layer]),
                top128_overlap=overlap(pooled[0,layer],pooled[1,layer],128)))
    np.savez_compressed(output / 'population-counts.npz', **arrays)
    save(output / 'populations.json', populations)
    with (output / 'layers.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(summary[0]));writer.writeheader();writer.writerows(summary)
    # Dense human-readable per-expert table for the unique authored cohort.
    name='authored:unique_input'
    with (output / 'experts.csv').open('w') as f:
        writer=csv.writer(f);writer.writerow(['phase','layer','expert','selections','request_presence','block_presence','equal_request_share'])
        for s,stage in enumerate(['prefill','decode']):
            for layer in range(48):
                for e in range(512):
                    writer.writerow([stage,layer,e,*[arrays[name+':'+k][s,layer,e] for k in ['counts','request_presence','block_presence','equal_request']]])
    stability=[]
    unique=populations['authored:unique_input']
    for index in unique:
        for s,stage in enumerate(['prefill','decode']):
            values=[overlap(halves[index,s,0,l],halves[index,s,1,l]) for l in range(48)]
            valid=[v for v in values if v is not None]
            stability.append(dict(request=index,phase=stage,defined_layers=len(valid),top64_half_overlap=sum(valid)/len(valid) if valid else None))
    pairwise=[]
    scenarios=[k for k in populations if k.startswith('scenario:')]
    for i,a in enumerate(scenarios):
        for b in scenarios[i+1:]:
            for s,stage in enumerate(['prefill','decode']):
                vals=[overlap(arrays[a+':counts'][s,l],arrays[b+':counts'][s,l]) for l in range(48)]
                valid=[v for v in vals if v is not None]
                pairwise.append(dict(a=a,b=b,phase=stage,defined_layers=len(valid),top64_overlap=sum(valid)/len(valid) if valid else None))
    save(output/'stability.json',dict(prefill_decode=stage_overlap,within_request=stability,between_scenarios=pairwise))
    save(output/'verification.json',dict(passed=True,requests=len(records),successful=sum(r['success'] for r in records),
        repeats=sum(r['duplicate_of'] is not None for r in records),
        repeat_route_differences=sum(r['repeated_route_equal'] is False for r in records),
        route_ids=int(counts.sum()),model_binary_sha256=sha(ROOT/plan['binary']),numpy=np.__version__,
        tool_sha256=sha(Path(__file__)),plan_sha256=sha(args.plan)))


if __name__ == '__main__':
    main()
