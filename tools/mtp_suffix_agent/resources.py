"""Summarize existing agent evidence without starting inference.

This post-run descriptive report does not change the frozen performance rules.
Physical RAM is reported once from meminfo; process RSS and cache are separate
observations and must not be added to it. GPU section times were not collected.
"""
import argparse
import json
from pathlib import Path
import re


def memory(raw):
    return {key: int(value) * 1024 for key, value in
            re.findall(r"^(\w+):\s+(\d+) kB$", raw, re.MULTILINE)}


def add_sample(summary, row):
    values = memory(row['meminfo'])
    summary['samples'] = summary.get('samples', 0) + 1
    observations = {
        'physical_used_including_reclaimable_bytes':
            values['MemTotal'] - values['MemFree'],
        'total_minus_available_bytes':
            values['MemTotal'] - values['MemAvailable'],
        'cached_bytes': values['Cached'],
        'buffers_bytes': values['Buffers'],
        'kernel_reclaimable_bytes': values['KReclaimable'],
        'swap_used_bytes': values['SwapTotal'] - values['SwapFree'],
    }
    for key, value in observations.items():
        summary[key] = max(summary.get(key, 0), value)
    for key in ['MemFree', 'MemAvailable']:
        name = 'minimum_' + key + '_bytes'
        summary[name] = min(summary.get(name, values[key]), values[key])
    summary['MemTotal_bytes'] = values['MemTotal']
    for proc in row.get('processes', {}).values():
        argv = proc.get('identity', {}).get('argv', [])
        if not argv or Path(argv[0]).name != 'q4t':
            continue
        rss = memory(proc.get('status', '')).get('VmRSS', 0)
        summary['q4t_rss_bytes'] = max(summary.get('q4t_rss_bytes', 0), rss)


def histogram(rows, key):
    total = [0] * 4
    for row in rows:
        counts = list(map(int, row[key].split(',')))
        if len(counts) != 4:
            raise ValueError('non-k3 acceptance histogram')
        total = [a + b for a, b in zip(total, counts)]
    return total


def workload(case, audit):
    ids = {r['response_id'] for r in case['rows']}
    suffix = [r for r in audit['suffix'] if r['request'] in ids]
    accepted = [r for r in audit['acceptance'] if r['request'] in ids]
    counts = histogram(accepted, 'draft_counts')
    rounds = sum(counts)
    hits = sum(int(r['hits']) for r in suffix)
    queries = sum(int(r['queries']) for r in suffix)
    result = dict(requests=len(ids), acceptance=counts,
                  verify_sequence_steps=rounds, verify_token_rows=4 * rounds,
                  suffix_hits=hits, suffix_queries=queries,
                  hit_fraction=hits / queries if queries else None,
                  suffix_acceptance=histogram(suffix, 'accepted'),
                  neural_acceptance=(histogram(suffix, 'neural_accepted')
                                     if suffix else counts),
                  neural_draft_token_rows=2 * (rounds - hits),
                  accepted_extend_rows_upper_bound=
                      sum((i + 1) * n for i, n in enumerate(counts)),
                  finish_reasons=[r['finish'] for r in case['rows']])
    # Terminal corrections skip extend. Length-only cases have no EOS skip;
    # the histogram upper bound is exact for their MTP accepted extensions.
    if all(r['finish'] == ['length'] for r in case['rows']):
        result['accepted_extend_token_rows'] = result[
            'accepted_extend_rows_upper_bound']
    for field in ['init_ns', 'append_ns', 'query_ns', 'release_ns', 'numeric_ns',
                  'indexed', 'candidates', 'compared', 'd0_mismatch',
                  'short_rejected', 'numeric_rejected', 'cache_decodes']:
        result[field] = sum(int(r[field]) for r in suffix)
    result['sum_request_index_peak_bytes'] = sum(
        int(r['peak_bytes']) + int(r['object_bytes']) for r in suffix)
    result['shared_class_cache_peak_bytes'] = max(
        (int(r['cache_bytes']) for r in suffix), default=0)
    result['index_peak_note'] = 'sum of per-request peaks; not a simultaneous sample'
    result['timing_note'] = 'CPU wall clocks include classification and lock waits; numeric_ns is included in query_ns; no independent GPU timings'
    return result


def summarize(root):
    services, windows = {}, []
    for audit_path in sorted(root.glob('*/agent-audit.json')):
        directory = audit_path.parent
        audit = json.loads(audit_path.read_text())
        cases = json.loads((directory / 'results.json').read_text())
        events = [json.loads(line) for line in
                  (directory / 'events.jsonl').read_text().splitlines()]
        start = next(e['monotonic_seconds'] for e in events
                     if e['event'] == 'server_before_start')
        end = next(e['monotonic_seconds'] for e in events
                   if e['event'] == 'server_after_shutdown')
        service = dict(memory={}, cases={})
        services[directory.name] = service
        windows.append((start, end, service['memory']))
        service['batches'] = audit['batches']
        service['actual_verify_batch_calls'] = (
            sum(b['steps'] for b in audit['batches'])
            if audit.get('independent_batch_work_audit', True) else None)
        service['streamed_prefill_trunk_max_bytes'] = max(
            (int(r['trunk_bytes']) for r in audit['streamed_prefill']), default=0)
        for case in cases:
            entry = workload(case, audit)
            entry['memory'] = {}
            service['cases'][case['id']] = entry
            windows.append((min(r['start'] for r in case['rows']),
                            max(r['end'] for r in case['rows']), entry['memory']))
    overall = {}
    with (root / 'unified-memory.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            add_sample(overall, row)
            for start, end, summary in windows:
                if start <= row['monotonic'] <= end:
                    add_sample(summary, row)
    return dict(overall_memory=overall, services=services,
                limitations=['One-second observations do not bound unsampled peaks.',
                             'MemTotal-Free includes reclaimable driver/kernel allocations and file cache.',
                             'RAM includes the whole host; RSS is not an additive GPU metric.',
                             'No independent GPU verify/extend times were collected.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads((args.root / 'state.json').read_text())
    if state['status'] == 'RUNNING':
        raise SystemExit('Wait for stage finalization before reading full telemetry.')
    result = summarize(args.root)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
