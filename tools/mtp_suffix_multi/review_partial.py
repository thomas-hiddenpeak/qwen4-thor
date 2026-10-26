"""Read-only review of the stopped F phase; never turn partial data into PASS."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics


def read(path):
    return json.loads(path.read_text())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fields(line):
    return dict(re.findall(r'(\w+)=(\S+)', line))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    services = {
        'full-on': root/'feasibility-v2/run/01-full-on',
        'full-off': root/'feasibility-v3/run/02-full-off',
        'subset-off': root/'feasibility-v3/run/03-subset-off',
        'subset-on': root/'feasibility-v3/run/04-subset-on',
    }
    review = {'decision': 'NO_GO_FOR_AGENT_WORKLOAD',
              'performance_acceptance': False, 'measurement_complete': False,
              'statistics': 'One startup pair/head; descriptive only.',
              'monitor': {}, 'services': {}, 'comparisons': [], 'sources': {}}
    def source(path):
        review['sources'][str(path)] = digest(path)

    for version in ['feasibility', 'feasibility-v2', 'feasibility-v3']:
        summary = root/version/'run/telemetry-summary.json'
        post = root/version/'postflight.json'
        source(summary)
        source(post)
        s, p = read(summary), read(post)
        valid = (s['sample_attempts'] == s['flushed_samples'] and
                 s['unflushed_attempts'] == 0 and not s['failures_at_exit'] and
                 not p['monitor_failures'] and not p['cleanup_errors'] and
                 not p['final_monitor_errors'])
        assert valid, version
        review['monitor'][version] = {
            'accounting_valid': valid, 'flushed': s['flushed_samples'],
            'stage_status': p['status']}
    for key, directory in services.items():
        log = directory/'server.log'
        source(log)
        lines = log.read_text().splitlines()
        suffix = {r['request']: r for r in
                  map(fields, (l for l in lines if l.startswith('[suffix] ')))}
        all_ended = [fields(l) for l in lines if 'event=end ' in l]
        rejected = [r for r in all_ended if r['id'] == 'unregistered']
        ended = [r for r in all_ended if r['id'] != 'unregistered']
        assert len(rejected) == (8 if key == 'subset-on' else 0)
        assert all(r['seq'] == '-1' and r['released'] == '0' and
                   r['outcome'] == 'error' for r in rejected)
        accepts = [fields(l) for l in lines if l.startswith('[mtp-accept] ')]
        batches = [{k: int(v) for k, v in fields(l).items()}
                   for l in lines if l.startswith('[mtp-batches] ')]
        cache = [fields(l) for l in lines if l.startswith('[suffix-classes] ')]
        final = [json.loads(l.split('] ', 1)[1]) for l in lines
                 if l.startswith('[startup-reservation] ') and
                 '"event":"finalized"' in l]
        assert len(final) == 1 and final[0]['status'] == 'PASS'
        assert final[0]['allocated'] == final[0]['freed']
        assert not final[0]['release_unknown']
        assert all(r['released'] == '1' and r['outcome'] in
                   ['success', 'cancelled'] for r in ended)
        verify_rows = sum(r['rows']*r['steps'] for r in batches)
        assert verify_rows == sum(sum(map(int, r['draft_counts'].split(',')))
                                  for r in accepts)
        assert sum(r['suffix']*r['steps'] for r in batches) == sum(
            int(r['hits']) for r in suffix.values())
        assert len(accepts) == len(ended)
        if suffix:
            assert len(suffix) == len(ended) and len(cache) == 2
            for shared, local in [('decodes', 'cache_decodes'),
                                  ('lookups', 'cache_lookups'),
                                  ('allocations', 'cache_allocations'),
                                  ('checks', 'numeric_checks')]:
                assert int(cache[-1][shared]) == sum(
                    int(r[local]) for r in suffix.values())
        cases = {}
        for case in ['repeat-code', 'repeat-text', 'mixed', 'nonrepeat']:
            path = directory/case/'result.json'
            source(path)
            value = read(path)
            rows = value['rows']
            assert len(rows) == 4 and all(r['success'] and
                r['output_tokens'] == 256 and r['finish'] == ['length'] and
                r['timing']['within_client_boundaries'] for r in rows)
            local = [suffix[r['response_id']] for r in rows if
                     r['response_id'] in suffix]
            value['suffix_cost_ns'] = {k: sum(int(r[k]) for r in local) for k in
                ['init_ns', 'append_ns', 'query_ns', 'release_ns', 'numeric_ns']}
            value['suffix_queries'] = sum(int(r['queries']) for r in local)
            value['suffix_hits'] = sum(int(r['hits']) for r in local)
            value['suffix_accepted_0_to_3'] = [sum(
                int(r['accepted'].split(',')[i]) for r in local) for i in range(4)]
            value['index_peak_bytes_per_request'] = [int(r['peak_bytes']) for r in local]
            cases[case] = value
        review['services'][key] = dict(cases=cases, ended=len(ended),
            rejected_before_registration=len(rejected),
            batches=batches, request_ledger_valid=True, cache=cache,
            verify_input_rows=verify_rows*4,
            neural_proposal_token_rows=2*sum((r['rows']-r['suffix'])*r['steps']
                                            for r in batches),
            accepted_extend_rows=sum(sum((i+1)*int(n) for i, n in
                enumerate(r['draft_counts'].split(','))) for r in accepts))
    for head in ['full', 'subset']:
        for case in ['repeat-code', 'repeat-text', 'mixed', 'nonrepeat']:
            on = review['services'][head+'-on']['cases'][case]
            off = review['services'][head+'-off']['cases'][case]
            outputs = lambda c: sorted((r['prompt_sha256'], r['output_sha256'])
                                       for r in c['rows'])
            review['comparisons'].append(dict(head=head, case=case,
                off_tps=off['throughput'], on_tps=on['throughput'],
                change_percent=100*(on['throughput']/off['throughput']-1),
                output_multisets_equal=outputs(on) == outputs(off),
                median_ttft_change_ms=1000*(statistics.median(
                    r['ttft'] for r in on['rows'])-statistics.median(
                    r['ttft'] for r in off['rows']))))
    queue = services['subset-on']/'queue-16/responses.json'
    source(queue)
    rows = read(queue)
    review['queue_16'] = dict(attempted=len(rows),
        successful=sum(bool(r['success']) for r in rows),
        failed=sum(not r['success'] for r in rows),
        source_limit='max(8, max_seq*2) = 8',
        attribution='Entry cap accounts for observed 8 rejections; client DB '
                    'does not retain HTTP error status/body. No claim of captured 503 body.')
    capacity = root/'capacity-review/capacity-v2.jsonl'
    source(capacity)
    review['capacity'] = [json.loads(l) for l in capacity.read_text().splitlines()]
    review['capacity_scope'] = 'Production CPU planner at max_prefill8192; '
    review['capacity_scope'] += 'not a long-context GPU or OOM guarantee.'
    review['missing'] = ['32-way burst', 'subset cancellation/recovery',
        'second concurrent block', 'five tiers and plain protections',
        'actual >64K main with 32K-64K subagent concurrency']
    review['generated_http_ledger'] = dict(attempted=101, admitted=93,
        normal_completed=92, expected_cancelled=1, rejected=8,
        remaining_unexecuted_F=35,
        note='Includes full lifecycle control/cancel/recovery; stage counter '
             'only advances after a whole service block and is not this ledger.')
    review['unified_ram'] = {}
    for version in ['feasibility-v2', 'feasibility-v3']:
        path = root/version/'run/unified-memory.jsonl'
        source(path)
        gauges = {}
        for line in path.open():
            sample = json.loads(line)
            for key, number in re.findall(r'^(MemFree|MemAvailable|Cached):\s+(\d+)',
                                           sample['meminfo'], re.MULTILINE):
                value = int(number)*1024
                old = gauges.setdefault(key, [value, value])
                old[0], old[1] = min(old[0], value), max(old[1], value)
        review['unified_ram'][version] = gauges
    review['ram_scope'] = 'System gauges [min,max] bytes overlap CUDA/RSS; '
    review['ram_scope'] += 'never sum these or attribute all system usage to q4t.'
    with args.output.open('x') as stream:
        json.dump(review, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    print(json.dumps({k: review[k] for k in
        ['decision', 'comparisons', 'queue_16', 'generated_http_ledger']}, indent=2))


if __name__ == '__main__':
    main()
