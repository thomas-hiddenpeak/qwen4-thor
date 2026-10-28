"""Offline review of the frozen scoring phase; never launches inference."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def memory(raw):
    return {k: int(v) * 1024 for k, v in
            re.findall(r'^(\w+):\s+(\d+) kB', raw, re.M)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    base, run = args.phase, args.phase / 'run'
    plan, state = read(base / 'plan.json'), read(run / 'state.json')
    assert state['status'] == 'MEASURED_REQUIRES_REVIEW'
    assert state['requests_completed'] == 16 and state['loads_started'] == 8
    assert state['elapsed_seconds'] < 5400
    assert state['finished'] < plan['task_deadline_unix']
    assert not state['monitor_failures']
    for name, expected in plan['files'].items():
        assert sha(Path(name)) == expected, name
    seal = read(run / 'files.json')
    for name, expected in seal.items():
        assert sha(run / name) == expected, name
    post = read(base / 'postflight.json')
    telemetry = read(run / 'telemetry-summary.json')
    assert not post['final_monitor_errors'] and not post['cleanup_errors']
    assert post['final_monitor_barrier']
    assert telemetry['flushed_samples'] == telemetry['sample_attempts']
    assert not telemetry['sampler_failure'] and not telemetry['failures_at_exit']
    assert telemetry['exit_pending']['active'] == 0
    assert read(base / 'lease/exit.json')['status'] == 'COMPLETED'
    guard_events = [json.loads(line) for line in
                    (base / 'lease/events.jsonl').read_text().splitlines()]
    assert all(not e.get('conflicts') and not e.get('unreadable_processes')
               for e in guard_events)
    windows, requests, timing, accepts, contracts = [], [], {}, {}, {}
    for step in plan['steps']:
        name = step['name']
        if step['kind'] == 'contract':
            raw = (run / (name + '-driver.log')).read_text()
            assert raw.count('PASS scoring_reference') == 4
            assert 'PASS prefix_state=1' in raw and 'FAIL' not in raw
            contracts[name] = raw.splitlines()
        elif step['http_requests']:
            folder = run / name
            assert read(folder / 'exit.json')['http_output_checks_passed']
            for audit in ['session-audit.json', 'prefix-audit.json']:
                assert read(folder / audit)['passed'], (name, audit)
            server_audit = read(folder / 'server-audit.json')
            assert server_audit['admitted_requests'] == step['http_requests']
            assert server_audit['finalization']['status'] == 'PASS'
            events = [json.loads(line) for line in
                      (folder / 'events.jsonl').read_text().splitlines()]
            start = next(e for e in events if e['event'] == 'server_started')
            stop = next(e for e in events
                        if e['event'] == 'server_after_shutdown')
            windows.append(dict(step=name, pid=str(start['pid']),
                start=start['unix_seconds'], stop=stop['unix_seconds']))
            rows = read(folder / 'results.json')
            assert len(rows) == step['http_requests']
            audit = read(folder / 'session-audit.json')
            accepts[name] = {r['request']: r['neural_accepted']
                             for r in audit['suffix']}
            for case, result in zip(step['cases'], rows):
                row = dict(result['rows'][0])
                assert all(row[k] == case[k] for k in
                           ['prompt_sha256', 'input_tokens', 'output_sha256'])
                assert row['success'] == 1 and row['output_tokens'] == 256
                assert row['finish'] == ['length']
                row.pop('text')
                row.update(step=name, head=step['head'], arm=step['arm'],
                           mode=step['mode'], tier=result['id'])
                row['post_ttft_seconds'] = row['latency'] - row['ttft']
                row['post_ttft_tokens_per_second'] = 255 / row['post_ttft_seconds']
                row['e2e_tokens_per_second'] = 256 / row['latency']
                requests.append(row)
            if step['mode'] == 'diagnostic':
                records = read(folder / 'timing.json')
                assert len(records) == 200 and all(r['ok'] == '1' for r in records)
                sums = collections.defaultdict(lambda: collections.defaultdict(float))
                for record in records:
                    for k in ['wall_ms', 'cpu_ms', 'stream_ms']:
                        sums[record['label']][k.replace('_ms', '_seconds')] += float(record[k]) / 1000
                timing[name] = dict(sums)
    assert len(requests) == 16 and len(windows) == 8 and len(contracts) == 2
    gate = read(run / 'benefit-gate.json')
    assert gate['passed']
    comparisons = []
    for head in ['full', 'subset']:
        for tier in ['context-8192', 'context-45056', 'context-204800']:
            pair = {r['arm']: r for r in requests if r['head'] == head and
                    r['tier'] == tier and r['mode'] == 'normal'}
            parent, candidate = pair['parent'], pair['candidate']
            accept_same = (accepts[parent['step']][parent['response_id']] ==
                           accepts[candidate['step']][candidate['response_id']])
            comparisons.append(dict(head=head, tier=tier,
                ttft_reduction=1-candidate['ttft']/parent['ttft'],
                e2e_reduction=1-candidate['latency']/parent['latency'],
                decode_rate_change=candidate['post_ttft_tokens_per_second']/parent['post_ttft_tokens_per_second']-1,
                acceptance_equal=accept_same))
    resources = {'phase': {}}
    counts = collections.Counter()
    source_statuses = collections.Counter()
    unavailable = {}
    def observe(summary, sample, pid=None):
        info = memory(sample['meminfo'])
        available = info['MemAvailable']
        summary['samples'] = summary.get('samples', 0) + 1
        summary['minimum_available_bytes'] = min(summary.get('minimum_available_bytes', available), available)
        occupied = info['MemTotal'] - available
        summary['maximum_total_minus_available_bytes'] = max(summary.get('maximum_total_minus_available_bytes', 0), occupied)
        if pid:
            proc = sample['processes'].get(pid, {})
            status = memory(proc.get('status', ''))
            for key in ['VmRSS', 'VmSwap']:
                if key in status:
                    summary['maximum_' + key + '_bytes'] = max(summary.get('maximum_' + key + '_bytes', 0), status[key])
    with (run / 'unified-memory.jsonl').open() as stream:
        for line in stream:
            sample = json.loads(line)
            counts['samples'] += 1
            observe(resources['phase'], sample)
            for unavailable_source in sample['unavailable']:
                unavailable[unavailable_source['label']] = unavailable_source
            for source in sample['source_values'].values():
                source_statuses[source['status']] += 1
            for window in windows:
                if window['start'] <= sample['unix_seconds'] <= window['stop']:
                    observe(resources.setdefault(window['step'], {}), sample,
                            window['pid'])
    assert counts['samples'] == telemetry['flushed_samples']
    assert set(source_statuses) == {'OK'}
    root = Path(__file__).resolve().parents[2]
    assert sha(root / 'build/q4t') == plan['parent_sha256']
    assert sha(root / 'build/q4t.release.json') == sha(base / 'frozen/parent.release.json')
    review = dict(
        decision='RESEARCH_GO_REQUIRES_RELEASE_VALIDATION',
        evidence_validity='VALID_WITH_SAMPLING_LIMITS',
        measurement_completeness='FROZEN_16_HTTP_8_SERVICES_2_CONTRACTS_COMPLETE_NOT_RELEASE_MATRIX',
        statistics='DESCRIPTIVE_SINGLE_PAIR_NO_NONINFERIORITY_CLAIM',
        automatic_release=False, default_binary_unchanged=True,
        state=state, plan_sha256=sha(base / 'plan.json'),
        candidate_sha256=plan['candidate_sha256'], parent_sha256=plan['parent_sha256'],
        source_commit=plan['source_commit'], frozen_files_verified=len(plan['files']),
        run_seal_files_verified=len(seal), postflight=post,
        telemetry=telemetry, guard_events=len(guard_events),
        resources=resources, source_status_counts=dict(source_statuses),
        unavailable_sources=list(unavailable.values()),
        requests=requests, comparisons=comparisons, timing=timing,
        acceptance=accepts, contracts=contracts,
        scope_notes=[
            'CUDA stream elapsed includes submission gaps; it is not pure kernel active time.',
            'Nested timing intervals overlap and must not be added.',
            'Host wall minus thread CPU is not a direct GPU wait measurement.',
            'Resource GB uses decimal bytes; RSS and CUDA allocations share physical RAM and must not be added.',
            'Four configured slots, serial HTTP; direct fixture uses four representative target layers and real draft.',
            'No new plain-decode/five-tier/full quality/cache-hit HTTP/repeated-agent net-benefit claims.',
            'Old SIMT versus GEMM rounding differs; fixed outputs and independent state references passed.',
        ])
    args.output.write_text(json.dumps(review, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(decision=review['decision'], comparisons=comparisons,
                         timing=timing, resources=resources), indent=2))


if __name__ == '__main__':
    main()
