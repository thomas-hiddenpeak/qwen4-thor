"""Summarize single-stream plain-decode traces produced by profile_http.py.

Boundaries rely on the current serve ABI: one BF16 vocabulary-row D2H marks
prefill completion; one int32 argmax D2H marks each scheduled decode step.
Fail rather than silently applying these assumptions to another trace shape.
GPU busy time is an interval union, not the sum of overlapping kernel times.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3


def union_ns(intervals):
    end = -1
    total = 0
    for a, b in sorted(intervals):
        if b > end:
            total += b - max(a, end)
            end = b
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    repo = Path(__file__).resolve().parents[2]
    if not any(root.is_relative_to(repo / d) for d in ['build', '.q4t-work']):
        parser.error('trace root must be under build/ or .q4t-work/')
    status = json.loads((root / 'exit.json').read_text())
    if (status['failure'] is not None or status['shutdown'] != 0 or
            status['launcher'] != 0 or not status['trace_exports_complete']):
        parser.error('requires a completed, cleanly shut down capture and export')
    cases = json.loads((root / 'results.json').read_text())
    results = []
    for case in cases:
        length = case['length']
        path = root / f'context-{length}/trace.sqlite'
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            names = dict(db.execute('select id,value from StringIds'))
            kinds = db.execute('select * from ENUM_CUDA_MEMCPY_OPER').fetchall()
            if not any(r[0] == 2 and 'Device-to-Host' in str(r) for r in kinds):
                raise RuntimeError('unrecognized CUDA copy-kind enum')
            kernels = db.execute('select start,end,shortName,gridX,gridY,gridZ '
                                 'from CUPTI_ACTIVITY_KIND_KERNEL order by start').fetchall()
            copies = db.execute('select start,end,bytes,copyKind '
                                'from CUPTI_ACTIVITY_KIND_MEMCPY order by start').fetchall()
            memsets = db.execute('select start,end from CUPTI_ACTIVITY_KIND_MEMSET').fetchall()
            api = db.execute('select start,end,nameId from CUPTI_ACTIVITY_KIND_RUNTIME').fetchall()
        pref = [r for r in copies if r[2:] == (248320 * 2, 2)]
        tokens = [r for r in copies if r[2:] == (4, 2)]
        embeds = [r for r in kernels if names[r[2]] == 'EmbedLookupKernel']
        argmax = [r for r in kernels if names[r[2]] == 'ArgmaxBf16ReduceKernel']
        if len(pref) != 1 or not tokens or len(argmax) != len(tokens):
            raise RuntimeError('ambiguous prefill/decode D2H or argmax markers')
        boundary = pref[0][1]
        prefix = [r for r in embeds if r[0] < boundary]
        decoding = [r for r in embeds if r[0] > boundary]
        if (sum(r[3] for r in prefix) != length or
                len(decoding) != len(tokens) or any(r[3] != 1 for r in decoding) or
                any(r[0] <= boundary for r in tokens)):
            raise RuntimeError('embedding grid/count does not match phase markers')
        all_events = [(r[0], r[1]) for r in kernels + copies + memsets]
        item = {'length': length, 'decode_forwards': len(tokens),
                'prefill_chunks': [r[3] for r in prefix],
                'profiled_ttft_s': case['ttft'],
                'profiled_e2e_s': case['latency'],
                'last_forward_ms': (tokens[-1][1] - decoding[-1][0]) / 1e6,
                'stages': {}}
        for label, start, end in [('prefill', min(r[0] for r in all_events), boundary),
                                   ('decode', boundary, tokens[-1][1])]:
            ks = [r for r in kernels if start <= r[0] and r[1] <= end]
            cs = [r for r in copies if start <= r[0] and r[1] <= end]
            ms = [r for r in memsets if start <= r[0] and r[1] <= end]
            intervals = [(r[0], r[1]) for r in ks + cs + ms]
            busy = union_ns(intervals)
            span = end - start
            groups = defaultdict(lambda: [0, 0])
            for a, b, name, *_ in ks:
                groups[names[name]][0] += 1
                groups[names[name]][1] += b - a
            kernel_total = sum(r[1] - r[0] for r in ks)
            top = [{'name': n, 'calls': v[0], 'total_ms': v[1] / 1e6,
                    'kernel_sum_pct': 100 * v[1] / kernel_total}
                   for n, v in sorted(groups.items(), key=lambda x: -x[1][1])]
            apis = defaultdict(lambda: [0, 0])
            for a, b, name in api:
                if a < end and b > start:
                    apis[names[name]][0] += 1
                    apis[names[name]][1] += min(b, end) - max(a, start)
            item['stages'][label] = {
                'span_ms': span / 1e6, 'gpu_busy_ms': busy / 1e6,
                'gpu_gap_ms': (span - busy) / 1e6,
                'gpu_busy_pct': 100 * busy / span,
                'kernel_count': len(ks), 'kernel_sum_ms': kernel_total / 1e6,
                'kernel_union_ms': union_ns([(r[0], r[1]) for r in ks]) / 1e6,
                'copy_sum_ms': sum(r[1] - r[0] for r in cs) / 1e6,
                'memset_sum_ms': sum(r[1] - r[0] for r in ms) / 1e6,
                'expert_count_d2h_calls': sum(r[2:] == (2048, 2) for r in cs),
                'top_kernels': top,
                'cuda_api': [{'name': n, 'calls': v[0], 'overlapping_ms': v[1] / 1e6}
                             for n, v in sorted(apis.items(), key=lambda x: -x[1][1])]}
        # Last kernel in each prefill chunk: inspect names/grids before
        # attributing it to lm_head; intermediate chunks deliberately skip head.
        item['prefill_chunk_final_kernels'] = []
        for i, emb in enumerate(prefix):
            end = prefix[i + 1][0] if i + 1 < len(prefix) else boundary
            k = max((r for r in kernels if emb[0] <= r[0] < end), key=lambda r: r[1])
            item['prefill_chunk_final_kernels'].append(
                {'chunk': i, 'name': names[k[2]], 'grid': list(k[3:]), 'ms': (k[1] - k[0]) / 1e6})
        results.append(item)
        (root / 'timeline-summary.json').write_text(json.dumps(results, indent=2) + '\n')
        print(length, 'decode forwards', len(tokens), 'last forward ms', round(item['last_forward_ms'], 3))
        for label, stage in item['stages'].items():
            print(label, 'window_s', round(stage['span_ms'] / 1000, 3),
                  'busy_pct', round(stage['gpu_busy_pct'], 2),
                  'kernels', stage['kernel_count'], 'D2H counts', stage['expert_count_d2h_calls'])
            for k in stage['top_kernels'][:5]:
                print(' ', k['name'], round(k['total_ms'], 2), round(k['kernel_sum_pct'], 2))


if __name__ == '__main__':
    main()
