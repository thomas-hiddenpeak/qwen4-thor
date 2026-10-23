#!/usr/bin/env python3
"""Offline cost audit of completed numeric-suffix stage steps.

Reads existing evidence only. It never starts a model, HTTP request, benchmark,
or test and refuses analysis before the first real HTTP quality gate completed.
"""
import argparse
import collections
import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics


BASE = Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/'
            'suffix-numeric-gate-20261023')
TIMING = re.compile(
    r'^\[mtp-timing\] B=(\d+) draft=([\d.]+) verify=([\d.]+) '
    r'extend\+accept=([\d.]+) ms \(k=(\d+) a=\[([^]]*)\]\) suffix=([01])'
    r'(?: gpu\[ draft=([\d.]+) verify=([\d.]+) extend=([\d.]+) \])?$')
HISTOGRAM = re.compile(r'^\[mtp-accept\] request=(\S+) draft_counts=([\d,]+)$')
INPUTS = {}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path):
    data = path.read_bytes()
    INPUTS[str(path)] = hashlib.sha256(data).hexdigest()
    return data.decode()


def read_json(path):
    return json.loads(read_text(path))


def fields(line):
    result = dict(re.findall(r'(\w+)=(\S+)', line))
    for key, value in result.items():
        if re.fullmatch(r'-?\d+', value):
            result[key] = int(value)
        elif re.fullmatch(r'-?\d+\.\d+', value):
            result[key] = float(value)
    return result


def stats(values):
    values = [x for x in values if x is not None]
    if not values:
        return dict(count=0, sum=None, minimum=None, median=None, maximum=None)
    return dict(count=len(values), sum=sum(values), minimum=min(values),
                median=statistics.median(values), maximum=max(values))


def require(condition, message, errors):
    if not condition:
        errors.append(message)


def response_records(output, step, errors):
    """Join by exact response ID, never by assumed request order."""
    records = {}
    cases = {c['id']: c for c in step.get('cases', [])}
    for path in sorted(output.rglob('responses.json')):
        case = path.parent.name
        for row in read_json(path):
            request = row.get('response_id')
            require(bool(request) and request not in records,
                    f'duplicate/missing response identity in {path}', errors)
            if not request:
                continue
            records[request] = dict(
                response_id=request, case=case,
                role=cases.get(case, {}).get('role', step['kind']),
                actual_input=row.get('actual_input'),
                actual_output=row.get('actual_output'),
                requested_max_tokens=row.get('requested_max_tokens'),
                finish=row.get('finish'),
                prompt_sha256=row.get('prompt_sha256'),
                output_sha256=hashlib.sha256(row.get('text', '').encode()).hexdigest(),
                success=row.get('success'), ttft_seconds=row.get('ttft'),
                latency_seconds=row.get('latency'), evidence=str(path))
    if step['kind'] == 'lifecycle':
        # The direct lifecycle runner retains raw SSE instead of evalscope
        # responses.json. Error-only cancellation may not retain usage.
        for label in ['control', 'cancel', 'recovery']:
            request = f"suffix-{step['head']}-{label}"
            path = output / f'{request}-response.json'
            if not path.is_file():
                continue
            response = read_json(path)
            chunks, finishes, usage, wire_ids, wire_errors = [], [], [], set(), []
            for line in response.get('body', '').splitlines():
                if not line.startswith('data: {'):
                    continue
                message = json.loads(line[6:])
                if message.get('id'):
                    wire_ids.add(message['id'])
                if message.get('error'):
                    wire_errors.append(message['error'])
                if message.get('usage'):
                    usage.append(message['usage'])
                for choice in message.get('choices', []):
                    content = choice.get('delta', {}).get('content')
                    if content:
                        chunks.append(content)
                    if choice.get('finish_reason'):
                        finishes.append(choice['finish_reason'])
            require(not wire_ids or wire_ids == {request},
                    f'lifecycle response ID mismatch: {request}', errors)
            records[request] = dict(
                response_id=request, case=label, role='lifecycle_contract',
                actual_input=usage[-1].get('prompt_tokens') if usage else None,
                actual_output=usage[-1].get('completion_tokens') if usage else None,
                requested_max_tokens=256, finish=finishes,
                output_sha256=hashlib.sha256(''.join(chunks).encode()).hexdigest(),
                usage=usage, wire_errors=wire_errors,
                observed_nonempty_chunks=len(chunks), evidence=str(path))
    return records


def parse_log(output, errors):
    text = read_text(output / 'server.log')
    requests = collections.OrderedDict()
    table, startup = [], []
    active = None

    def get(request):
        return requests.setdefault(request, dict(request=request, timing=[]))

    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith('[suffix-classes] '):
            table.append(dict(fields(line), line=number))
        if line.startswith('[q4t][startup] suffix_token_classes '):
            startup.append(dict(line=number, raw=line))
        if line.startswith('[q4t][request] '):
            event = fields(line)
            request = event.get('id')
            if not request:
                continue
            event['line'] = number
            record = get(request)
            if event.get('event') == 'admitted':
                require(active in (None, request),
                        f'overlapping B1 admitted requests at {output}:{number}', errors)
                active = request
                record['admitted'] = event
            if event.get('event') == 'end':
                record['end'] = event
                if active == request:
                    active = None
            continue
        match = HISTOGRAM.fullmatch(line)
        if match:
            record = get(match[1])
            require('acceptance' not in record,
                    f'duplicate acceptance summary {match[1]}', errors)
            record['acceptance'] = [int(v) for v in match[2].split(',')]
            record['acceptance_line'] = number
            continue
        if line.startswith('[suffix] '):
            row = fields(line)
            request = row.get('request')
            if 'disabled' in row:
                errors.append(f'suffix disabled at {output}:{number}')
            elif request:
                record = get(request)
                require('suffix' not in record,
                        f'duplicate suffix summary {request}', errors)
                for name in ['accepted', 'neural_accepted']:
                    if name in row:
                        row[name] = [int(x) for x in str(row[name]).split(',')]
                record['suffix'] = dict(row, line=number)
            continue
        if line.startswith('[mtp-timing] '):
            match = TIMING.fullmatch(line)
            require(match is not None and active is not None,
                    f'unbound or malformed timing at {output}:{number}', errors)
            if not match or active is None:
                continue
            acceptance = [int(x) for x in match[6].split(',')]
            require(int(match[1]) == 1 and int(match[5]) == 3 and
                    len(acceptance) == 1 and 0 <= acceptance[0] <= 3,
                    f'unexpected timing shape at {output}:{number}', errors)
            row = dict(line=number, B=int(match[1]), k=int(match[5]),
                       accepted=acceptance[0], suffix=bool(int(match[7])),
                       wall_ms=dict(draft=float(match[2]), verify=float(match[3]),
                                    extend_accept=float(match[4])),
                       gpu_event_ms=None)
            if match[8] is not None:
                row['gpu_event_ms'] = dict(draft=float(match[8]), verify=float(match[9]),
                                          extend_accept=float(match[10]))
            get(active)['timing'].append(row)
    return requests, table, startup


def derive_work(row, step, errors):
    counts = row.get('acceptance')
    waits = row.get('end', {}).get('scheduler_waits')
    response = row.get('response', {})
    if counts is None:
        row['work'] = dict(scope='ordinary decode / no MTP summary',
                           observed_scheduler_waits=waits)
        return
    require(len(counts) == 4, 'acceptance histogram is not k3: ' + row['request'], errors)
    if len(counts) != 4:
        return
    verify = sum(counts)
    accepted = sum(i * n for i, n in enumerate(counts))
    committed = verify + accepted
    suffix = row.get('suffix', {})
    hits = suffix.get('hits', 0)
    require(0 <= hits <= verify, 'invalid hit count: ' + row['request'], errors)
    neural = 2 * (verify - hits)
    tail_waits = None if waits is None else waits - verify
    require(tail_waits is None or tail_waits >= 0,
            'scheduler waits smaller than verify count: ' + row['request'], errors)
    countable_length = (response.get('finish') == ['length'] and
                        isinstance(response.get('actual_output'), int) and
                        row.get('end', {}).get('outcome') == 'success')
    work = dict(
        scope='whole iterative generation; excludes initial main/MTP prefill; includes first MTP round',
        verify_calls_T4=verify, accepted_draft_tokens=accepted,
        committed_by_MTP_tokens=committed,
        checkpoint_restore_calls=sum(counts[:3]),
        suffix_hits=hits, neural_MTP_rounds=verify-hits,
        neural_proposal_forward_calls_T1=neural,
        observed_scheduler_waits=waits,
        non_MTP_scheduler_waits=tail_waits,
        draft_extend_calls_upper=verify,
        draft_extend_calls_lower=max(0, verify-1),
        draft_extend_rows_upper=committed,
        draft_extend_rows_lower=max(0, committed-max(
            (a+1 for a, n in enumerate(counts) if n), default=0)),
        exact_nonterminal_length_counts=countable_length)
    if countable_length:
        tail_tokens = response['actual_output'] - committed
        require(tail_tokens >= 1,
                'successful length case has no final pending tail: ' + row['request'], errors)
        tail_forwards = tail_tokens - 1
        require(tail_waits == tail_forwards,
                'tail forward/scheduler closure mismatch: ' + row['request'], errors)
        shapes = counts.copy()
        shapes[0] += neural
        work.update(
            ordinary_tail_output_tokens=tail_tokens,
            ordinary_tail_forward_calls_T1=tail_forwards,
            total_target_forward_calls=verify+tail_forwards,
            total_target_forward_rows=4*verify+tail_forwards,
            target_logical_consumed_output_tokens=response['actual_output']-1,
            target_speculative_extra_rows=4*verify+tail_forwards-(response['actual_output']-1),
            draft_extend_calls=verify, draft_extend_rows=committed,
            draft_extend_calls_lower=verify, draft_extend_calls_upper=verify,
            draft_extend_rows_lower=committed, draft_extend_rows_upper=committed,
            draft_extend_shapes_T1_T2_T3_T4=counts,
            iterative_MTP_forward_calls=neural+verify,
            iterative_MTP_forward_rows=neural+committed,
            iterative_MTP_shapes_T1_T2_T3_T4=shapes)
    else:
        work['limit'] = ('Stop/cancel may skip the final accepted-prefix extend; aggregate '
                         'histograms do not identify the terminal correction. Exact extend '
                         'counts and row shapes are not asserted for these requests. '
                         'Cancellation can interrupt emission after committed model work.')
    if suffix:
        require(suffix.get('queries') == verify,
                'query/verify closure mismatch: ' + row['request'], errors)
        sa, na = suffix.get('accepted'), suffix.get('neural_accepted')
        require(sa is not None and na is not None and len(sa) == len(na) == 4 and
                [a+b for a, b in zip(sa, na)] == counts and sum(sa) == hits,
                'suffix/neural acceptance closure mismatch: ' + row['request'], errors)
        checks = suffix.get('numeric_checks')
        rejected = suffix.get('numeric_rejected')
        require(isinstance(checks, int) and isinstance(rejected, int) and
                checks == hits+rejected,
                'numeric decision closure mismatch: ' + row['request'], errors)
        classified = suffix.get('classified_tokens', -1)
        require(3 * checks <= classified <= 64 * checks,
                'classified-token bounds mismatch: ' + row['request'], errors)
        outcomes = ['hits', 'no_match', 'incomplete', 'd0_mismatch', 'invalid',
                    'exhausted', 'numeric_rejected']
        require(sum(suffix.get(k, 0) for k in outcomes) == verify,
                'query outcome closure mismatch: ' + row['request'], errors)
        require(suffix.get('numeric_ns', -1) >= 0 and
                suffix['numeric_ns'] <= suffix.get('query_ns', -1),
                'numeric/query nested timing mismatch: ' + row['request'], errors)
        # matched_tokens accumulates every match before incomplete/d0/numeric
        # gates; its denominator must include all four final match outcomes.
        matched_queries = sum(suffix.get(k, 0) for k in
                              ['hits', 'incomplete', 'd0_mismatch', 'numeric_rejected'])
        row['index_cost'] = dict(
            accounted_total_ns=sum(suffix.get(k, 0) for k in
                                   ['init_ns', 'append_ns', 'query_ns', 'release_ns']),
            numeric_ns_is_subset_of_query_ns=True,
            matched_query_count=matched_queries,
            mean_match_length_all_matched_queries=(suffix.get('matched', 0)/matched_queries
                                                   if matched_queries else None),
            peak_payload_bytes=suffix.get('peak_bytes'), object_bytes=suffix.get('object_bytes'),
            excludes_shared_table=True,
            limit='Timed index sections and requested payload, not all host/allocator overhead or RSS.')
    if row['timing']:
        timing_hist = [sum(t['accepted'] == a for t in row['timing']) for a in range(4)]
        require(timing_hist == counts,
                'timing/acceptance histogram mismatch: ' + row['request'], errors)
        require(sum(t['suffix'] for t in row['timing']) == hits,
                'timing/suffix source mismatch: ' + row['request'], errors)
    row['work'] = work


def timing_summary(rows):
    summary = {}
    for source in ['all', 'neural', 'suffix']:
        chosen = [r for r in rows if source == 'all' or
                  r['suffix'] == (source == 'suffix')]
        entry = dict(steps=len(chosen), accepted_histogram=[
            sum(r['accepted'] == a for r in chosen) for a in range(4)])
        for clock in ['wall_ms', 'gpu_event_ms']:
            entry[clock] = {phase: stats(r[clock][phase] for r in chosen if r[clock])
                            for phase in ['draft', 'verify', 'extend_accept']}
        entry['gpu_event_missing_steps'] = sum(r['gpu_event_ms'] is None for r in chosen)
        summary[source] = entry
    return summary


def audit_step(stage, step):
    output = Path(stage['output']) / step['name']
    errors = []
    requests, table, startup = parse_log(output, errors)
    responses = response_records(output, step, errors)
    admitted = {k: r for k, r in requests.items() if 'admitted' in r}
    require(len(admitted) == step['http_requests'],
            'admitted count differs from frozen budget', errors)
    for request, row in admitted.items():
        row['response'] = responses.get(request)
        require(row['response'] is not None,
                'missing retained response for ' + request, errors)
        if row['response'] is None:
            row['response'] = {}
        require('end' in row, 'missing server end for ' + request, errors)
        require(row.get('end', {}).get('released') == 1,
                'request slot release missing: ' + request, errors)
        require(('suffix' in row) == bool(step.get('suffix')),
                'suffix summary does not match arm: ' + request, errors)
        if 'suffix' in row:
            require(row['admitted']['line'] < row['suffix']['line'] <
                    row.get('end', {}).get('line', 0),
                    'request index lifecycle ordering: ' + request, errors)
        derive_work(row, step, errors)
    lifecycle = None
    if step['kind'] == 'lifecycle':
        by_case = {r['response'].get('case'): r for r in admitted.values()}
        control, cancel, recovery = (by_case.get(k, {}) for k in
                                     ['control', 'cancel', 'recovery'])
        same_response = all(control.get('response', {}).get(k) ==
                            recovery.get('response', {}).get(k) for k in
                            ['output_sha256', 'actual_input', 'actual_output', 'finish'])
        same_acceptance = control.get('acceptance') == recovery.get('acceptance')
        index_keys = ['queries', 'hits', 'candidates', 'compared', 'matched',
                      'no_match', 'incomplete', 'd0_mismatch', 'invalid',
                      'exhausted', 'indexed', 'numeric_checks', 'classified_tokens',
                      'numeric_rejected', 'accepted', 'neural_accepted']
        same_index = all(control.get('suffix', {}).get(k) ==
                         recovery.get('suffix', {}).get(k) for k in index_keys)
        require(same_response and same_acceptance and same_index,
                'fresh recovery differs from lifecycle control', errors)
        cancel_response = cancel.get('response', {})
        require(cancel.get('end', {}).get('outcome') == 'cancelled' and
                cancel.get('end', {}).get('reason') == 'explicit_cancel' and
                cancel_response.get('observed_nonempty_chunks', 0) > 0 and
                cancel_response.get('wire_errors') == [{'message': 'request cancelled'}] and
                cancel.get('suffix', {}).get('hits', 0) > 0,
                'cancellation not observed after suffix generation', errors)
        raw = read_json(Path(cancel_response['evidence']))
        messages = [json.loads(line[6:]) for line in raw.get('body', '').splitlines()
                    if line.startswith('data: {')]
        require(bool(messages) and messages[-1].get('error') ==
                {'message': 'request cancelled'},
                'cancellation SSE does not end in expected error', errors)
        ack = read_json(output/'cancel-control-response.json')
        require(ack.get('status') == 202 and
                json.loads(ack.get('body', '{}')).get('cancel_requested') is True,
                'explicit cancellation acknowledgement missing', errors)
        health = {label: read_json(output/f'after-{label}-health.json')
                  for label in ['cancel', 'recovery']}
        require(all(v.get('status') == 'ok' and v.get('gpu_healthy') is True and
                    v.get('seq_slots_free') == v.get('seq_slots_total') == 1
                    for v in health.values()),
                'slot or GPU health did not recover', errors)
        lifecycle = dict(control_recovery_response_equal=same_response,
                         control_recovery_acceptance_equal=same_acceptance,
                         control_recovery_index_counts_equal=same_index,
                         explicit_cancel_acknowledgement=ack,
                         cancelled_after_suffix_hits=cancel.get('suffix', {}).get('hits'),
                         cancelled_after_nonempty_chunks=cancel_response.get('observed_nonempty_chunks'),
                         health=health,
                         limit='These concrete HTTP cases do not prove arbitrary-input state equivalence.')
    init = [r for r in table if r.get('event') == 'init']
    release = [r for r in table if r.get('event') == 'release']
    if step.get('suffix'):
        require(len(init) == len(release) == 1 and len(table) == 2,
                'shared classification table lifecycle count', errors)
        if len(init) == len(release) == 1:
            require(init[0].get('bytes') == release[0].get('bytes') == 248320 and
                    init[0].get('cap_bytes') == 262144 and init[0].get('unknown') == 243,
                    'shared classification table identity/size', errors)
            require(all(init[0]['line'] < r['admitted']['line'] and
                        r.get('end', {}).get('line', 10**18) < release[0]['line']
                        for r in admitted.values()),
                    'classification table lifetime does not surround requests', errors)
    else:
        require(not table, 'suffix-off arm allocated classification table', errors)
    timing_rows = [t for r in admitted.values() for t in r['timing']]
    if step['kind'] == 'timing':
        require(len(admitted) == 1 and bool(timing_rows),
                'old-8K timing request missing step samples', errors)
        require(all(r['response'].get('actual_input') == 8159 and
                    r['response'].get('actual_output') == 256 for r in admitted.values()),
                'timing case is not the frozen old-8K case', errors)
    else:
        require(not timing_rows, 'regular evidence contains intrusive MTP phase timings', errors)
    return dict(name=step['name'], kind=step['kind'], head=step.get('head'),
                suffix=step.get('suffix'), block=step.get('block'),
                evidence_directory=str(output), requests=list(admitted.values()),
                shared_classification_table=table, table_startup_phase=startup,
                timing=timing_summary(timing_rows) if timing_rows else None,
                lifecycle=lifecycle,
                errors=errors)


def grouped_work(groups):
    result = []
    keys = ['verify_calls_T4', 'ordinary_tail_forward_calls_T1',
            'total_target_forward_calls', 'total_target_forward_rows',
            'checkpoint_restore_calls', 'neural_proposal_forward_calls_T1',
            'draft_extend_calls', 'draft_extend_rows', 'iterative_MTP_forward_calls',
            'iterative_MTP_forward_rows']
    for group in groups:
        by_case = collections.defaultdict(list)
        for row in group['requests']:
            by_case[row['response'].get('case')].append(row)
        for case, rows in by_case.items():
            result.append(dict(group=group['name'], kind=group['kind'],
                block=group['block'], head=group['head'], suffix=group['suffix'],
                case=case, requests=len(rows), role=rows[0]['response'].get('role'),
                work={key: stats(row['work'].get(key) for row in rows) for key in keys},
                output_sha256=sorted({r['response'].get('output_sha256', '') for r in rows})))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, default=BASE/'acceptance-v1')
    parser.add_argument('--output', type=Path, default=BASE/'audit-costs.json')
    parser.add_argument('--partial', action='store_true',
                        help='After first HTTP gate, inspect completed steps only.')
    args = parser.parse_args()
    plan = read_json(args.stage/'frozen-plan.json')
    state = read_json(Path(plan['output'])/'state.json')
    if 'quality-full-on' not in state.get('completed', []):
        raise RuntimeError('First real HTTP quality gate has not completed; no cost analysis.')
    if state['status'] == 'RUNNING' and not args.partial:
        raise RuntimeError('Stage is still running; wait or explicitly request completed-step partial audit.')
    completed = set(state.get('completed', []))
    groups = [audit_step(plan, step) for step in plan['steps']
              if step['name'] in completed and step['http_loads']]
    missing = [s['name'] for s in plan['steps'] if s['http_loads'] and s['name'] not in completed]
    timings = {g['name']: dict(head=g['head'], suffix=g['suffix'], timing=g['timing'],
                              request_work=[r['work'] for r in g['requests']])
               for g in groups if g['kind'] == 'timing'}
    errors = [f"{g['name']}: {error}" for g in groups for error in g['errors']]
    report = dict(
        created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        tool_sha256=sha(Path(__file__)), stage_state=state,
        candidate_sha256=plan['candidate_sha256'], source_commit=plan['source_commit'],
        completed_http_groups=len(groups), missing_http_groups=missing,
        audit_scope='Independent offline raw-server/response work-count and timing audit; no performance acceptance decision.',
        limits=[
            'Counts cover whole iterative generation, including first MTP round; latency-minus-TTFT excludes first-token work.',
            'MTP extend+accept includes checkpoint restore and accept/seed handling; no independent restore timer exists.',
            'CUDA-event intervals include busy and stream-idle time between markers; they are not pure GPU busy time.',
            'Phase values are printed to 0.1 ms; summed rounded values are approximate.',
            'No GPU phase span covers ordinary tail forwards; scheduler waits identify calls, not isolated kernel cost.',
            'Plain-only scheduler waits can include batched prefill; they are retained as observations, not asserted as decode call counts.',
            'Shared table init is startup cost and release is per-service teardown; neither is repeated per request.',
            'numeric_ns is already in query_ns. Do not add them when computing index cost.',
            'Stop/cancel aggregate logs do not fully determine terminal extend work; bounds are retained.',
            'Requested payload/object bytes are not total process RSS, unified RAM, or measured physical DRAM traffic.',
            'Four timing requests are separate diagnostics, never acceptance-throughput samples or independent repeated starts.'
        ], groups=groups, per_case_work=grouped_work(groups),
        old_8k_diagnostics_by_head_arm=timings,
        errors=errors, internal_consistency='PASS' if not errors else 'FAIL',
        inputs_sha256=INPUTS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(output=str(args.output), completed_http_groups=len(groups),
                         missing_http_groups=missing, errors=errors,
                         timing_groups=list(timings)), ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
