#!/usr/bin/env python3
"""Offline HTTP and raw CPU-span analysis of the frozen execution stage.

Requires the stage, sampler, and outer lease to have terminated successfully.
Does not import runners or call HTTP, CUDA, NVML, Nsight, or live proc/sysfs.
Raw clocks and all CPU duration arithmetic remain integer nanoseconds. Output
files must be new and outside the frozen stage. Nsight activity is analyzed by
an independent companion; these CPU records never estimate GPU busy time.
"""
import argparse
import base64
from collections import defaultdict
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import pickle
import re
import sqlite3
import statistics

from spec import validate

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Reader:
    """Validate every consumed raw file against the completed driver's seal."""
    def __init__(self, plan):
        self.root = Path(plan['output']).resolve()
        self.frozen = plan['files']
        self.inputs = {}
        self.seal = None
        self.seal = self.json(self.root / 'files.json')

    def verify(self, path):
        path = Path(path).resolve()
        digest = sha(path)
        if self.seal is not None and path.is_relative_to(self.root):
            relative = str(path.relative_to(self.root))
            if relative != 'files.json':
                require(self.seal.get(relative) == digest,
                        'consumed raw file differs from seal: ' + str(path))
        if str(path) in self.frozen:
            require(self.frozen[str(path)] == digest,
                    'consumed frozen input changed: ' + str(path))
        self.inputs[str(path)] = digest
        return path

    def text(self, path):
        return self.verify(path).read_text()

    def json(self, path):
        return json.loads(self.text(path))

    def jsonl(self, path):
        return [json.loads(line) for line in self.text(path).splitlines()
                if line.strip()]


def fields(line):
    values = dict(re.findall(r'(\w+)=(\S+)', line))
    for key, value in values.items():
        if re.fullmatch(r'-?\d+', value):
            values[key] = int(value)
        elif re.fullmatch(r'-?\d+\.\d+', value):
            values[key] = float(value)
    return values


def union_ns(intervals):
    ordered = sorted(intervals)
    total, left, right = 0, None, None
    for start, end in ordered:
        require(0 <= start <= end, 'invalid clock interval')
        if left is None:
            left, right = start, end
        elif start <= right:
            right = max(right, end)
        else:
            total += right - left
            left, right = start, end
    return total + (right - left if left is not None else 0)


def enrich_batch(batch):
    rows, flush = batch['spans'], batch['flush']
    require(batch['complete'] == 1 and batch['overflow'] == 0 and
            batch['clock_error'] == 0 and batch['capacity'] == 4096 and
            batch['records'] == len(rows) and 0 < len(rows) <= 4096,
            'incomplete execution batch')
    require(flush['clock_error'] == 0 and flush['wall_ns'] >= 0 and
            flush['cpu_ns'] >= 0 and flush['line'] > rows[-1]['line'],
            'incomplete execution flush')
    children = defaultdict(list)
    for index, row in enumerate(rows):
        require(row['id'] == index and -1 <= row['parent'] < index and
                row['closed'] == 1 and row['pid'] == batch['pid'] and
                row['tid'] == batch['tid'] and row['batch'] == batch['batch'],
                'invalid execution span tree or identity')
        children[row['parent']].append(index)
        for clock in ('wall', 'cpu'):
            start, end = row[clock + '_begin_ns'], row[clock + '_end_ns']
            require(isinstance(start, int) and isinstance(end, int) and
                    0 <= start <= end, 'CPU raw timestamps must be integer ns')
            row[clock + '_ns'] = end - start
            if row['parent'] >= 0:
                parent = rows[row['parent']]
                require(parent[clock + '_begin_ns'] <= start <= end <=
                        parent[clock + '_end_ns'], 'child span escapes parent')
        row['nonrunning_wall_ns'] = row['wall_ns'] - row['cpu_ns']
    for parent, indices in children.items():
        for clock in ('wall', 'cpu'):
            intervals = [(rows[i][clock + '_begin_ns'], rows[i][clock + '_end_ns'])
                         for i in indices]
            total = sum(end - start for start, end in intervals)
            require(union_ns(intervals) == total,
                    'sibling CPU spans overlap on one observed thread')
            if parent >= 0:
                rows[parent][clock + '_children_ns'] = total
    for row in rows:
        for clock in ('wall', 'cpu'):
            row[clock + '_exclusive_ns'] = (row[clock + '_ns'] -
                                           row.get(clock + '_children_ns', 0))
            require(row[clock + '_exclusive_ns'] >= 0,
                    'negative exclusive CPU interval')
        row['nonrunning_exclusive_ns'] = (row['wall_exclusive_ns'] -
                                          row['cpu_exclusive_ns'])
    batch['root_span_ids'] = children[-1]
    for clock in ('wall', 'cpu'):
        root_total = sum(rows[i][clock + '_ns'] for i in children[-1])
        require(root_total == sum(row[clock + '_exclusive_ns'] for row in rows),
                'exclusive partition does not close to batch roots')
    flush['nonrunning_wall_ns'] = flush['wall_ns'] - flush['cpu_ns']


def parse_log(text, step, server_pid):
    requests, tables, batches, captures = {}, [], {}, []
    active = None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith('[q4t][request] '):
            row = dict(fields(line), line=number)
            key = row.get('id')
            if key in (None, 'unregistered'):
                continue
            request = requests.setdefault(key, dict(events=[], batches=[]))
            request['events'].append(row)
            if row.get('event') == 'admitted':
                require(active is None, 'overlapping B1 admissions')
                active = key
            elif row.get('event') == 'end':
                require(active == key, 'unbound/out-of-order request end')
                request['end'] = row
                active = None
        elif line.startswith('[suffix-classes] '):
            tables.append(dict(fields(line), line=number))
        elif line.startswith('[mtp-accept] '):
            row = fields(line)
            key = row['request']
            require(key in requests and 'histogram' not in requests[key],
                    'duplicate/unbound MTP acceptance')
            requests[key]['histogram'] = [int(x) for x in
                                         str(row['draft_counts']).split(',')]
        elif line.startswith('[suffix] '):
            row = dict(fields(line), line=number)
            require('disabled' not in row and row['request'] in requests,
                    'suffix disabled or summary unbound')
            for name in ('accepted', 'neural_accepted'):
                row[name] = [int(x) for x in str(row[name]).split(',')]
            require('suffix' not in requests[row['request']],
                    'duplicate suffix summary')
            requests[row['request']]['suffix'] = row
        elif line.startswith(('[mtp-timing]', '[layer-timing]')):
            raise ValueError('legacy intrusive timing appeared in new stage')
        elif line.startswith('[exec-'):
            require(step['execution_diagnostic'],
                    'normal service emitted execution instrumentation')
            marker = line.split(']', 1)[0] + ']'
            row = dict(fields(line), line=number)
            if marker == '[exec-capture]':
                captures.append(row)
                continue
            key = row['batch']
            if marker == '[exec-batch]':
                require(key not in batches and active is not None and
                        row['pid'] == server_pid,
                        'duplicate/unbound/wrong-process execution batch')
                if str(row['label']).startswith('chatcmpl-'):
                    require(row['label'] == active,
                            'handler execution batch belongs to another request')
                row.update(spans=[], request_id=active)
                batches[key] = row
                requests[active]['batches'].append(row)
            elif marker == '[exec-span]':
                require(key in batches, 'span before its batch header')
                batches[key]['spans'].append(row)
            elif marker == '[exec-flush]':
                require(key in batches and 'flush' not in batches[key],
                        'duplicate/unbound flush')
                batches[key]['flush'] = row
            else:
                raise ValueError('unknown execution marker: ' + marker)
    require(active is None, 'request remains admitted after log end')
    require(bool(batches) == step['execution_diagnostic'],
            'execution activation/coverage mismatch')
    for batch in batches.values():
        require('flush' in batch, 'missing execution flush')
        enrich_batch(batch)
    if step['measurement_role'] == 'nsys_profile':
        require([row['event'] for row in captures] == ['begin', 'end'] and
                all(row['pid'] == server_pid and row['error'] == 0 and
                    row['input_tokens'] == step['profile_input_tokens']
                    for row in captures), 'Nsight capture markers differ')
    else:
        require(not captures, 'capture enabled outside the frozen branch')
    return requests, tables, captures


class BasicUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError('non-container object in retained SSE pickle')


def wire_records(reader, directory, responses):
    databases = list(directory.rglob('benchmark_data.db'))
    require(len(databases) == 1, 'expected one HTTP database per case')
    path = reader.verify(databases[0])
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        records = db.execute('SELECT response_messages,start_time,completed_time,'
            'first_chunk_latency FROM result ORDER BY start_time').fetchall()
    require(len(records) == len(responses), 'HTTP database repeat count differs')
    result = []
    for response, (blob, start, end, ttft) in zip(responses, records):
        messages = BasicUnpickler(io.BytesIO(base64.b64decode(blob))).load()
        require(isinstance(messages, list), 'retained SSE is not a list')
        frames, first, ids = 0, None, set()
        for message in messages:
            require(isinstance(message, dict), 'retained SSE entry is not object')
            if message.get('id'):
                ids.add(message['id'])
            for choice in message.get('choices', []):
                delta = choice.get('delta', {})
                if delta.get('role') or choice.get('finish_reason'):
                    continue
                if 'content' in delta:
                    frames += 1
                    if delta['content'] and first is None:
                        first = frames
        require(ids == {response['response_id']} and frames == 256 and
                first is not None, 'SSE identity/frame contract failed')
        times = response['timing']
        require(abs(start - times['start_monotonic_seconds']) < 1e-8 and
                abs(end - times['end_monotonic_seconds']) < 1e-8 and
                abs(ttft - response['ttft']) < 1e-8,
                'HTTP SQLite/JSON clock contract differs')
        result.append(dict(token_frames=frames,
            first_content_token_ordinal=first,
            source='Retained SSE ordering; one content frame per emitted token.'))
    return result


def cpu_summary(batches):
    by_phase = {}
    by_thread = defaultdict(lambda: dict(root_wall_sum_ns=0, root_cpu_sum_ns=0,
                                         root_intervals=[]))
    roots = []
    for batch in batches:
        for row in batch['spans']:
            group = by_phase.setdefault(row['phase'], dict(
                count=0, inclusive_wall_ns=0, inclusive_cpu_ns=0,
                inclusive_nonrunning_wall_ns=0, exclusive_wall_ns=0,
                exclusive_cpu_ns=0, exclusive_nonrunning_wall_ns=0))
            group['count'] += 1
            for clock in ('wall', 'cpu'):
                group['inclusive_' + clock + '_ns'] += row[clock + '_ns']
                group['exclusive_' + clock + '_ns'] += row[clock + '_exclusive_ns']
            group['inclusive_nonrunning_wall_ns'] += row['nonrunning_wall_ns']
            group['exclusive_nonrunning_wall_ns'] += row['nonrunning_exclusive_ns']
        for index in batch['root_span_ids']:
            row = batch['spans'][index]
            roots.append(row)
            group = by_thread[str(batch['tid'])]
            group['root_wall_sum_ns'] += row['wall_ns']
            group['root_cpu_sum_ns'] += row['cpu_ns']
            group['root_intervals'].append((row['wall_begin_ns'], row['wall_end_ns']))
    for group in by_thread.values():
        group['root_wall_union_ns'] = union_ns(group.pop('root_intervals'))
        require(group['root_wall_sum_ns'] == group['root_wall_union_ns'],
                'distinct batch roots overlap on one observed thread')
        group['root_nonrunning_wall_ns'] = (group['root_wall_sum_ns'] -
                                            group['root_cpu_sum_ns'])
    return dict(batches=len(batches), spans=sum(len(b['spans']) for b in batches),
        root_wall_sum_across_threads_ns=sum(r['wall_ns'] for r in roots),
        covered_root_wall_union_ns=union_ns(
            [(r['wall_begin_ns'], r['wall_end_ns']) for r in roots]),
        root_cpu_sum_across_observed_threads_ns=sum(r['cpu_ns'] for r in roots),
        per_thread=dict(by_thread), phases=by_phase,
        flush_wall_ns=sum(b['flush']['wall_ns'] for b in batches),
        flush_cpu_ns=sum(b['flush']['cpu_ns'] for b in batches),
        scope='Inclusive phase rows overlap parents and must not be added. '
              'Exclusive rows partition observed roots only. Cross-thread wall '
              'sums are not request latency; clock nonrunning is signed and '
              'contains blocking/descheduling/clock endpoint skew, not GPU time. '
              'Flush is measured outside roots and listed separately.')


def execution_record(log, work, wire, actual_input):
    batches = log['batches']
    if not batches:
        return None
    all_rows = [row for batch in batches for row in batch['spans']]
    calls = sorted((row['p'], row['t']) for row in all_rows
                   if row['phase'] == 'target_prefill_call')
    expected = [(base, min(8192, actual_input - base))
                for base in range(0, actual_input, 8192)]
    require(calls == expected, 'target prefill chunk coverage mismatch')
    required = {'handler_prefill', 'handler_mtp_init', 'prompt_trunk_free',
                'target_begin_sequence', 'prompt_draft_extend_call',
                'prefill_final_d2h_submit', 'prefill_final_readback_wait_commit'}
    require(required <= {row['phase'] for row in all_rows},
            'required CPU boundaries missing')
    rounds = []
    for batch in batches:
        steps = [row for row in batch['spans'] if row['phase'] == 'mtp.step']
        if not steps:
            continue
        require(len(steps) == 1, 'multiple MTP rounds in one execution batch')
        tokens = [row for row in batch['spans'] if row['phase'] == 'mtp.draft_token']
        extend = [row for row in batch['spans'] if row['phase'] == 'mtp.accepted_extend']
        require(len(extend) == 1 and 1 <= extend[0]['t'] <= 4 and
                len(tokens) in (0, 2), 'MTP round token/extend shape differs')
        verify = [row for row in batch['spans'] if row['phase'] == 'mtp.verify_forward']
        require(len(verify) == 1, 'MTP target verify coverage differs')
        rounds.append(dict(batch=batch['batch'], position=steps[0]['p'],
            accepted=extend[0]['t'] - 1, committed=extend[0]['t'],
            suffix=not tokens, wall_begin_ns=steps[0]['wall_begin_ns'],
            wall_end_ns=steps[0]['wall_end_ns'], cpu_begin_ns=steps[0]['cpu_begin_ns'],
            cpu_end_ns=steps[0]['cpu_end_ns']))
    rounds.sort(key=lambda r: r['wall_begin_ns'])
    require(len(rounds) == work['verify_T4'] and
            [sum(row['accepted'] == a for row in rounds) for a in range(4)] ==
            work['histogram'] and
            sum(not row['suffix'] for row in rounds) * 2 == work['neural_proposal_T1'],
            'CPU round/acceptance/neural work closure failed')
    position, emitted, first = actual_input, 0, None
    for i, row in enumerate(rounds):
        require(row['position'] == position, 'MTP position/accepted prefix mismatch')
        position += row['committed']
        emitted += row['committed']
        if first is None and emitted >= wire['first_content_token_ordinal']:
            first = i
    require(first is not None, 'first content outside captured MTP rounds')
    batch_by_id = {b['batch']: b for b in batches}
    round_batches = [batch_by_id[r['batch']] for r in rounds]
    non_round = [b for b in batches if b['batch'] not in {r['batch'] for r in rounds}]
    return dict(raw_batches=batches, whole_request_observed=cpu_summary(batches),
        prefill_and_init_batches=cpu_summary(non_round),
        whole_generation_rounds=cpu_summary(round_batches),
        first_round=cpu_summary(round_batches[:1]),
        through_server_first_content_round=cpu_summary(round_batches[:first + 1]),
        after_server_first_content_round=cpu_summary(round_batches[first + 1:]),
        first_content_round_zero_based=first, rounds=rounds,
        boundary_scope='SSE ordinal maps to the accepting/emitting server round. '
            'The after-round group is a server-emission proxy, not an exact '
            'client TTFT timestamp slice. Ordinary tail/transport/scheduler gaps '
            'and unobserved host work are not manufactured into CPU/GPU buckets.')


def request_record(step, case, response, boundary, wire, log, ordinal,
                   service_ordinal, server_pid):
    require(response['success'] == 1 and response['finish'] == ['length'] and
            response['actual_input'] == case['input_tokens'] and
            response['actual_output'] == 256, 'HTTP output/token contract failed')
    output_hash = hashlib.sha256(response['text'].encode()).hexdigest()
    require(output_hash == case['expected_output_sha256'] and
            response['prompt_sha256'] == case['prompt_sha256'],
            'frozen prompt/output hash contract failed')
    require(boundary['http_timing'] == response['timing'] and
            response['timing']['within_client_boundaries'], 'HTTP clocks unbound')
    end = log['end']
    require(end['outcome'] == 'success' and end['released'] == 1,
            'request lifecycle incomplete')
    stages = {e['stage']: e for e in log['events'] if e['event'] == 'stage'}
    require('prefill' in stages and 'decode' in stages, 'handler markers missing')
    histogram = log['histogram']
    require(len(histogram) == 4, 'acceptance histogram is not fixed k3')
    rounds = sum(histogram)
    committed = sum((a + 1) * count for a, count in enumerate(histogram))
    tail = 255 - committed
    require(tail >= 0 and end['scheduler_waits'] == rounds + tail,
            'target work / scheduler closure failed')
    suffix = log.get('suffix', {})
    require(bool(suffix) == step['suffix'], 'suffix activation differs')
    if suffix:
        require(suffix['queries'] == rounds and
                suffix['numeric_ns'] <= suffix['query_ns'] and
                suffix['numeric_checks'] == suffix['hits'] + suffix['numeric_rejected'] +
                suffix['short_rejected'] and
                [a + b for a, b in zip(suffix['accepted'], suffix['neural_accepted'])] ==
                histogram, 'suffix counter closure failed')
    work = dict(verify_T4=rounds, tail_T1=tail, target_calls=rounds + tail,
        target_rows=4 * rounds + tail, restore=sum(histogram[:3]),
        neural_proposal_T1=2 * (rounds - suffix.get('hits', 0)),
        extend_calls=rounds, extend_rows=committed, histogram=histogram,
        scope='Generation only; excludes target prefill and full-prompt draft extend.')
    ttft_ms, latency_ms = response['ttft'] * 1000, response['latency'] * 1000
    entered, done = stages['prefill']['elapsed_ms'], stages['decode']['elapsed_ms']
    costs = {key: suffix.get(key, 0) for key in
             ('init_ns', 'query_ns', 'append_ns', 'release_ns', 'numeric_ns')}
    return dict(group=step['name'], head=step['head'], suffix=step['suffix'],
        measurement_role=step['measurement_role'], case=case['id'], ordinal=ordinal,
        service_ordinal=service_ordinal, first_request_of_service=service_ordinal == 1,
        server_pid=server_pid, request_id=response['response_id'],
        actual_input=response['actual_input'], actual_output=256,
        output_sha256=output_hash, prompt_sha256=response['prompt_sha256'],
        boundaries=response['timing'], client_boundary_anchors=boundary, wire=wire,
        handler_events=log['events'], request_end=end,
        metrics=dict(ttft_ms=ttft_ms, latency_ms=latency_ms,
            post_first_output_ms=latency_ms - ttft_ms,
            decode_tps=255000 / (latency_ms - ttft_ms),
            handler_prefill_entry_ms=entered, handler_main_prefill_done_ms=done,
            handler_main_prefill_interval_ms=done - entered,
            client_ttft_minus_handler_main_done_ms=ttft_ms - done,
            scheduler_completion_ms=end['scheduler_completion_ms'],
            stream_write_ms=end['stream_write_ms']),
        work=work, suffix_counters=suffix, suffix_operations_ns=costs,
        suffix_operation_total_ns=sum(costs[k] for k in
                                     ('init_ns', 'query_ns', 'append_ns', 'release_ns')),
        execution=execution_record(log, work, wire, response['actual_input']))


def case_summary(rows):
    return dict(requests=len(rows), request_ids=[r['request_id'] for r in rows],
        medians={k: statistics.median(r['metrics'][k] for r in rows)
                 for k in rows[0]['metrics']},
        values={k: [r['metrics'][k] for r in rows] for k in rows[0]['metrics']},
        output_sha256=sorted({r['output_sha256'] for r in rows}),
        acceptance_histograms=[r['work']['histogram'] for r in rows])


def pair(a, b):
    require(a['case'] == b['case'] and a['head'] == b['head'],
            'comparison changes case or head')
    result = dict(a=a['group'], b=b['group'],
        decode_ratio=b['metrics']['decode_tps'] / a['metrics']['decode_tps'],
        metric_deltas={k: b['metrics'][k] - a['metrics'][k] for k in a['metrics']},
        output_equal=a['output_sha256'] == b['output_sha256'],
        work_a=a['work'], work_b=b['work'],
        target_work_equal=all(a['work'][k] == b['work'][k] for k in
                             ('verify_T4', 'tail_T1', 'target_rows')))
    if a['execution'] is not None and b['execution'] is not None:
        result['cpu_phase_deltas_ns'] = {}
        for window in ('prefill_and_init_batches', 'whole_generation_rounds',
                       'first_round', 'after_server_first_content_round'):
            left, right = a['execution'][window], b['execution'][window]
            phases = sorted(left['phases'].keys() | right['phases'].keys())
            result['cpu_phase_deltas_ns'][window] = {
                phase: {key: right['phases'].get(phase, {}).get(key, 0) -
                             left['phases'].get(phase, {}).get(key, 0)
                        for key in ('count', 'inclusive_wall_ns', 'inclusive_cpu_ns',
                            'inclusive_nonrunning_wall_ns', 'exclusive_wall_ns',
                            'exclusive_cpu_ns', 'exclusive_nonrunning_wall_ns')}
                for phase in phases}
    return result


def analyze(plan_path, audit_path=None):
    plan = json.loads(plan_path.read_text())
    validate(plan)
    root, lease = Path(plan['output']).resolve(), Path(plan['lease']).resolve()
    # Check completion before touching any request metrics or raw logs.
    state = json.loads((root / 'state.json').read_text())
    guard = json.loads((lease / 'exit.json').read_text())
    require(state.get('status') == 'MEASURED_REQUIRES_REVIEW' and state.get('finished') and
            not state.get('monitor_failures') and not state.get('cleanup_errors') and
            state['requests_completed'] == 49 and state['loads_started'] == 13 and
            guard.get('status') == 'COMPLETED' and guard.get('child_returncode') == 0,
            'stage/lease incomplete or invalid; refusing performance analysis')
    reader = Reader(plan)
    reader.verify(Path(__file__))
    reader.verify(Path(__file__).with_name('spec.py'))
    reader.verify(plan_path)
    require(state['plan_sha256'] == reader.inputs[str(plan_path)], 'plan SHA differs')
    reader.verify(root / 'state.json')
    reader.verify(lease / 'exit.json')
    events = reader.jsonl(lease / 'events.jsonl')
    require(any(e.get('event') == 'lease_released' for e in events),
            'outer GPU lease has not been released')
    postflight = reader.json(plan_path.parent / 'postflight.json')
    summary = reader.json(root / 'telemetry-summary.json')
    require(postflight['status'] == 'MEASURED_REQUIRES_REVIEW' and
            not postflight['monitor_failures'] and not postflight['cleanup_errors'] and
            not postflight.get('final_monitor_errors') and
            postflight['state_sha256'] == reader.inputs[str(root / 'state.json')] and
            postflight['final_summary_sha256'] == reader.inputs[str(root / 'telemetry-summary.json')] and
            summary['sampler_status'] == 'STOPPED' and
            summary['sampler_failure'] is None and not summary['failures_at_exit'] and
            summary['unflushed_attempts'] == 0 and
            summary['sample_attempts'] == summary['flushed_samples'] and
            summary['flushed_samples'] > 0, 'postflight/sampler closure failed')
    barrier = postflight['final_monitor_barrier']
    require(barrier['boundary_monotonic'] <= barrier['sample_started_monotonic'] <=
            barrier['flushed_monotonic'], 'final monitor barrier not fresh')
    audit = None
    if audit_path is not None:
        audit = reader.json(audit_path)
        audit_plan = audit.get('plan_sha256', audit.get('identities', {}).get('plan_sha256'))
        require(audit.get('evidence_validity') == 'VALID' and
                audit.get('measurement_complete') is True and not audit.get('errors') and
                not audit.get('environment_errors') and not audit.get('measurement_gaps') and
                audit_plan == state['plan_sha256'], 'independent audit is not valid/complete')
    requests, groups = [], []
    for step in plan['steps']:
        folder = root / step['name']
        exit_record = reader.json(folder / 'exit.json')
        require(exit_record['server'] == 0 and exit_record['http_output_checks_passed'] and
                not exit_record['failure'] and not exit_record['cleanup_errors'] and
                exit_record['execution_records_complete'], 'service lifecycle/output check failed')
        execution_audit = reader.json(folder / 'execution-audit.json')
        require(execution_audit['complete'] and not execution_audit['errors'],
                'driver raw execution audit failed')
        if step['measurement_role'] == 'nsys_profile':
            require(exit_record['launcher'] == 0 and exit_record['trace_exports_complete'],
                    'Nsight launcher/export incomplete')
            reader.verify(folder / 'trace-evidence.json')
            reader.verify(folder / 'export-exit.json')
        server_pid = int(reader.text(folder / 'server.pid'))
        logs, tables, captures = parse_log(reader.text(folder / 'server.log'), step, server_pid)
        selected = []
        for case in step['cases']:
            directory = folder / case['id']
            responses = reader.json(directory / 'responses.json')
            boundaries = reader.json(directory / 'request-boundaries.json')
            require(len(responses) == len(boundaries) == case['repeats'],
                    'response/repeat budget mismatch')
            wires = wire_records(reader, directory, responses)
            for ordinal, (response, boundary, wire) in enumerate(
                    zip(responses, boundaries, wires), 1):
                key = response['response_id']
                require(key in logs, 'response lacks native request log')
                row = request_record(step, case, response, boundary, wire, logs[key],
                                     ordinal, len(selected) + 1, server_pid)
                selected.append(row)
                requests.append(row)
        require(len(selected) == len(logs) == step['http_requests'],
                'unmatched HTTP/native request count')
        if step['suffix']:
            require(len(tables) == 2 and tables[0]['event'] == 'init' and
                    tables[1]['event'] == 'release' and tables[0]['bytes'] == tables[1]['bytes'] and
                    tables[1]['line'] > max(r['request_end']['line'] for r in selected),
                    'service suffix class table lifecycle incomplete')
        else:
            require(not tables, 'suffix OFF allocated class table')
        groups.append(dict(name=step['name'], head=step['head'], suffix=step['suffix'],
            measurement_role=step['measurement_role'], server_pid=server_pid,
            table_lifecycle=tables, capture_markers=captures,
            execution_driver_audit=execution_audit,
            cases={case['id']: case_summary([r for r in selected if r['case'] == case['id']])
                   for case in step['cases']}))
    require(len(requests) == 49 and len(groups) == 13, 'stage budget coverage differs')
    hashes = defaultdict(set)
    for row in requests:
        hashes[row['head'] + '/' + row['case']].add(row['output_sha256'])
    require(all(len(values) == 1 for values in hashes.values()), 'same-head output changed')
    lookup = {(r['group'], r['case'], r['ordinal']): r for r in requests}
    gs = {g['name']: g for g in groups}
    repeat = {}
    for case in ('repeat-code', 'repeat-text'):
        full = gs['full-on']['cases'][case]['medians']['decode_tps'] / gs['full-off']['cases'][case]['medians']['decode_tps']
        ratios = [gs['subset-on-b']['cases'][case]['medians']['decode_tps'] /
                  gs[g]['cases'][case]['medians']['decode_tps']
                  for g in ('subset-off-a1', 'subset-off-a2')]
        gm = math.sqrt(math.prod(ratios))
        repeat[case] = dict(full_ratio=full, subset_B_A1=ratios[0], subset_B_A2=ratios[1],
            subset_shared_B_geomean=gm,
            frozen_descriptive_target_met=full >= 1.05 and min(ratios) > 1 and gm >= 1.05,
            raw_and_medians={name: gs[name]['cases'][case] for name in
                ('full-off', 'full-on', 'subset-off-a1', 'subset-on-b', 'subset-off-a2')})
    comparisons = {}
    for case in ('context-8192', 'context-204800'):
        comparisons[case] = {}
        for prefix, names in [('normal', ('subset-off-a1', 'subset-on-b', 'subset-off-a2')),
                              ('execution', ('exec-off-a1', 'exec-on-b', 'exec-off-a2'))]:
            a1, b, a2 = names
            comparisons[case][prefix] = {
                name: pair(lookup[(left, case, 1)], lookup[(right, case, 1)])
                for name, left, right in [('B-A1', a1, b), ('B-A2', a2, b), ('A2-A1', a1, a2)]}
        off, on = (('nsys8-off', 'nsys8-on') if case == 'context-8192' else
                   ('nsys200-off', 'nsys200-on'))
        comparisons[case]['profile_diagnostic'] = pair(lookup[(off, case, 1)], lookup[(on, case, 1)])
    return dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        evidence_validity=('VALID_INDEPENDENT_AUDIT_BOUND' if audit is not None else
                           'CONSUMED_RAW_CHECKS_PASSED_FULL_IDENTITY_ENVIRONMENT_AUDIT_REQUIRED'),
        measurement_complete=True,
        measurement_scope='49 HTTP contracts and raw CPU span coverage only; '
                          'full environment/model identity and GPU activity '
                          'completeness require their independent audits.',
        gpu_activity_measurement_complete='NOT_ASSESSED_HERE',
        http_contract_requests=49, services=13,
        statistical_conclusion='DESCRIPTIVE_ONLY', performance_acceptance=False,
        engineering_decision='ROOT_REVIEW_REQUIRED_NO_AUTOMATIC_OPTIMIZATION_OR_DEPLOYMENT',
        plan_sha256=state['plan_sha256'], candidate_sha256=plan['candidate_sha256'],
        stage_state=state, guard=guard, postflight=postflight,
        telemetry_summary=summary, independent_audit=audit,
        requests=requests, groups=groups,
        same_head_output_hashes={key: sorted(values) for key, values in hashes.items()},
        normal_repeat_confirmation=repeat, long_comparisons=comparisons,
        long_probe_zero_hit={r['group'] + '/' + r['case']: r['suffix_counters'].get('hits', 0) == 0
                             for r in requests if r['case'].startswith('context-')},
        inputs_sha256=reader.inputs, limits=LIMITS)


LIMITS = [
    'This parser verifies consumed raw seals, output contracts and completion. '
    'Without --audit it is not a complete independent environment/model/input audit.',
    'Normal, CPU-observed and Nsight services remain separate; observation changes execution timing.',
    'Repeat material is already seen development material. Three requests share a startup; '
    'subset B/A1 and B/A2 share B and are not independent blocks or a population noninferiority result.',
    'CPU clocks retain integer nanoseconds. Wall and thread CPU clocks are sampled sequentially; '
    'small signed wall-minus-CPU values are preserved rather than clamped or called GPU wait.',
    'Inclusive phase totals are nested. Exclusive phases partition observed roots only. '
    'Thread CPU sums cover observed threads and windows, not all process CPU.',
    'CUDA API wall can include driver CPU, blocking and descheduling; exact GPU activity '
    'requires the separately audited Nsight timeline. Host wall minus CPU is not GPU busy time.',
    'First-content SSE ordinal maps to a server emitting round. Removing it is a proxy '
    'and does not slice at the actual client TTFT timestamp.',
    'Batch flush costs are separate from CPU scopes. Observer clocks/NVTX bookkeeping '
    'and unobserved scheduling gaps are not subtracted to fabricate uninstrumented costs.',
    'Suffix numeric time is nested in query time. Init/query/append/release are host wall '
    'intervals with different windows from post-TTFT; no memory/RAM inference is made here.',
    'The 49/13 observation matrix is not five-tier/plain-decode optimization acceptance; '
    'it cannot erase the prior NO_GO or deploy the observation binary.',
]


def markdown(report):
    lines = ['# 固定 k3 suffix 执行观测结果', '', report['evidence_validity'], '',
             '逐请求合同：49/49；服务：13。仅描述性定位，GPU活动另表审计。', '',
             '| 重复材料 | 完整ON/OFF | 子集B/A1 | 子集B/A2 | 共享B几何均值 | 目标 |',
             '|---|---:|---:|---:|---:|---|']
    for case, row in report['normal_repeat_confirmation'].items():
        lines.append(f"| {case} | {row['full_ratio']:.6f} | {row['subset_B_A1']:.6f} | "
                     f"{row['subset_B_A2']:.6f} | {row['subset_shared_B_geomean']:.6f} | "
                     f"{row['frozen_descriptive_target_met']} |")
    lines += ['', '| 探针 | 组 | 比较 | TTFT差ms | prefill完成差ms | decode变化 | 目标工作相同 |',
              '|---|---|---|---:|---:|---:|---|']
    for case, groups in report['long_comparisons'].items():
        for group, comparisons in groups.items():
            if group == 'profile_diagnostic':
                comparisons = {'ON-OFF': comparisons}
            for name, row in comparisons.items():
                delta = row['metric_deltas']
                lines.append(f"| {case} | {group} | {name} | {delta['ttft_ms']:+.3f} | "
                    f"{delta['handler_main_prefill_done_ms']:+.3f} | "
                    f"{(row['decode_ratio'] - 1) * 100:+.3f}% | {row['target_work_equal']} |")
    phases = ('handler_prefill', 'target_prefill_call', 'host_readback.stream_sync',
              'ple.reader', 'ple.previous_upload_wait', 'handler_mtp_init',
              'mtp.draft_token', 'mtp.attention', 'mtp.moe', 'mtp.api.moe_counts_d2h',
              'mtp.wait.moe_counts_sync', 'mtp.moe.experts_submit', 'mtp.head',
              'mtp.api.draft_matrix_d2h', 'mtp.verify_forward', 'mtp.accepted_extend')
    lines += ['', 'CPU表按独立phase展示inclusive差值；父子行不能相加。原始ns及exclusive分区均在JSON。', '',
              '| 探针 | 比较 | 窗口 | phase | wall差ms | thread CPU差ms | 非运行wall差ms |',
              '|---|---|---|---|---:|---:|---:|']
    for case, groups in report['long_comparisons'].items():
        for name in ('B-A1', 'B-A2', 'A2-A1'):
            row = groups['execution'][name]
            for window in ('prefill_and_init_batches', 'after_server_first_content_round'):
                for phase in phases:
                    delta = row['cpu_phase_deltas_ns'][window].get(phase)
                    if delta is None:
                        continue
                    lines.append(f"| {case} | {name} | {window} | {phase} | "
                        f"{delta['inclusive_wall_ns'] / 1e6:+.6f} | "
                        f"{delta['inclusive_cpu_ns'] / 1e6:+.6f} | "
                        f"{delta['inclusive_nonrunning_wall_ns'] / 1e6:+.6f} |")
    lines += ['', '所有响应hash、接受分布、目标工作、suffix计数、逐batch CPU原始记录及首轮拆分见JSON。', '']
    lines += ['- ' + text for text in report['limits']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--markdown', type=Path, required=True)
    args = parser.parse_args()
    args.plan = args.plan.resolve()
    plan = json.loads(args.plan.read_text())
    protected = [args.plan.parent, Path(plan['output']).resolve(),
                 Path(plan['lease']).resolve(), Path(plan['source_snapshot']).resolve()]
    for output in (args.output, args.markdown):
        require(not output.exists() and
                not any(output.resolve().is_relative_to(path) for path in protected),
                'output already exists or enters frozen evidence')
    require(args.output.resolve() != args.markdown.resolve(), 'outputs overlap')
    report = analyze(args.plan, args.audit.resolve() if args.audit else None)
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    with args.markdown.open('x') as stream:
        stream.write(markdown(report))
    print(json.dumps(dict(output=str(args.output), markdown=str(args.markdown),
                         requests=len(report['requests']), services=len(report['groups']))))


if __name__ == '__main__':
    main()
