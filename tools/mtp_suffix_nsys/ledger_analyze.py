#!/usr/bin/env python3
"""Read-only, bounded analysis of the selected-request memset ledger.

No profiler, CUDA, process inspection, export, or measurement is invoked.  A
VALID result closes only the observed memset API/parameter/activity contract.
"""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import mechanism_analyze as mechanism

MAX_BYTES = 64 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_RECORDS = 131072
RUNTIME_NAMES = {49: 'cudaMemset', 51: 'cudaMemsetAsync',
                 233: 'cudaMemset_ptds', 235: 'cudaMemsetAsync_ptsz'}
# Pinned CUDA 13.3 cupti_driver_cbid.h. These callbacks only establish ownership;
# their byte/element arguments are deliberately not sampled by this collector.
DRIVER_NAMES = dict(zip(
    (71, 73, 75, 77, 79, 81, 216, 218, 220, 222, 224, 226,
     249, 250, 251, 252, 253, 254, 411, 412, 413, 414, 415, 416,
     428, 429, 430, 431, 432, 433),
    ('cuMemsetD8', 'cuMemsetD16', 'cuMemsetD32', 'cuMemsetD2D8',
     'cuMemsetD2D16', 'cuMemsetD2D32', 'cuMemsetD8Async', 'cuMemsetD16Async',
     'cuMemsetD32Async', 'cuMemsetD2D8Async', 'cuMemsetD2D16Async',
     'cuMemsetD2D32Async', 'cuMemsetD8_v2', 'cuMemsetD16_v2', 'cuMemsetD32_v2',
     'cuMemsetD2D8_v2', 'cuMemsetD2D16_v2', 'cuMemsetD2D32_v2',
     'cuMemsetD8_v2_ptds', 'cuMemsetD16_v2_ptds', 'cuMemsetD32_v2_ptds',
     'cuMemsetD2D8_v2_ptds', 'cuMemsetD2D16_v2_ptds', 'cuMemsetD2D32_v2_ptds',
     'cuMemsetD8Async_ptsz', 'cuMemsetD16Async_ptsz', 'cuMemsetD32Async_ptsz',
     'cuMemsetD2D8Async_ptsz', 'cuMemsetD2D16Async_ptsz', 'cuMemsetD2D32Async_ptsz')))
DRIVER_NAMES.update({cbid + 1: DRIVER_NAMES[cbid].replace('cuMemset', 'cu64Memset', 1)
                     for cbid in (71, 73, 75, 77, 79, 81, 216, 218, 220, 222, 224, 226)})
LIMITS = [
    'Only the single selected capture interval and the four selected runtime memset APIs are covered.',
    'Driver callbacks are correlation bridges; their unobserved count field is not a zero-byte argument.',
    'The collector uses SYSTEM thread IDs; no inferred pthread/OS thread mapping is used.',
    'No correlation ID or parameter is transferred between processes or old captures.',
    'Observed zero-byte calls do not retroactively identify the old 6381 calls.',
    'API/activity closure cannot exclude simultaneous loss of both records outside collector loss reporting.',
    'This is instrumented local evidence, not normal latency, throughput, whole-trace or model acceptance.',
    'Startup allocation work before ProfilerStart is outside this request ledger.',
]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum, 'invalid ' + name)
    return value


def unique_object(items):
    result = {}
    for key, value in items:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_bounded(path):
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        first = os.fstat(fd)
        require(stat.S_ISREG(first.st_mode) and first.st_size <= MAX_BYTES,
                'input is not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        last, named = os.fstat(fd), path.lstat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        require(identity(first) == identity(last) == identity(named) and
                not stat.S_ISLNK(named.st_mode), 'input changed during read')
    finally:
        os.close(fd)
    require(len(raw) <= MAX_BYTES, 'input exceeds byte bound')
    return raw, dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def initial(kind):
    return dict(schema=1, kind=kind, status='INVALID', errors=[], completed=False,
                performance_acceptance=False, model_qualification=False,
                whole_trace_completeness_claimed=False, proof_limits=list(LIMITS))


def _one(rows, label):
    require(len(rows) == 1, label + ': expected exactly one')
    return rows[0]


def _parse(path, result):
    raw, result['source'] = read_bounded(path)
    require(raw and raw.endswith(b'\n'), 'ledger is empty or has an incomplete final line')
    records = []
    for number, line in enumerate(raw.splitlines(), 1):
        require(0 < len(line) <= MAX_LINE, 'empty/oversized ledger line')
        row = json.loads(line, object_pairs_hook=unique_object)
        require(type(row) is dict and row.get('event') in
                ('config', 'boundary', 'api', 'activity', 'summary'), 'unknown ledger event')
        row['_source_line'] = number
        records.append(row)
        require(len(records) <= MAX_RECORDS + 4, 'ledger record bound exceeded')
    require(records[0]['event'] == 'config' and records[-1]['event'] == 'summary',
            'config/summary are not first/last')
    groups = {name: [r for r in records if r['event'] == name]
              for name in ('config', 'boundary', 'api', 'activity', 'summary')}
    return records, groups


def _analyze(path, expected_pid, result):
    records, groups = _parse(path, result)
    config, summary = _one(groups['config'], 'config'), _one(groups['summary'], 'summary')
    pid, tid = integer(config.get('pid'), 'config pid', 1), integer(config.get('tid'), 'config tid', 1)
    require(expected_pid is None or integer(expected_pid, 'expected pid', 1) == pid,
            'unexpected target PID')
    require(config.get('schema') == 1 and config.get('mode') == 'memset_ledger' and
            config.get('runtime_cbids') == list(RUNTIME_NAMES) and
            config.get('record_capacity') == MAX_RECORDS and
            config.get('thread_id_type') == 'SYSTEM', 'collector configuration differs')
    for field, value in dict(collector_limit_bytes=MAX_BYTES, log_limit_bytes=MAX_BYTES,
                             host_buffer_count=8, host_buffer_bytes=1048576,
                             device_buffer_count=4, device_buffer_bytes=1048576,
                             configured_cupti_buffer_bytes=5 * 1048576).items():
        require(type(config.get(field)) is int and config[field] == value,
                'fixed collector configuration differs: ' + field)
    libraries = config.get('libraries')
    require(type(libraries) is list and len(libraries) == 3 and
            all(type(r) is dict for r in libraries) and
            {r.get('name') for r in libraries} == {'cudart', 'cupti_linked', 'cublasLt'} and
            all(isinstance(r.get('path'), str) and Path(r['path']).is_absolute() for r in libraries),
            'missing/duplicate runtime library receipts')
    for key in ('runtime_version', 'driver_version', 'cupti_version',
                'cupti_header_api_version', 'cublasLt_version'):
        integer(config.get(key), key, 1)
    result.update(target_pid=pid, config=config, collector_summary=summary)
    boundary = groups['boundary']
    require(len(boundary) == 2, 'need exactly one start/end boundary')
    begin, end = boundary
    require(begin.get('site') == 'start' and end.get('site') == 'end', 'boundary order differs')
    for row in boundary:
        require(row.get('pid') == pid and row.get('tid') == tid and
                row.get('original_return') == 0, 'boundary PID/TID/original return differs')
        integer(row.get('timestamp'), 'boundary CUPTI timestamp', 1)
    require(begin['timestamp'] < end['timestamp'], 'invalid boundary timestamps')
    require(config['_source_line'] < begin['_source_line'] < end['_source_line'] <
            summary['_source_line'], 'boundary/config/summary source order differs')
    result['boundaries'] = boundary
    api, activity = groups['api'], groups['activity']
    sequence = [integer(r.get('seq'), 'record seq', 1) for r in records
                if r['event'] in ('api', 'activity')]
    require(sequence == list(range(1, len(sequence) + 1)), 'record sequence gap/duplicate/reorder')
    require(len(sequence) <= MAX_RECORDS, 'collector record capacity exceeded')
    actual = dict(api_enter_count=sum(r.get('site') == 'enter' for r in api),
                  api_exit_count=sum(r.get('site') == 'exit' for r in api),
                  activity_count=len(activity), record_count=len(sequence))
    for key, value in actual.items():
        require(type(summary.get(key)) is int and summary[key] == value,
                'summary actual count differs: ' + key)
    for key in ('errors', 'drops', 'overflow', 'buffers_outstanding'):
        require(type(summary.get(key)) is int and summary[key] == 0, 'collector ' + key)
    for key in ('flushed', 'disabled', 'unsubscribed', 'completed'):
        require(summary.get(key) is True, 'collector did not finish: ' + key)
    for key in ('finalized', 'cuda_sync_added'):
        require(summary.get(key) is False, 'unexpected collector operation: ' + key)
    require(summary.get('original_start_calls') == 1 and summary.get('original_stop_calls') == 1 and
            summary.get('flush_flags') == [0, 1], 'original API/flush call contract differs')
    require(summary.get('mode') == 'memset_ledger' and summary.get('pid') == pid and
            summary.get('tid') == tid and summary.get('subscriber_used') is True,
            'summary identity/subscriber differs')
    for key in ('collector_limit_bytes', 'collector_owned_bytes', 'configured_cupti_buffer_bytes'):
        require(summary.get(key) == config[key], 'summary allocation differs: ' + key)
    require(integer(summary.get('buffer_requests'), 'buffer_requests', 1) ==
            integer(summary.get('buffer_completions'), 'buffer_completions', 1),
            'activity buffer request/completion differs')
    require(integer(config.get('collector_owned_bytes'), 'collector_owned_bytes', 1) +
            integer(config.get('configured_cupti_buffer_bytes'), 'configured_cupti_buffer_bytes', 1)
            <= MAX_BYTES, 'explicit collector allocation budget exceeded')
    result['raw_counts'] = actual
    pairs, stacks, by_corr = {}, defaultdict(list), defaultdict(list)
    identity_fields = ('domain', 'name', 'pid', 'tid', 'correlation_id', 'enter_seq', 'phase',
                       'parent_runtime_correlation_id', 'parent_runtime_enter_seq',
                       'context_uid', 'cbid', 'is_memset',
                       'count', 'dev_ptr', 'value', 'stream', 'stream_parameter_present')
    for row in api:
        domain, site = row.get('domain'), row.get('site')
        require(domain in ('runtime', 'driver') and site in ('enter', 'exit'), 'invalid callback kind')
        require(row.get('pid') == pid and row.get('phase') == 'capture', 'callback PID/phase differs')
        thread = integer(row.get('tid'), 'callback tid', 1)
        corr = integer(row.get('correlation_id'), 'callback correlation', 1)
        entry = integer(row.get('enter_seq'), 'callback enter_seq', 1)
        cbid = integer(row.get('cbid'), 'callback cbid', 1)
        integer(row.get('context_uid'), 'callback context UID')
        require(isinstance(row.get('name'), str) and row['name'], 'callback name missing')
        if domain == 'runtime':
            require(cbid in RUNTIME_NAMES and mechanism.api_name(row['name']) == RUNTIME_NAMES[cbid],
                    'unrecognized runtime callback ID/name')
            require(row.get('is_memset') is True, 'runtime memset parameters absent')
            integer(row.get('count'), 'runtime memset count')
            integer(row.get('dev_ptr'), 'runtime memset pointer', 1)
            integer(row.get('stream'), 'runtime memset stream')
            require(row.get('stream_parameter_present') is (cbid in (51, 235)),
                    'runtime stream argument presence differs from callback signature')
            require(cbid in (51, 235) or row['stream'] == 0,
                    'synchronous memset carries an invented stream argument')
            require(type(row.get('value')) is int, 'runtime memset value absent')
        else:
            require(cbid in DRIVER_NAMES and row['name'] == DRIVER_NAMES[cbid] and
                    row.get('is_memset') is False and
                    row.get('count') == 0 and row.get('stream_parameter_present') is False,
                    'driver is not an unparameterized memset bridge')
        key = entry
        stack = stacks[thread]
        if site == 'enter':
            require(key not in pairs and row['seq'] == entry and row.get('retval') == -1,
                    'duplicate/malformed ENTER')
            parent = next((p for p in reversed(stack) if pairs[p]['enter']['domain'] == 'runtime'), None)
            require(row.get('parent_runtime_correlation_id') ==
                    (pairs[parent]['enter']['correlation_id'] if parent else 0),
                    'TLS runtime parent does not match callback nesting')
            require(row.get('parent_runtime_enter_seq') == (parent or 0),
                    'TLS runtime parent instance differs')
            require(domain == 'runtime' or parent is not None, 'direct driver memset has unknown parameters')
            require(domain != 'runtime' or parent is None, 'nested selected runtime memset is ambiguous')
            pairs[key] = dict(enter=row, parent=parent, activities=[])
            by_corr[(domain, corr)].append(key)
            stack.append(key)
        else:
            require(key in pairs and 'exit' not in pairs[key] and stack and stack[-1] == key,
                    'unmatched/reordered/duplicate EXIT')
            require(all(row.get(k) == pairs[key]['enter'].get(k) for k in identity_fields),
                    'ENTER/EXIT identity or parameters changed')
            require(row.get('retval') == 0 and type(row['retval']) is int, 'unsuccessful API return')
            pairs[key]['exit'] = row
            stack.pop()
    require(pairs and all(not s for s in stacks.values()) and
            all('exit' in p for p in pairs.values()), 'callbacks not completely closed')
    for (domain, corr), keys in by_corr.items():
        if len(keys) == 1:
            continue
        parent = pairs[keys[0]]['parent']
        require(domain == 'driver' and parent is not None and
                all(pairs[k]['parent'] == parent for k in keys) and
                all(pairs[a]['exit']['seq'] < pairs[b]['enter']['seq'] for a, b in zip(keys, keys[1:])),
                'correlation ID reused across owners or overlapping callbacks')
    runtime = {key: pair for key, pair in pairs.items() if pair['enter']['domain'] == 'runtime'}
    children = defaultdict(list)
    for pair in pairs.values():
        if pair['parent'] is not None:
            children[pair['parent']].append(pair['enter'])
    runtime_activity, gpu = {}, []
    for row in activity:
        require(row.get('kind') in ('runtime', 'memset') and row.get('process_id') == pid,
                'unexpected activity kind/PID')
        start = integer(row.get('start'), 'activity start', 1)
        stop = integer(row.get('end'), 'activity end', 1)
        require(stop >= start, 'activity timestamp incomplete/reversed')
        integer(row.get('correlation_id'), 'activity correlation', 1)
        if row['kind'] == 'runtime':
            require(row.get('process_id_source') == 'activity_record', 'runtime PID not from activity record')
            keys = by_corr[('runtime', row['correlation_id'])]
            require(len(keys) == 1, 'runtime activity has no unique callback')
            key, owner = keys[0], pairs[keys[0]]['enter']
            require(key not in runtime_activity and row.get('cbid') == owner['cbid'] and
                    row.get('thread_id') == owner['tid'] and row.get('return_value') == 0 and
                    mechanism.api_name(row.get('name', '')) == mechanism.api_name(owner['name']),
                    'runtime activity duplicate/CBID/SYSTEM TID/name/return mismatch')
            require(begin['timestamp'] <= start <= stop <= end['timestamp'],
                    'runtime API activity outside capture boundary')
            runtime_activity[key] = row
        else:
            require(row.get('process_id_source') == 'collector_process', 'GPU PID source differs')
            require(row.get('runtime_correlation_id') in (None, 0), 'unexpected raw MEMSET runtime correlation')
            integer(row.get('bytes'), 'MEMSET activity bytes')
            integer(row.get('device_id'), 'MEMSET device')
            integer(row.get('context_id'), 'MEMSET context', 1)
            integer(row.get('stream_id'), 'MEMSET stream')
            require(type(row.get('value')) is int, 'MEMSET activity value absent')
            gpu.append(row)
    require(set(runtime_activity) == set(runtime), 'selected runtime API activity missing')
    gpu_signatures = set()
    for row in gpu:
        signature = tuple(row.get(k) for k in ('correlation_id', 'start', 'end', 'bytes', 'value',
                                              'device_id', 'context_id', 'stream_id'))
        require(signature not in gpu_signatures, 'duplicate MEMSET activity')
        gpu_signatures.add(signature)
        drivers = by_corr[('driver', row['correlation_id'])]
        owners = {pairs[key]['parent'] for key in drivers}
        require(drivers and len(owners) == 1 and None not in owners, 'MEMSET has no unique driver/runtime bridge')
        owner_key = next(iter(owners))
        owner = runtime[owner_key]
        require(row['start'] >= runtime_activity[owner_key]['start'], 'MEMSET precedes runtime API')
        owner['activities'].append(row)
    calls = []
    for key, pair in runtime.items():
        row, acts = pair['enter'], pair['activities']
        count = row['count']
        require(all(a['value'] == (row['value'] & 255) for a in acts), 'MEMSET GPU byte value differs')
        gpu_bytes = sum(a['bytes'] for a in acts)
        require((count == 0 and gpu_bytes == 0) or
                (count > 0 and acts and gpu_bytes == count), 'nonzero MEMSET activity missing/byte total differs')
        calls.append(dict(enter_seq=key, exit_seq=pair['exit']['seq'], pid=pid, tid=row['tid'],
                          name=row['name'], cbid=row['cbid'], correlation_id=row['correlation_id'],
                          count=count, value=row['value'], dev_ptr=row['dev_ptr'], stream=row['stream'],
                          stream_parameter_present=row['stream_parameter_present'],
                          retval=pair['exit']['retval'], runtime_activity=runtime_activity[key],
                          driver_bridges=children[key],
                          activities=acts, gpu_activity_count=len(acts), gpu_bytes=gpu_bytes,
                          classification=('EXPLICIT_ZERO_NO_ACTIVITY' if count == 0 and not acts else
                                          'EXPLICIT_ZERO_WITH_ZERO_ACTIVITY' if count == 0 else
                                          'NONZERO_ACTIVITY_COVERED')))
    result.update(status='VALID', completed=True, per_call=calls,
                  counts=dict(runtime_memset_calls=len(calls), driver_bridge_calls=len(pairs)-len(calls),
                              zero_calls=sum(c['count'] == 0 for c in calls),
                              nonzero_calls=sum(c['count'] > 0 for c in calls),
                              memset_activities=len(gpu), nonzero_bytes=sum(c['count'] for c in calls)),
                  byte_count_distribution=dict(sorted(Counter(str(c['count']) for c in calls).items())))


def analyze(path, expected_pid=None):
    result = initial('SELECTED_REQUEST_MEMSET_LEDGER')
    try:
        _analyze(path, expected_pid, result)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, AttributeError, IndexError) as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    return result


def validate_fixture(ledger_result, probe_log_path, expected_pid=None):
    result = initial('KNOWN_WORK_PRELOAD_FIXTURE_NO_NSYS')
    try:
        require(ledger_result.get('status') == 'VALID' and ledger_result.get('errors') == [],
                'ledger is not valid')
        log = mechanism.read_log(probe_log_path)
        require(not any('[memset-ledger-error]' in line for line in log['other_lines']),
                'collector reported an error outside ledger')
        shared = mechanism.common(log, 'nsys')
        result.update(source=log['source'], target_pid=shared['pid'], algorithms=shared['algorithms'],
                      probe_mode_name='nsys', nsight_was_run=False,
                      probe_has_internal_subscriber=False)
        require(ledger_result['target_pid'] == shared['pid'] and
                (expected_pid is None or expected_pid == shared['pid']), 'fixture/ledger PID differs')
        calls = ledger_result['per_call']
        # The fixed source executes control0, control512, first GEMM, cached
        # GEMM, sequentially on this thread, with a readback after each phase.
        require(len(calls) == 4 and [c['count'] for c in calls] == [0, 512, 0, 0],
                'fixed four-call control/GEMM parameter sequence not reproduced')
        require(all(c['tid'] == shared['tid'] for c in calls), 'fixture calls changed thread')
        require(calls[0]['value'] == calls[1]['value'] == 0x5a and
                calls[0]['dev_ptr'] == calls[1]['dev_ptr'] and
                calls[0]['stream'] == calls[1]['stream'], 'fixed controls differ')
        require(calls[1]['gpu_activity_count'] == 1 and calls[1]['gpu_bytes'] == 512,
                'positive 512-byte control activity missing')
        result.update(status='VALID', completed=True, eligible_model=True,
                      exact_output_checks=4, actual_call_sequence=calls,
                      association_basis='Fixed probe source order on one thread; no GPU kernel or copy activities collected.')
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, AttributeError, IndexError) as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
        result['eligible_model'] = False
    return result


def validate_model(ledger_result, server_log_path, expected_pid=None, expected_input_tokens=8170):
    result = initial('ACTUAL_8K_LEDGER_SERVER_ORACLE')
    try:
        require(ledger_result.get('status') == 'VALID' and ledger_result.get('errors') == [],
                'ledger is not valid')
        raw, result['source'] = read_bounded(server_log_path)
        text = raw.decode('utf-8', errors='strict')
        require('[memset-ledger-error]' not in text, 'collector reported an error outside ledger')
        pid = ledger_result['target_pid']
        require(expected_pid is None or expected_pid == pid, 'model ledger PID differs')
        captures = [dict(re.findall(r'(\w+)=([^\s]+)', line)) for line in text.splitlines()
                    if line.startswith('[exec-capture] ')]
        require(len(captures) == 2 and [r.get('event') for r in captures] == ['begin', 'end'],
                'model capture markers not closed')
        require(all(r.get('pid') == str(pid) and r.get('input_tokens') == str(expected_input_tokens) and
                    r.get('error') == '0' for r in captures), 'model capture identity/input/error differs')
        spans = [dict(re.findall(r'(\w+)=([^\s]+)', line)) for line in text.splitlines()
                 if line.startswith('[exec-span] ') and ' phase=mtp.verify_forward ' in line]
        require(len(spans) == 74 and all(r.get('pid') == str(pid) and r.get('t') == '4' and
                                       r.get('closed') == '1' for r in spans), '74 T4 verify spans not closed')
        require(len({(r.get('pid'), r.get('tid'), r.get('batch'), r.get('id')) for r in spans}) == 74,
                'duplicate verify span identity')
        accepted = re.findall(r'^\[mtp-accept\] request=(\S+) draft_counts=(\d+),(\d+),(\d+),(\d+)\s*$',
                              text, re.MULTILINE)
        require(len(accepted) == 1, 'expected one MTP acceptance summary')
        request_id, *values = accepted[0]
        hist = list(map(int, values))
        require(hist == [7, 8, 7, 52], 'fixed output acceptance distribution differs')
        endings = [dict(re.findall(r'(\w+)=([^\s]+)', line)) for line in text.splitlines()
                   if line.startswith('[q4t][request] ') and ' event=end ' in line]
        finish = _one(endings, 'request end')
        require(finish.get('id') == request_id and finish.get('released') == '1' and
                finish.get('outcome') == 'success' and finish.get('reason') == 'completed' and
                finish.get('scheduler_waits') == '77', 'request end/work/slot closure differs')
        require('[suffix] ' not in text and 'suffix enabled' not in text, 'suffix unexpectedly enabled')
        result.update(status='VALID', completed=True, target_pid=pid, request_id=request_id,
                      oracle_counts=dict(t4=74, t1=3, scheduler_waits=77, accept_histogram=hist,
                                         t1_basis='scheduler_waits minus verified T4 calls'),
                      independent_http_output_oracle_required=True)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, AttributeError, IndexError) as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    return result
