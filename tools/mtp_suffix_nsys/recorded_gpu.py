#!/usr/bin/env python3
"""Scoped recorded-GPU comparison; preserves the unchanged strict audit.

This module never collects, exports, invokes CUDA, or fills missing parameters.
The caller separately proves HTTP/output/work, normal exits, lease and monitors.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

import analyze_gpu as strict

MAX_LOG = 256 * 1024 * 1024
MAX_SQLITE = 8 * 1024 ** 3
MAX_LINE = 1024 * 1024
MAX_SECONDS = 120
TARGET_WARNINGS = {'Not all CUDA events might have been collected.',
                   'Not all NVTX events might have been collected.'}
MISSING_KIND = 'application_memory_submit_without_activity'
MISSING_NAME = re.compile(r'^cudaMemsetAsync(?:_v\d+)?$')
HEADER = re.compile(r'\bpid=\s*(\d+)\s+tid=\s*(\d+)\s*\|')
TRACE = re.compile(r'Trace\[([^\]]+)\]')
LOSS = re.compile(r'(?:CUPTI dropped|Number of incomplete CUPTI events dropped:)\s*[1-9]\d*|'
                  r'Could not parse|Couldn.t allocate CUPTI buffers|'
                  r'\b(?:dropped|lost)\s+[1-9]\d*\s+(?:events|records)|\bbuffer overflow\b', re.I)
FALLBACK = ('CUPTI failed to enable HES trace. Falling back to legacy '
            '(software instrumentation) tracing. CUptiResult=27')
BACKEND_ERRORS = {
    ('Cupti_Public', '', 'Error: Invalid parameter for enabling raw timestamp: 255.'),
    ('Cupti_Public', '', 'CUPTI Error 27: CUPTI_ERROR_NOT_SUPPORTED'),
    ('Cupti_Public', '', 'Failed to enable HW trace.'),
    ('Injection', 'CheckForHesUsage', 'HES data collection failed, CUPTI_ERROR_NOT_SUPPORTED'),
}
LIMITS = [
    'Qualification covers recorded execution and correlated phase intervals only; it is not whole-trace validity.',
    'The original strict judgment, diagnostics and all missing-memory rows remain unchanged.',
    'Missing cudaMemsetAsync arguments are PARAMETERS_UNOBSERVED_IN_THIS_CAPTURE, never inferred zero.',
    'No callback parameters or correlation IDs are imported from another process or the earlier 8K ledger.',
    'Terminal-service filtering is observed; the warning-emitter branch and its unique cause are not proved.',
    'Matched API/activity pairs cannot exclude simultaneous omission of both records.',
    'Recorded GPU unions exclude unknown work; interval gaps are not proof of device idle time.',
    'CPU wall minus thread CPU is non-running wall, not GPU wait; overlapping durations cannot be added.',
    'One pair is descriptive under instrumentation, not normal performance acceptance or statistical noninferiority.',
]


def require(value, message):
    if not value:
        raise ValueError(message)


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(path, maximum=MAX_LOG):
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        first = os.fstat(fd)
        require(stat.S_ISREG(first.st_mode) and first.st_size <= maximum,
                'source is not a bounded regular file: ' + str(path))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        final, named = os.fstat(fd), path.lstat()
        require(_identity(first) == _identity(final) == _identity(named) and
                not stat.S_ISLNK(named.st_mode), 'source changed during read: ' + str(path))
    finally:
        os.close(fd)
    require(len(data) <= maximum, 'source exceeds bound')
    return data, dict(path=str(path), sha256=hashlib.sha256(data).hexdigest(),
                      bytes=len(data), device=first.st_dev, inode=first.st_ino,
                      mtime_ns=first.st_mtime_ns)


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('fixed 120-second analysis budget exhausted')


def terminal_evidence(data, target_pid):
    """Bind service events to the consumer's exact source-file PID batch.

    A marker's filter must occur before the next ReadStorage event on that
    consumer thread. Its batch summary supplies the source PID; its Trace ID
    and stop timestamp must match the recorded session Stop. No lookahead size
    or global cross-process event count is used to invent ownership.
    """
    require(type(target_pid) is int and 0 < target_pid <= strict.LOW, 'invalid target PID')
    producers = {key: [] for key in ('CUDAFinish', 'NVTXFinish', 'ReportCUDADone')}
    stops, pending, batches = defaultdict(list), {}, defaultdict(list)
    filtered, unresolved, losses, target_errors = [], [], [], []
    backend_enable, backend_fallback = [], []
    wanted = {'CUDA profiling finished': 'CUDAFinish', 'NVTX profiling finished': 'NVTXFinish'}
    for number, raw in enumerate(data.splitlines(), 1):
        require(len(raw) <= MAX_LINE, 'NVLOG line exceeds bound')
        line = raw.decode('utf-8', errors='strict')
        row = dict(line=number, raw=line)
        if LOSS.search(line):
            losses.append(row)
        match = HEADER.search(line)
        if not match:
            continue
        process, tid = map(int, match.groups())
        thread = (process, tid)
        trace = TRACE.search(line)
        if process == target_pid:
            if re.match(r'^[EF]\d\d:', line):
                target_errors.append(row)
            if '[ResourceCallbackHandler]: Enabling HES trace via CUPTI' in line:
                backend_enable.append(row)
            if line.endswith('[ResourceCallbackHandler]: ' + FALLBACK):
                backend_fallback.append(row)
            for key in ('CUDAFinish', 'NVTXFinish'):
                if 'Sending service event: ' + key + '.' in line:
                    producers[key].append(row)
            if 'ReportCUDADone: DONE.' in line:
                producers['ReportCUDADone'].append(row)
        stop = re.search(r'\[Stop\]: Trace\[[^\]]+\]: Stop\((\d+)\)', line)
        if stop and trace:
            stops[(process, trace[1], int(stop[1]))].append(row)
        if '[ReadStorageImpl]: {' in line:
            previous = pending.pop(thread, None)
            if previous:
                batches[thread].append(previous)
            marker = next((name for name in wanted if 'Text="' + name + '"' in line), None)
            if marker:
                pending[thread] = dict(marker=marker, consumer_read=row, filters=[])
        if thread in pending and 'Event ignored: start timestamp ' in line:
            fields = re.search(r'start timestamp (\d+) > session stop timestamp (\d+)', line)
            if fields and trace:
                pending[thread]['filters'].append(dict(consumer_filter=row, trace_id=trace[1],
                    consumer_pid=process, converted_start_ns=int(fields[1]),
                    session_stop_ns=int(fields[2]), after_stop_ns=int(fields[1])-int(fields[2])))
        batch = re.search(r'Events diagnostic:.*from file pid (\d+), uid (\d+)\.', line)
        if batch and trace:
            previous = pending.pop(thread, None)
            if previous:
                batches[thread].append(previous)
            items = batches.pop(thread, [])
            if int(batch[1]) == target_pid:
                for item in items:
                    if len(item['filters']) != 1:
                        unresolved.append(dict(**item, consumer_batch=row,
                            reason='target terminal marker has no unique late filter'))
                        continue
                    fact = item['filters'][0]
                    stop_rows = stops[(process, trace[1], fact['session_stop_ns'])]
                    if not (fact['trace_id'] == trace[1] and fact['after_stop_ns'] > 0 and
                            len(stop_rows) == 1 and
                            stop_rows[0]['line'] < item['consumer_read']['line']):
                        unresolved.append(dict(**item, consumer_batch=row,
                            reason='terminal filter does not bind one preceding same-session Stop'))
                        continue
                    filtered.append(dict(marker=item['marker'], source_pid=target_pid,
                        source_uid=int(batch[2]), consumer_read=item['consumer_read'],
                        consumer_batch=row, session_stop=stop_rows[0], **fact))
    require(not losses, 'NVLOG contains explicit loss/decode failure')
    classified_errors, unknown_errors = [], []
    fallback_bound = bool(backend_fallback and backend_enable and
                         any(e['line'] < f['line'] for e in backend_enable for f in backend_fallback))
    for row in target_errors:
        channel = re.match(r'^E\d\d:\d\d:\d\d\.\d+\|\s*([^|]+?)\s*\|', row['raw'])
        payload = re.search(r'\[([^\]]*)\]: (.*)$', row['raw'])
        known = channel and payload and (channel[1].strip(), payload[1], payload[2]) in BACKEND_ERRORS
        ordered = (known and fallback_bound and
                   (any(f['line'] < row['line'] for f in backend_fallback)
                    if payload[1] == 'CheckForHesUsage' else
                    any(row['line'] < f['line'] for f in backend_fallback)))
        (classified_errors if ordered else unknown_errors).append(row)
    require(not unknown_errors, 'NVLOG contains unknown actual-target error/fatal records or unbound backend failure')
    for key, values in producers.items():
        require(values, 'actual-target producer terminal absent: ' + key)
    by_marker = {name: [r for r in filtered if r['marker'] == name] for name in wanted}
    complete = not unresolved and all(len(values) == 1 for values in by_marker.values())
    if complete:
        for marker, values in by_marker.items():
            complete = complete and any(p['line'] < values[0]['consumer_read']['line']
                                        for p in producers[wanted[marker]])
        complete = complete and len({(r['consumer_pid'], r['trace_id'], r['session_stop_ns'])
                                     for r in filtered}) == 1
    return dict(target_pid=target_pid, producer_finish=producers, consumer_filters=filtered,
                unresolved_terminal_markers=unresolved,
                late_filter_pair_observed=complete, explicit_loss=losses, target_errors=target_errors,
                backend=dict(classification='KNOWN_BACKEND_FALLBACK' if fallback_bound else 'UNOBSERVED',
                             observed_backend='SOFTWARE_FALLBACK' if fallback_bound else 'UNOBSERVED',
                             enable=backend_enable, fallback=backend_fallback,
                             classified_error_records=classified_errors, unknown_error_records=unknown_errors),
                warning_emitter_branch_observed=False, warning_unique_cause_proven=False,
                scope='Exact target source-file batch and same-session service-marker filtering only.')


def _nvlog(path, receipt_path, target_pid):
    receipt_raw, receipt_source = _read(receipt_path)
    receipt = json.loads(receipt_raw)
    require(receipt.get('schema') == 1 and receipt.get('kind') == 'immutable_nvlog_captured_prefix',
            'invalid NVLOG snapshot receipt')
    snapshot, source = _read(receipt['snapshot']['path'])
    final, final_source = _read(path)
    require(receipt['source']['path'] == str(Path(path).absolute()) and
            receipt['source']['identity'] == {k: final_source[k] for k in ('device', 'inode')},
            'NVLOG original identity differs from receipt')
    require(source['sha256'] == receipt['captured_sha256'] == receipt['snapshot']['sha256'] and
            source['bytes'] == receipt['captured_bytes'] == receipt['snapshot']['bytes'] and
            len(final) >= len(snapshot) and final[:len(snapshot)] == snapshot,
            'NVLOG captured prefix or snapshot changed')
    require(all(source[k] == receipt['snapshot'][k] for k in ('device','inode','mtime_ns')),
            'NVLOG snapshot file identity differs')
    require(not (Path(receipt['snapshot']['path']).stat().st_mode & 0o222), 'NVLOG snapshot is writable')
    require(final.endswith(b'\n'), 'NVLOG final observation has an incomplete line')
    evidence = terminal_evidence(final, target_pid)
    evidence.update(source=final_source, captured_snapshot=source, snapshot_receipt=receipt_source,
                    appended_bytes=len(final)-len(snapshot), exact_prefix=True,
                    final_reconciliation_after_outer_guard_required=True)
    return evidence


def all_missing_memory(database, target_pid, audit_result, scratch_dir, deadline):
    """Independently enumerate every raw missing-memory row, not 32 examples."""
    helpers = {(r['source'], r['source_row']) for r in audit_result.get('helper_api_evidence', [])}
    output, rows_seen = [], 0
    with tempfile.TemporaryDirectory(prefix='recorded-memory-', dir=scratch_dir) as directory:
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as raw, \
                sqlite3.connect(str(Path(directory)/'keys.sqlite')) as keys:
            raw.row_factory = sqlite3.Row
            raw.execute('PRAGMA query_only=ON')
            raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100000)
            keys.execute('PRAGMA cache_size=-65536')
            keys.execute('CREATE TABLE gpu(gpid INTEGER,corr INTEGER,PRIMARY KEY(gpid,corr)) WITHOUT ROWID')
            tables = strict.table_names(raw)
            for table in strict.KINDS.values():
                if table not in tables:
                    continue
                pending = []
                for row in raw.execute('SELECT globalPid,correlationId FROM "' + table +
                                       '" WHERE ((globalPid >> 24) & 16777215)=?', (target_pid,)):
                    rows_seen += 1
                    require(rows_seen <= strict.MAX_ROWS, 'GPU key scan exceeds frozen row budget')
                    pending.append(tuple(row))
                    if len(pending) == 4096:
                        _check_time(deadline)
                        keys.executemany('INSERT OR IGNORE INTO gpu VALUES(?,?)', pending)
                        pending.clear()
                keys.executemany('INSERT OR IGNORE INTO gpu VALUES(?,?)', pending)
            for table in ('CUPTI_ACTIVITY_KIND_RUNTIME', 'CUPTI_ACTIVITY_KIND_DRIVER'):
                if table not in tables:
                    continue
                sql = ('SELECT a.rowid AS source_row,a.*,s.value AS name FROM "' + table +
                       '" a JOIN StringIds s ON s.id=a.nameId '
                       'WHERE ((a.globalTid >> 24) & 16777215)=? AND a.returnValue=0')
                if 'callchainId' in strict.columns(raw, table):
                    sql += ' AND a.callchainId IS NULL'
                for row in raw.execute(sql, (target_pid,)):
                    rows_seen += 1
                    if rows_seen % 4096 == 0:
                        _check_time(deadline)
                    require(rows_seen <= strict.MAX_ROWS, 'API/key scan exceeds frozen row budget')
                    if strict.api_category(row['name']) not in ('copy', 'memset') or \
                            (table, row['source_row']) in helpers:
                        continue
                    if keys.execute('SELECT 1 FROM gpu WHERE gpid=? AND corr=?',
                                    (row['globalTid'] & strict.MASK, row['correlationId'])).fetchone():
                        continue
                    output.append(dict(source=table, **dict(row),
                        classification='PARAMETERS_UNOBSERVED_IN_THIS_CAPTURE',
                        observed_count_argument=None, zero_inferred=False))
    return output


def qualify(audit_result, missing, terminal, target_pid):
    require('analysis_error' not in audit_result, 'strict analyzer could not parse the trace')
    require(audit_result.get('source_unchanged_after_read') is True and
            audit_result.get('source', {}).get('target_pid') == target_pid,
            'strict source/PID binding incomplete')
    require(not audit_result.get('api_errors'), 'strict CUDA API error')
    diagnostics = audit_result['diagnostics']
    require(not diagnostics['errors'] and not diagnostics['profiler_log_findings'],
            'strict diagnostic error/loss/log failure')
    warnings = diagnostics['warnings_require_review']
    if warnings:
        require(Counter(r['text'].strip() for r in warnings) == Counter(TARGET_WARNINGS) and
                all(r.get('severity_name') == 'Warning' and r.get('globalPid') is not None and
                    ((r['globalPid'] >> 24) & strict.LOW) == target_pid for r in warnings),
                'warning is new, duplicated, or does not belong to the actual target')
        require(terminal['late_filter_pair_observed'], 'warning pair lacks per-capture terminal-filter evidence')
    else:
        require(diagnostics['valid'] is True, 'no-warning strict diagnostics not valid')
    findings = audit_result['coverage_findings']
    require(all(r.get('kind') == MISSING_KIND and r.get('status') == 'UNKNOWN_CAPABILITY'
                for r in findings), 'strict structural/launch/kernel/ownership failure')
    require(sum(r['count'] for r in findings) == len(missing), 'full raw missing-memory count disagrees with strict audit')
    require(all(r['source'] == 'CUPTI_ACTIVITY_KIND_RUNTIME' and MISSING_NAME.fullmatch(r['name'])
                for r in missing), 'missing copy or unsupported memory submission')
    require(not audit_result['coverage']['absent_activity_tables'], 'required GPU activity table absent')
    require(audit_result['coverage']['cpu_spans'] == audit_result['coverage']['nvtx_ranges'] > 0,
            'CPU/NVTX keyed coverage incomplete')
    require(audit_result.get('trace_integrity') in ('PASS', 'FAIL', 'UNKNOWN_CAPABILITY'),
            'unknown strict integrity judgment')
    expected_strict = 'FAIL' if warnings else ('UNKNOWN_CAPABILITY' if findings else 'PASS')
    require(audit_result['trace_integrity'] == expected_strict,
            'strict integrity has another unexplained failure')
    return dict(warnings_retained=warnings,
                warning_classification=('TERMINAL_FILTER_COMPATIBLE_NOT_COMPLETE' if warnings else 'STRICT_DIAGNOSTICS_VALID'),
                missing_memory_count=len(missing), missing_memory_by_name=dict(Counter(r['name'] for r in missing)),
                parameters_imported_from_other_processes=False, zero_parameters_inferred=0)


def analyze_capture(output, target_pid, wrapper_pid, *, scratch_dir, nvlog_path, nvlog_snapshot_receipt):
    """One strict audit invocation followed by a separately labelled scope gate."""
    started = time.monotonic()
    deadline = started + MAX_SECONDS
    output, scratch_dir = Path(output).resolve(), Path(scratch_dir).resolve()
    strict_path, result_path = output/'strict-audit.json', output/'recorded-gpu-audit.json'
    require(not strict_path.exists() and not result_path.exists(), 'refusing analysis output overwrite')
    result = dict(schema=1, status='INVALID', qualification_passed=False, errors=[],
                  stage_environment_verified=False, full_trace_integrity_proven=False,
                  performance_acceptance=False, model_qualification=False, limitations=list(LIMITS))
    audit_result = None
    try:
        require(type(target_pid) is int and 0 < target_pid <= strict.LOW and
                (wrapper_pid is None or type(wrapper_pid) is int and 0 < wrapper_pid <= strict.LOW and wrapper_pid != target_pid),
                'invalid target/wrapper identity')
        database = output/'trace.sqlite'
        info = database.lstat()
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_SQLITE,
                'SQLite exceeds fixed bound or is not regular')
        server, server_source = _read(output/'server.log')
        export, export_source = _read(output/'export.log')
        scratch_dir.mkdir(parents=True, exist_ok=True)
        try:
            audit_result = strict.audit_database(database, target_pid,
                server_text=server.decode('utf-8'), export_text=export.decode('utf-8'),
                scratch_dir=scratch_dir, mode='model', wrapper_pid=wrapper_pid)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            audit_result = dict(trace_integrity='FAIL', analysis_error=repr(error),
                                performance_acceptance=False)
        save(strict_path, audit_result)
        result['strict_audit'] = dict(path=str(strict_path), sha256=strict.sha(strict_path),
                                     trace_integrity=audit_result['trace_integrity'])
        _check_time(deadline)
        require('analysis_error' not in audit_result, 'strict trace parsing failed')
        terminal = _nvlog(nvlog_path, nvlog_snapshot_receipt, target_pid)
        missing = all_missing_memory(database, target_pid, audit_result, scratch_dir, deadline)
        result.update(terminal_evidence=terminal, all_unmatched_memory=missing,
                      source_logs=dict(server=server_source, export=export_source))
        classification = qualify(audit_result, missing, terminal, target_pid)
        require(strict.sha(database) == audit_result['source']['sha256'], 'SQLite changed after full row check')
        _check_time(deadline)
        result.update(status='QUALIFIED_RECORDED_GPU_ONLY', qualification_passed=True,
                      classification=classification,
                      measurements_location='Original strict-audit.json, including its unchanged strict failure/unknown judgment.',
                      final_driver_lifecycle_and_environment_audit_required=True)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, sqlite3.Error, TimeoutError) as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    result['elapsed_seconds'] = time.monotonic()-started
    save(result_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target-pid', type=int, required=True)
    parser.add_argument('--wrapper-pid', type=int)
    parser.add_argument('--scratch-dir', type=Path, required=True)
    parser.add_argument('--nvlog-path', type=Path, required=True)
    parser.add_argument('--nvlog-snapshot-receipt', type=Path, required=True)
    args = parser.parse_args()
    result = analyze_capture(args.output, args.target_pid, args.wrapper_pid,
        scratch_dir=args.scratch_dir, nvlog_path=args.nvlog_path,
        nvlog_snapshot_receipt=args.nvlog_snapshot_receipt)
    print(json.dumps(dict(status=result['status'], qualification_passed=result['qualification_passed'],
                          errors=result['errors'])))
    return 0 if result['qualification_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
