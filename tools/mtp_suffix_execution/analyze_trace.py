"""Read-only Nsight/CPU observer attribution for one completed profile group.

No collection, export, CUDA, NVML or subprocess invocation is performed here.
The result audits this trace, not the stage lease/environment or acceptance.
CUDA launches belong to the deepest containing q4t NVTX range on their thread;
GPU work follows the process-qualified CUPTI correlation ID even when it runs
after that host range ends. Parent/child inclusive results must not be summed.
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics


NVTX_NAME = re.compile(
    r'^q4t\|(\d+)\|(\d+)\|([^|]+)\|seq=(-?\d+)\|p=(-?\d+)\|t=(-?\d+)$')
PROCESS_MASK = 0xFFFFFFFFFF000000
LOW_ID_MASK = 0xFFFFFF
ACTIVITY_TABLES = {
    'kernel': 'CUPTI_ACTIVITY_KIND_KERNEL',
    'copy': 'CUPTI_ACTIVITY_KIND_MEMCPY',
    'memset': 'CUPTI_ACTIVITY_KIND_MEMSET',
}
# This auxiliary feature is not requested by --trace=cuda,nvtx. Keep its exact
# diagnostic in the output instead of silently discarding a severity=Error.
UNSUPPORTED_UM = re.compile(
    r'^CUDA device \d+: Unified Memory trace is not supported by the current '
    r'driver version or configuration\.$')
LOSS = re.compile(r'\b(dropped|lost|overflow|truncat\w*|incomplete)\b', re.I)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def union_ns(intervals):
    end = None
    total = 0
    for begin, stop in sorted(intervals):
        require(stop >= begin, 'negative interval')
        if end is None or begin >= end:
            total += stop - begin
        elif stop > end:
            total += stop - end
        end = stop if end is None else max(end, stop)
    return total


def distribution(values):
    if not values:
        return dict(count=0, min=None, median=None, p95=None, max=None)
    ordered = sorted(values)
    return dict(count=len(ordered), min=ordered[0],
                median=statistics.median(ordered),
                p95=ordered[(95 * len(ordered) - 1) // 100], max=ordered[-1])


def fields(line, prefix):
    values = dict(part.split('=', 1) for part in line[len(prefix):].split())
    return {key: int(value) if re.fullmatch(r'-?\d+', value) else value
            for key, value in values.items()}


def cpu_records(text):
    """Select precisely the one profiler-start/stop window in the server log."""
    batches, spans, flushes, captures = {}, {}, {}, []
    capture_active = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.startswith('[exec-capture] '):
            event = fields(line, '[exec-capture] ')
            event['line'] = line_number
            require(event['error'] == 0, 'cudaProfiler start/stop failed')
            if event['event'] == 'begin':
                require(not captures and not capture_active,
                        'multiple or nested profiler captures')
                capture_active = True
            else:
                require(event['event'] == 'end' and capture_active,
                        'unpaired profiler stop')
                capture_active = False
            captures.append(event)
        elif capture_active and line.startswith('[exec-batch] '):
            value = fields(line, '[exec-batch] ')
            key = value['batch']
            require(key not in batches, 'duplicate CPU batch')
            require(value['complete'] == 1 and value['overflow'] == 0 and
                    value['clock_error'] == 0, 'incomplete CPU batch')
            require(0 < value['records'] <= value['capacity'], 'CPU capacity')
            batches[key] = value
        elif capture_active and line.startswith('[exec-span] '):
            value = fields(line, '[exec-span] ')
            key = (value['batch'], value['id'])
            require(key not in spans and value['closed'] == 1,
                    'duplicate or unclosed CPU span')
            require(value['wall_end_ns'] >= value['wall_begin_ns'] and
                    value['cpu_end_ns'] >= value['cpu_begin_ns'],
                    'invalid CPU clock interval')
            spans[key] = value
        elif capture_active and line.startswith('[exec-flush] '):
            value = fields(line, '[exec-flush] ')
            require(value['batch'] not in flushes and value['clock_error'] == 0,
                    'duplicate or failed CPU flush')
            flushes[value['batch']] = value
    require(len(captures) == 2 and not capture_active, 'capture not closed')
    require(all(captures[0][key] == captures[1][key]
                for key in ('pid', 'tid', 'input_tokens')), 'capture identity')
    require(batches and set(batches) == set(flushes), 'CPU batch/flush mismatch')
    for batch, value in batches.items():
        rows = [v for (b, _), v in spans.items() if b == batch]
        require({v['id'] for v in rows} == set(range(value['records'])),
                'CPU records/count mismatch')
        require(all(v['pid'] == value['pid'] == captures[0]['pid'] and
                    v['tid'] == value['tid'] and v['seq'] == value['seq']
                    for v in rows), 'CPU batch identity mismatch')
    require(all(batch in batches for batch, _ in spans), 'orphan CPU span')
    children = defaultdict(list)
    for key, value in spans.items():
        if value['parent'] < 0:
            value['depth'] = 0
            continue
        parent_key = (key[0], value['parent'])
        require(parent_key in spans and value['parent'] < value['id'],
                'invalid CPU parent')
        parent = spans[parent_key]
        for clock in ('wall', 'cpu'):
            require(parent[clock + '_begin_ns'] <= value[clock + '_begin_ns'] <=
                    value[clock + '_end_ns'] <= parent[clock + '_end_ns'],
                    'CPU child outside parent')
        value['depth'] = parent['depth'] + 1
        children[parent_key].append(value)
    for key, value in spans.items():
        for clock in ('wall', 'cpu'):
            duration = value[clock + '_end_ns'] - value[clock + '_begin_ns']
            intervals = [(v[clock + '_begin_ns'], v[clock + '_end_ns'])
                         for v in children[key]]
            require(union_ns(intervals) == sum(b - a for a, b in intervals),
                    'CPU sibling overlap')
            value[clock + '_ns'] = duration
            value[clock + '_exclusive_ns'] = duration - union_ns(intervals)
        # Do not clamp small negative deltas caused by sequential clock reads.
        value['non_running_wall_ns'] = value['wall_ns'] - value['cpu_ns']
        value['non_running_wall_exclusive_ns'] = (
            value['wall_exclusive_ns'] - value['cpu_exclusive_ns'])
    return batches, spans, flushes, captures


def columns(db, table):
    return {row['name'] for row in db.execute('PRAGMA table_info("' + table + '")')}


def all_rows(db, table):
    return [dict(row) for row in db.execute('SELECT * FROM "' + table + '"')]


def diagnostic_audit(db, tables, server_text, export_text):
    require('DIAGNOSTIC_EVENT' in tables and
            'ENUM_DIAGNOSTIC_SEVERITY_LEVEL' in tables,
            'missing Nsight diagnostics/severity table')
    levels = {r['id']: r['name'] for r in
              all_rows(db, 'ENUM_DIAGNOSTIC_SEVERITY_LEVEL')}
    errors, auxiliary, warnings, messages = [], [], [], []
    for row in all_rows(db, 'DIAGNOSTIC_EVENT'):
        row['severity_name'] = levels.get(row['severity'], 'UNKNOWN')
        messages.append(row)
        text = row['text'].strip()
        if UNSUPPORTED_UM.fullmatch(text):
            auxiliary.append(row)
        elif LOSS.search(text) or row['severity_name'] in ('Error', 'UNKNOWN'):
            errors.append(row)
        elif row['severity_name'] == 'Warning':
            warnings.append(row)
    # Only profiler diagnostics are scanned in stdout: the model's own text
    # and normal parameter names must not masquerade as profiler loss reports.
    log_findings = []
    for label, text in [('server.log', server_text), ('export.log', export_text)]:
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith(('[exec-', '[q4t]', '[suffix]', '[suffix-',
                                '[mtp-', '[startup-', '[request-', 'MTP [')):
                continue
            if LOSS.search(line) or re.search(r'\b(FATAL|ERROR):', line):
                log_findings.append(dict(file=label, line=number, text=line))
    return dict(messages=messages, errors=errors, warnings_require_review=warnings,
                unavailable_auxiliary_features=auxiliary,
                profiler_log_findings=log_findings,
                valid=not errors and not warnings and not log_findings)


def nvtx_records(db, tables, names, spans, target_pid):
    require('NVTX_EVENTS' in tables, 'missing NVTX table')
    cols = columns(db, 'NVTX_EVENTS')
    require({'start', 'end', 'globalTid', 'eventType', 'text', 'textId'} <= cols,
            'unsupported NVTX schema')
    result = {}
    for row in all_rows(db, 'NVTX_EVENTS'):
        text = names.get(row['textId'], row['text'] or '')
        if not text.startswith('q4t|'):
            continue
        match = NVTX_NAME.fullmatch(text)
        require(match is not None, 'malformed/truncated q4t NVTX label')
        batch, span_id, phase, seq, position, tokens = match.groups()
        key = (int(batch), int(span_id))
        require(row['eventType'] in (59, 70), 'q4t NVTX is not push/pop')
        require(row['end'] is not None and row['end'] >= row['start'],
                'unclosed q4t NVTX range')
        require(row.get('endGlobalTid') in (None, row['globalTid']),
                'q4t NVTX changed thread')
        require(key in spans and key not in result, 'NVTX/CPU key mismatch')
        raw = spans[key]
        require(((row['globalTid'] >> 24) & LOW_ID_MASK) == target_pid and
                (row['globalTid'] & LOW_ID_MASK) == raw['tid'],
                'NVTX PID/TID mismatch')
        require((phase, int(seq), int(position), int(tokens)) ==
                (raw['phase'], raw['seq'], raw['p'], raw['t']),
                'NVTX/CPU phase or position mismatch')
        result[key] = dict(start=row['start'], end=row['end'],
                           global_tid=row['globalTid'], depth=raw['depth'])
    require(set(result) == set(spans), 'missing CPU-to-NVTX records')
    for key, row in result.items():
        parent_id = spans[key]['parent']
        if parent_id >= 0:
            parent = result[(key[0], parent_id)]
            require(parent['global_tid'] == row['global_tid'] and
                    parent['start'] <= row['start'] <= row['end'] <= parent['end'],
                    'NVTX parent nesting mismatch')
    return result


def runtime_records(db, tables, names, nvtx, target_pid):
    records, by_correlation, errors = [], defaultdict(list), []
    for table in ('CUPTI_ACTIVITY_KIND_RUNTIME', 'CUPTI_ACTIVITY_KIND_DRIVER'):
        if table not in tables:
            continue
        cols = columns(db, table)
        require({'start', 'end', 'globalTid', 'correlationId', 'nameId'} <= cols,
                'unsupported CUDA API schema: ' + table)
        for row in all_rows(db, table):
            if row['globalTid'] is None or (
                    (row['globalTid'] >> 24) & LOW_ID_MASK) != target_pid:
                continue
            # Nsight's own nvtx_kern_sum excludes callchain duplicate records.
            if row.get('callchainId') is not None:
                continue
            require(row['end'] >= row['start'], 'negative CUDA API interval')
            row['source'] = table
            row['name'] = names[row['nameId']]
            row['index'] = len(records)
            row['owner'] = None
            records.append(row)
            correlation = row['correlationId']
            if correlation is not None:
                by_correlation[(row['globalTid'] & PROCESS_MASK, correlation)].append(row)
            code = row.get('returnValue', 0)
            pending_query = 'Query' in row['name'] and (
                code == 34 or (table.endswith('_DRIVER') and code == 600))
            if code != 0 and not pending_query:
                errors.append(dict(index=row['index'], name=row['name'],
                                   return_value=code))
    require(records, 'no target CUDA API records')
    by_thread = defaultdict(list)
    for key, row in nvtx.items():
        by_thread[row['global_tid']].append((key, row))
    for tid, ranges in by_thread.items():
        ranges.sort(key=lambda pair: pair[1]['start'])
        active, cursor = [], 0
        for api in sorted((r for r in records if r['globalTid'] == tid),
                          key=lambda row: row['start']):
            while cursor < len(ranges) and ranges[cursor][1]['start'] <= api['start']:
                active.append(ranges[cursor])
                cursor += 1
            active = [pair for pair in active if pair[1]['end'] >= api['start']]
            containing = [pair for pair in active if pair[1]['end'] >= api['end']]
            if containing:
                api['owner'] = max(containing, key=lambda pair: pair[1]['depth'])[0]
    return records, by_correlation, errors


def gpu_records(db, tables, names, by_correlation, target_pid):
    activities, errors = [], []
    absent = [kind for kind, table in ACTIVITY_TABLES.items() if table not in tables]
    for kind, table in ACTIVITY_TABLES.items():
        if table not in tables:
            continue
        require({'start', 'end', 'globalPid', 'correlationId', 'deviceId'} <=
                columns(db, table), 'unsupported GPU activity schema: ' + table)
        for row in all_rows(db, table):
            if row['globalPid'] is None or (
                    (row['globalPid'] >> 24) & LOW_ID_MASK) != target_pid:
                continue
            require(row['end'] >= row['start'], 'negative GPU activity interval')
            row['kind'], row['index'], row['owner'] = kind, len(activities), None
            if kind == 'kernel':
                row['name'] = names.get(row.get('shortName'),
                                        names.get(row.get('demangledName'), 'UNKNOWN'))
            key = (row['globalPid'], row['correlationId'])
            candidates = by_correlation.get(key, [])
            # Runtime and driver records can describe different launch APIs.
            # A duplicate correlation is never resolved by nearest timestamp.
            if len(candidates) == 1:
                api = candidates[0]
                row['api_index'] = api['index']
                row['owner'] = api['owner']
                row['api_start_to_activity_ns'] = row['start'] - api['start']
                row['api_end_to_activity_ns'] = row['start'] - api['end']
                if row['api_start_to_activity_ns'] < 0:
                    errors.append(dict(activity=row['index'],
                                       reason='GPU begins before correlated API'))
            else:
                errors.append(dict(activity=row['index'], kind=kind,
                                   correlation_id=row['correlationId'],
                                   reason='missing' if not candidates else 'ambiguous',
                                   api_candidates=len(candidates)))
            activities.append(row)
    require(any(a['kind'] == 'kernel' for a in activities), 'no target kernels')
    return activities, errors, absent


def gpu_summary(activities):
    result = dict(activity_count=len(activities), gpu_activity_union_ns=union_ns(
        [(a['start'], a['end']) for a in activities]))
    for kind in ACTIVITY_TABLES:
        rows = [a for a in activities if a['kind'] == kind]
        result[kind] = dict(count=len(rows),
                            sum_ns=sum(a['end'] - a['start'] for a in rows),
                            union_ns=union_ns([(a['start'], a['end']) for a in rows]))
        if kind == 'copy':
            result[kind]['bytes'] = sum(a.get('bytes', 0) for a in rows)
    kernels = [a for a in activities if a['kind'] == 'kernel']
    result['launch_start_to_kernel_ns'] = distribution(
        [a['api_start_to_activity_ns'] for a in kernels if 'api_index' in a])
    result['launch_return_to_kernel_ns'] = distribution(
        [a['api_end_to_activity_ns'] for a in kernels if 'api_index' in a])
    return result


def attribute(batches, spans, nvtx, apis, activities):
    direct_gpu, direct_api = defaultdict(list), defaultdict(list)
    for activity in activities:
        direct_gpu[activity['owner']].append(activity)
    for api in apis:
        direct_api[api['owner']].append(api)
    inclusive = {key: list(direct_gpu[key]) for key in spans}
    for key in sorted(spans, key=lambda key: spans[key]['depth'], reverse=True):
        parent = spans[key]['parent']
        if parent >= 0:
            inclusive[(key[0], parent)].extend(inclusive[key])
    rows, phase_groups = [], defaultdict(list)
    offset_low, offset_high = [], []
    for key, raw in spans.items():
        interval = nvtx[key]
        offset_low.append(raw['wall_end_ns'] - interval['end'])
        offset_high.append(raw['wall_begin_ns'] - interval['start'])
        row = dict(raw, request_or_batch_label=batches[key[0]]['label'],
                   nvtx_start_ns=interval['start'], nvtx_end_ns=interval['end'],
                   gpu_exclusive=gpu_summary(direct_gpu[key]),
                   gpu_inclusive=gpu_summary(inclusive[key]))
        api_groups = defaultdict(list)
        for api in direct_api[key]:
            api_groups[api['name']].append(api)
        row['cuda_api_exclusive'] = [dict(name=name, calls=len(values),
            union_ns=union_ns([(a['start'], a['end']) for a in values]),
            sum_ns=sum(a['end'] - a['start'] for a in values))
            for name, values in sorted(api_groups.items())]
        rows.append(row)
        phase_groups[raw['phase']].append(key)
    phases = []
    for phase, keys in sorted(phase_groups.items()):
        gpu = [a for key in keys for a in direct_gpu[key]]
        phases.append(dict(phase=phase, spans=len(keys),
            host_wall_exclusive_sum_ns=sum(spans[k]['wall_exclusive_ns'] for k in keys),
            thread_cpu_exclusive_sum_ns=sum(spans[k]['cpu_exclusive_ns'] for k in keys),
            non_running_wall_exclusive_sum_ns=sum(
                spans[k]['non_running_wall_exclusive_ns'] for k in keys),
            gpu_exclusive=gpu_summary(gpu)))
    return dict(spans=rows, phases_exclusive=phases,
                unowned_gpu=gpu_summary(direct_gpu[None]),
                unowned_cuda_api_count=len(direct_api[None]),
                monotonic_minus_nsight_offset_bounds_ns=dict(
                    lower=max(offset_low), upper=min(offset_high),
                    common_intersection_exists=max(offset_low) <= min(offset_high)),
                clock_alignment_used_for_attribution=False)


def analyze(group):
    paths = {name: group / name for name in (
        'server.log', 'exit.json', 'export-exit.json', 'export.log',
        'trace-evidence.json', 'trace.sqlite', 'trace.nsys-rep', 'step.json',
        'target-identity.json', 'target-exit.json', 'profile-shutdown.json')}
    require(all(path.is_file() for path in paths.values()), 'missing profile artifacts')
    hashes = {name: sha(path) for name, path in paths.items()}
    status = read_json(paths['exit.json'])
    require(status['server'] == 0 and status['launcher'] == 0 and
            status['failure'] is None and status['trace_exports_complete'] is True and
            status['http_output_checks_passed'] is True, 'unclean profile group exit')
    require(read_json(paths['export-exit.json'])['returncode'] == 0, 'export failed')
    evidence = read_json(paths['trace-evidence.json'])
    require(evidence['sqlite_sha256'] == hashes['trace.sqlite'] and
            evidence['report_sha256'] == hashes['trace.nsys-rep'], 'trace identity mismatch')
    identity, target = [read_json(paths[name]) for name in
                        ('target-identity.json', 'target-exit.json')]
    shutdown = read_json(paths['profile-shutdown.json'])
    require(target['returncode'] == 0 and target['pid'] == identity['pid'] and
            target['wrapper_pid'] == identity['wrapper_pid'] and
            shutdown['target'] == target and shutdown['launcher_returncode'] == 0 and
            shutdown['forced_target_kill'] is False, 'unclean target shutdown')
    text = paths['server.log'].read_text()
    batches, spans, flushes, captures = cpu_records(text)
    step = read_json(paths['step.json'])
    require(captures[0]['pid'] == identity['pid'] and
            captures[0]['input_tokens'] == step['profile_input_tokens'] and
            step['measurement_role'] == 'nsys_profile', 'capture target/input mismatch')
    with sqlite3.connect(paths['trace.sqlite'].resolve().as_uri() +
                         '?mode=ro&immutable=1', uri=True) as db:
        db.row_factory = sqlite3.Row
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok',
                'SQLite integrity failure')
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        require({'StringIds', 'CUPTI_ACTIVITY_KIND_RUNTIME',
                 'CUPTI_ACTIVITY_KIND_KERNEL'} <= tables, 'required trace tables absent')
        names = {row['id']: row['value'] for row in all_rows(db, 'StringIds')}
        diagnostics = diagnostic_audit(db, tables, text, paths['export.log'].read_text())
        nvtx = nvtx_records(db, tables, names, spans, identity['pid'])
        apis, correlations, api_errors = runtime_records(db, tables, names, nvtx,
                                                       identity['pid'])
        activities, correlation_errors, absent = gpu_records(
            db, tables, names, correlations, identity['pid'])
        metadata = {table: all_rows(db, table) for table in
                    ('META_DATA_EXPORT', 'ANALYSIS_DETAILS',
                     'TARGET_INFO_SESSION_START_TIME') if table in tables}
        counts = {table: db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
                  for table in evidence['required_counts']}
        require(counts == evidence['required_counts'], 'export table counts changed')
    device_ids = sorted({a['deviceId'] for a in activities})
    require(len(device_ids) == 1, 'single-device interval union required')
    attribution = attribute(batches, spans, nvtx, apis, activities)
    valid = diagnostics['valid'] and not api_errors and not correlation_errors
    return dict(schema=1, trace_integrity='PASS' if valid else 'FAIL',
        stage_environment_verified=False, performance_acceptance=False,
        group=str(group), consumed_sha256=hashes, capture=captures,
        batch_count=len(batches), span_count=len(spans), metadata=metadata,
        device_ids=device_ids, absent_activity_tables=absent,
        diagnostics=diagnostics, cuda_api_errors=api_errors,
        correlation_errors=correlation_errors,
        all_gpu=gpu_summary(activities), attribution=attribution,
        observer_flush=dict(count=len(flushes),
            wall_sum_ns=sum(v['wall_ns'] for v in flushes.values()),
            thread_cpu_sum_ns=sum(v['cpu_ns'] for v in flushes.values())),
        limits=[
            'Trace validity does not replace the complete stage lease/resource/identity audit.',
            'CUDA activity union measures traced execution intervals, not occupancy or FLOPs.',
            'The trace is instrumented; do not substitute it for normal HTTP timing.',
            'CPU wall minus thread CPU includes blocking and descheduling, not pure scheduler wait.',
            'Thread CPU and wall clocks are sampled sequentially; small negative differences are retained.',
            'API ownership uses same-thread containment then process-qualified correlationId; no nearest-time guesses.',
            'Inclusive parent and child results overlap. Phase aggregates use exclusive ownership only.',
            'Exclusive GPU work may run beyond the submitting host span; CPU and GPU times are not additive.',
            'Launch-return-to-kernel latency can be negative when execution starts before the API returns.',
            'Unowned traced activities remain visible; they are not assigned to a neighboring request.',
            'No direct client-TTFT alignment is inferred from NVTX or raw host clocks.',
            'Absence of a copy/memset table means no exported rows, not proof of zero untraced work.',
        ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    group, output = args.group.resolve(), args.output.resolve()
    protected = next((p for p in group.parents if (p / 'frozen-plan.json').is_file()),
                     group.parent)
    require(not output.exists() and not output.is_relative_to(protected),
            'output must be new and outside the frozen stage directory')
    try:
        result = analyze(group)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result = dict(schema=1, trace_integrity='FAIL', performance_acceptance=False,
                      stage_environment_verified=False, group=str(group),
                      analysis_error=f'{type(error).__name__}: {error}')
    with output.open('x') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps(dict(trace_integrity=result['trace_integrity'], output=str(output))))
    return 0 if result['trace_integrity'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
