#!/usr/bin/env python3
"""Bounded, read-only analysis of the one-shape memset mechanism probe.

No CUDA, profiler, export, subprocess, or live process APIs are used here.
This is a local mechanism contract, never model/trace/performance acceptance.
"""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct

MAX_LOG = 64 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_EVENTS = 200000
MAX_SQL_ROWS = 1000000
PHASES = ('control_zero', 'control_nonzero', 'gemm_first', 'gemm_cached')
GEMMS = PHASES[2:]
FIXED = dict(M=4, N=48, K=2560, workspace_bytes=33554432)
FAMILY = 'cutlass_80_wmma_tensorop_bf16_s161616gemm_bf16_16x16_64x1_tn_align8'
PREFIXES = ('config', 'event', 'ready', 'exit')
LIMITS = [
    'One fixed shape and two cache phases; no population or performance claim.',
    'CUPTI and Nsight are separate processes; their correlation IDs are never joined.',
    'Missing parameters/records are UNKNOWN, never inferred as a zero byte count.',
    'Observed zero-byte calls do not retroactively identify the old 6381 calls.',
    'API/activity closure cannot exclude loss of both an API and its activity.',
    'Profiler warnings remain recorded; local observation does not waive model gates.',
    'Algorithm/key and kernel-family correspondence is not cross-process event identity.',
    'OS/CUPTI thread correspondence is inferred from unique API identities, not a direct pthread sample.',
]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, label, low=0):
    require(type(value) is int and value >= low, label + ': invalid integer')
    return value


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_log(path):
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_LOG,
                'probe log is not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_LOG + 1)
        after = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        require(identity == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                'probe log changed while reading')
        final = path.lstat()
        require(not stat.S_ISLNK(final.st_mode) and
                (final.st_dev, final.st_ino) == (before.st_dev, before.st_ino),
                'probe log path changed')
    finally:
        os.close(fd)
    require(len(raw) <= MAX_LOG, 'probe log exceeds bound')
    found = {name: [] for name in PREFIXES}
    other = []
    for ordinal, line in enumerate(raw.decode('utf-8', errors='strict').splitlines(), 1):
        require(len(line.encode()) <= MAX_LINE, 'oversized log line')
        match = re.match(r'^\[mechanism-(config|event|ready|exit)\]\s*(.*)$', line)
        if match:
            item = json.loads(match[2], object_pairs_hook=unique_object)
            require(isinstance(item, dict), 'event is not an object')
            item = dict(item, _source_line=ordinal)
            found[match[1]].append(item)
        else:
            require(not line.startswith('[mechanism-'), 'unknown mechanism record prefix')
            other.append(line)
        require(sum(map(len, found.values())) <= MAX_EVENTS, 'record count bound exceeded')
    found['other_lines'] = other
    found['source'] = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                           bytes=len(raw))
    return found


def one(items, label):
    require(len(items) == 1, label + ': expected exactly one record')
    return items[0]


def event_name(row):
    # Producer emits event; reject contradictory aliases rather than guessing.
    require(isinstance(row.get('event'), str), 'missing event discriminator')
    return row['event']


def api_name(name):
    # cuFoo_v2 is an API name; _v3020 is a CUPTI callback-version suffix.
    return re.sub(r'_v[0-9]{4,}$', '', name)


def bf16_word(value):
    return struct.unpack('<I', struct.pack('<f', float(value)))[0] >> 16


def common(log, mode):
    config = one(log['config'], 'config')
    ready = one(log['ready'], 'ready')
    end = one(log['exit'], 'exit')
    pid, tid = integer(config['pid'], 'config pid', 1), integer(config['tid'], 'config tid', 1)
    require(config['mode'] == mode, 'wrong probe mode')
    for key, value in FIXED.items():
        require(config.get(key) == value and ready.get(key) == value, 'fixed config: ' + key)
    require(ready.get('pid') == pid and ready.get('tid') == tid and ready.get('mode') == mode,
            'ready identity differs')
    require(ready.get('passed') is True and ready.get('gemm_calls') == 2 and
            ready.get('controls') == 2, 'ready workload/output contract failed')
    for key in ('profiler_start_error', 'profiler_stop_error', 'final_sync_error'):
        require(ready.get(key) == 0, 'ready missing/nonzero ' + key)
    require(end.get('pid') == pid and end.get('tid') == tid and
            end.get('released') is True and end.get('returncode') == 0,
            'probe did not release and exit successfully')
    require(config['_source_line'] < ready['_source_line'] < end['_source_line'],
            'config/ready/exit ordering differs')
    events = log['event']
    allowed = {'phase_begin', 'phase_end', 'api', 'activity', 'algo', 'check', 'summary',
               'library', 'gemm_return'}
    require(all(event_name(r) in allowed for r in events), 'unknown/error probe event')
    groups = {name: [r for r in events if event_name(r) == name] for name in allowed}
    require(len(groups['library']) == 3 and {r.get('name') for r in groups['library']} ==
            {'cudart', 'cublasLt', 'cupti_linked'}, 'missing/duplicate library receipts')
    for library in groups['library']:
        require(Path(library['path']).is_absolute() and integer(library['bytes'], 'library bytes', 1) and
                integer(library['inode'], 'library inode', 1), 'invalid library receipt')
    require(len(groups['gemm_return']) == 2, 'missing GEMM return records')
    for phase in GEMMS:
        returned = one([r for r in groups['gemm_return'] if r.get('phase') == phase], phase + ' return')
        require(returned.get('status') == 0 and returned.get('has_algo') is True,
                'production GEMM failed: ' + phase)
    for name in ('phase_begin', 'phase_end', 'check'):
        require(len(groups[name]) == 4 and {r.get('phase') for r in groups[name]} == set(PHASES),
                name + ': missing/duplicate phase')
    phase_last = 0
    for ordinal, phase in enumerate(PHASES):
        begin = one([r for r in groups['phase_begin'] if r['phase'] == phase], phase + ' begin')
        finish = one([r for r in groups['phase_end'] if r['phase'] == phase], phase + ' end')
        require(begin.get('ordinal') == ordinal and finish.get('ordinal') == ordinal and
                phase_last < begin['_source_line'] < finish['_source_line'],
                'phase ordering/ordinal differs: ' + phase)
        phase_last = finish['_source_line']
        check = one([r for r in groups['check'] if r['phase'] == phase], phase + ' check')
        if phase == 'control_zero':
            expected = [0xA5] * 512
        elif phase == 'control_nonzero':
            expected = [0x5A] * 512
        else:
            expected = [bf16_word((n % 7 - 3) * (m % 5 + 1) * (m + 1))
                        for m in range(4) for n in range(48)]
        require(check.get('passed') is True and check.get('mismatches') == 0 and
                check.get('expected_words') == expected and check.get('actual_words') == expected,
                'independent fixed output mismatch: ' + phase)
    require(len(groups['algo']) == 2, 'need two algorithm records')
    algos = {}
    for phase in GEMMS:
        row = one([r for r in groups['algo'] if r.get('phase') == phase], phase + ' algo')
        require(row.get('key') == dict(FIXED, out_bits=0), 'GEMM key differs')
        require(isinstance(row.get('raw_hex'), str) and
                re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,512}', row['raw_hex']), 'invalid algorithm bytes')
        require(isinstance(row.get('attrs'), list) and 0 < len(row['attrs']) <= 32,
                'missing algorithm attributes')
        require(len({a['name'] for a in row['attrs']}) == len(row['attrs']), 'duplicate algo attribute')
        for attr in row['attrs']:
            require(isinstance(attr['name'], str) and type(attr['status']) is int and
                    type(attr['value']) is int, 'bad algorithm attribute')
        algos[phase] = {k: row[k] for k in ('key', 'raw_hex', 'attrs', 'cache_before', 'cache_after')}
    require(algos['gemm_first']['cache_before'] in (False, 0) and
            algos['gemm_first']['cache_after'] in (True, 1) and
            algos['gemm_cached']['cache_before'] in (True, 1) and
            algos['gemm_cached']['cache_after'] in (True, 1), 'first/cache path not exercised')
    for key in ('key', 'raw_hex', 'attrs'):
        require(algos['gemm_first'][key] == algos['gemm_cached'][key], 'cached algorithm differs')
    summary = one(groups['summary'], 'summary')
    require(summary.get('mode') == mode, 'summary mode differs')
    for field in ('drops', 'overflow', 'errors'):
        require(integer(summary.get(field), field) == 0, 'collector ' + field)
    api = groups['api']
    activity = groups['activity']
    counts = dict(api_enter_count=sum(r.get('site') == 'enter' for r in api),
                  api_exit_count=sum(r.get('site') == 'exit' for r in api),
                  activity_count=len(activity), record_count=len(api) + len(activity))
    for field, value in counts.items():
        require(summary.get(field) == value, 'summary actual count differs: ' + field)
    require(summary.get('collector_limit_bytes') == MAX_LOG, 'collector bound differs')
    require(summary.get('buffers_outstanding') == 0, 'activity buffers not returned')
    require(integer(summary.get('collector_owned_bytes'), 'collector_owned_bytes') +
            integer(summary.get('configured_cupti_buffer_bytes'), 'configured_cupti_buffer_bytes') <= MAX_LOG,
            'explicit collector allocation budget exceeded')
    require(summary.get('subscriber_used') is (mode == 'cupti') and
            summary.get('flushed') is (mode == 'cupti'), 'subscriber/flush mode differs')
    if mode == 'cupti':
        require(integer(summary.get('buffer_requests'), 'buffer_requests', 1) ==
                integer(summary.get('buffer_completions'), 'buffer_completions', 1),
                'invalid activity buffer completion counts')
        seqs = [integer(r.get('seq'), 'record seq', 1) for r in events
                if event_name(r) in ('api', 'activity')]
        require(seqs == list(range(1, len(seqs) + 1)), 'record seq duplicate/gap/reorder')
    else:
        require(not api and not activity, 'Nsight process contains independent CUPTI subscriber records')
    return dict(config=config, pid=pid, tid=tid, ready=ready, exit=end, groups=groups,
                algorithms=algos, summary=summary, counts=counts)


def initial_result(kind):
    return dict(schema=1, kind=kind, status='INVALID', eligible_nsys=False,
                local_mechanism_valid=False, performance_acceptance=False,
                model_qualification=False, whole_trace_completeness_claimed=False,
                errors=[], limitations=list(LIMITS))


def _cupti(log, result):
    shared = common(log, 'cupti')
    result.update(config=shared['config'], target_pid=shared['pid'],
                  algorithms=shared['algorithms'], libraries=shared['groups']['library'],
                  collector_summary=shared['summary'],
                  actual_counts=shared['counts'])
    pairs, stacks, enter_seqs = {}, defaultdict(list), set()
    by_correlation = defaultdict(list)
    parameter_fields = ('domain', 'name', 'pid', 'tid', 'correlation_id', 'enter_seq', 'phase',
                        'parent_runtime_correlation_id', 'context_uid', 'is_memset',
                        'count', 'dev_ptr', 'value', 'stream', 'cbid')
    for row in shared['groups']['api']:
        require(row.get('domain') in ('runtime', 'driver') and row.get('site') in ('enter', 'exit'),
                'invalid API domain/site')
        require(row.get('pid') == shared['pid'], 'callback process identity differs')
        tid = integer(row.get('tid'), 'callback tid', 1)
        corr = integer(row.get('correlation_id'), 'callback correlation', 1)
        entry = integer(row.get('enter_seq'), 'callback enter_seq', 1)
        integer(row.get('cbid'), 'callback cbid', 1)
        # correlationId relates activity to an API; it is not the identity of
        # every callback invocation. The producer stores its unique ENTER seq
        # in CUPTI correlationData and recovers that exact seq at EXIT.
        key = (row['domain'], corr, entry)
        integer(row.get('count'), 'memset count')
        require(type(row.get('is_memset')) is bool, 'missing is_memset field')
        require(isinstance(row.get('name'), str) and row['name'], 'missing callback name')
        stack = stacks[tid]
        if row['site'] == 'enter':
            require(key not in pairs and row['enter_seq'] == row['seq'] and
                    row['enter_seq'] not in enter_seqs and row['retval'] == -1,
                    'duplicate/bad callback ENTER')
            enter_seqs.add(row['enter_seq'])
            parent = next((p for p in reversed(stack) if p[0] == 'runtime'), None)
            require(row['parent_runtime_correlation_id'] == (parent[1] if parent else 0),
                    'callback runtime bridge does not match thread nesting')
            pairs[key] = dict(enter=row, depth=len(stack), parent=parent, activities=[])
            by_correlation[(row['domain'], corr)].append(key)
            stack.append(key)
        else:
            require(key in pairs and 'exit' not in pairs[key] and stack and stack[-1] == key,
                    'missing/reordered/duplicate callback EXIT')
            start = pairs[key]['enter']
            require(all(row.get(k) == start.get(k) for k in parameter_fields),
                    'ENTER/EXIT parameters or identity differ')
            require(type(row.get('retval')) is int, 'missing callback return value')
            pairs[key]['exit'] = row
            stack.pop()
    require(pairs and all(not stack for stack in stacks.values()) and
            all('exit' in pair for pair in pairs.values()), 'incomplete callbacks')
    shared_correlations = []
    for (domain, corr), keys in by_correlation.items():
        if len(keys) == 1:
            continue
        members = [pairs[key] for key in keys]
        parent = members[0]['parent']
        # Only the observed, provable case is admitted: sequential driver
        # invocations inside one exact runtime parent, sharing its correlation.
        # Cross-parent/thread reuse and overlapping callbacks remain invalid.
        require(domain == 'driver' and parent is not None and parent[1] == corr and
                all(p['parent'] == parent and
                    p['enter']['tid'] == pairs[parent]['enter']['tid'] and
                    pairs[parent]['enter']['seq'] < p['enter']['seq'] <
                    p['exit']['seq'] < pairs[parent]['exit']['seq']
                    for p in members) and
                all(a['exit']['seq'] < b['enter']['seq']
                    for a, b in zip(members, members[1:])),
                'correlation reuse is not sequential drivers inside one runtime parent')
        shared_correlations.append(dict(domain=domain, correlation_id=corr,
                                        parent_enter_seq=parent[2],
                                        enter_seqs=[key[2] for key in keys]))
    activity_api, gpu = {}, []
    os_to_cupti, cupti_to_os, thread_evidence = {}, {}, Counter()
    activity_candidates = defaultdict(list)
    for key, pair in pairs.items():
        row = pair['enter']
        activity_candidates[(key[0], key[1], row['cbid'], pair['exit']['retval'])].append(key)
    for row in shared['groups']['activity']:
        require(row.get('kind') in ('runtime', 'driver', 'kernel', 'copy', 'memset'),
                'unknown activity kind')
        require(row.get('process_id') == shared['pid'], 'activity process identity differs')
        start, end = integer(row.get('start'), 'activity start', 1), integer(row.get('end'), 'activity end', 1)
        require(end >= start, 'negative activity interval')
        corr = integer(row.get('correlation_id'), 'activity correlation', 1)
        if row['kind'] in ('runtime', 'driver'):
            cbid = integer(row.get('cbid'), 'activity cbid', 1)
            require(type(row.get('return_value')) is int, 'missing activity return value')
            candidates = activity_candidates[(row['kind'], corr, cbid, row['return_value'])]
            require(len(candidates) == 1, 'API activity has no unique callback identity')
            key = candidates[0]
            require(key not in activity_api, 'duplicate API activity for callback')
            require(isinstance(row.get('name'), str) and bool(row['name']),
                    'missing API activity name')
            os_tid = pairs[key]['enter']['tid']
            cupti_tid = integer(row.get('thread_id'), 'activity thread id', 1)
            # Installed cupti_activity.h:11166-11182 documents Linux DEFAULT
            # as pthread_self(), SYSTEM as SYS_gettid. This probe records the
            # latter in callbacks but leaves activity mode DEFAULT. Infer a
            # bijection only from the unique PID/domain/corr/cbid/return join;
            # never use a guessed thread map to resolve an ambiguous API join.
            require(os_to_cupti.get(os_tid, cupti_tid) == cupti_tid and
                    cupti_to_os.get(cupti_tid, os_tid) == os_tid,
                    'OS/CUPTI thread correspondence is not bijective')
            os_to_cupti[os_tid], cupti_to_os[cupti_tid] = cupti_tid, os_tid
            thread_evidence[os_tid] += 1
            activity_api[key] = row
        else:
            gpu.append(row)
    require(activity_api, 'no API activities')
    activity_by_corr = defaultdict(list)
    for key in activity_api:
        activity_by_corr[key[1]].append(key)
    for row in gpu:
        keys = activity_by_corr[row['correlation_id']]
        require(len(keys) == 1, 'GPU activity has no unique process-local API correlation')
        key = keys[0]
        pair = pairs[key]
        require(pair['exit']['retval'] == 0, 'activity linked to unsuccessful API')
        require(row['start'] >= activity_api[key]['start'], 'GPU activity precedes owner API')
        pair['activities'].append(row)
        parent_key = pair['parent'] if key[0] == 'driver' else None
        parent = parent_key[1] if parent_key else 0
        if parent_key:
            require(parent_key in pairs and pairs[parent_key]['enter']['tid'] == pair['enter']['tid'],
                    'missing same-thread parent runtime API')
            pairs[parent_key]['activities'].append(row)
        runtime_corr = row.get('runtime_correlation_id')
        if row['kind'] == 'copy' and runtime_corr not in (None, 0):
            require(runtime_corr == (parent if parent else key[1]) and
                    len(by_correlation[('runtime', runtime_corr)]) == 1,
                    'copy runtime correlation bridge differs')
    memsets = []
    for key, pair in pairs.items():
        row = pair['enter']
        if row['domain'] != 'runtime' or not row['name'].startswith('cudaMemsetAsync'):
            continue
        require(row['is_memset'] and pair['exit']['retval'] == 0 and key in activity_api,
                'memset params/return/API activity missing')
        integer(row['dev_ptr'], 'memset pointer', 1)
        integer(row['stream'], 'memset stream')
        count = row['count']
        acts = pair['activities']
        require(all(a['kind'] == 'memset' for a in acts), 'memset associated with non-memset activity')
        if count:
            require(acts and sum(integer(a.get('bytes'), 'activity bytes') for a in acts) == count,
                    'nonzero memset has missing/different GPU bytes')
            require(all(a.get('value') == row['value'] for a in acts), 'memset GPU value differs')
        else:
            require(all(a.get('bytes') == 0 for a in acts), 'zero-count memset has nonzero GPU bytes')
        memsets.append(dict(phase=row['phase'], domain=row['domain'], correlation_id=key[1],
                            enter_seq=row['enter_seq'], exit_seq=pair['exit']['seq'], tid=row['tid'],
                            count=count, value=row['value'], dev_ptr=row['dev_ptr'], stream=row['stream'],
                            gpu_activity_count=len(acts), gpu_bytes=sum(a['bytes'] for a in acts),
                            classification=('EXPLICIT_ZERO_NO_ACTIVITY' if not acts else 'ACTIVITY_OBSERVED'),
                            pair_depth=pair['depth']))
    controls = {}
    for phase, count in (('control_zero', 0), ('control_nonzero', 512)):
        item = one([r for r in memsets if r['phase'] == phase], phase + ' callback')
        require(item['count'] == count and item['value'] == 0x5A, 'fixed control arguments differ')
        controls[phase] = item
    require(controls['control_zero']['dev_ptr'] == controls['control_nonzero']['dev_ptr'] and
            controls['control_zero']['stream'] == controls['control_nonzero']['stream'],
            'controls do not share the fixed buffer/stream')
    readbacks = {}
    for phase in PHASES:
        readback = one([p for p in pairs.values() if p['enter']['domain'] == 'runtime' and
                        api_name(p['enter']['name']) == 'cudaMemcpy' and p['enter']['phase'] == phase],
                       phase + ' explicit readback API')
        copies = readback['activities']
        expected_bytes = 512 if phase in PHASES[:2] else 384
        require(readback['exit']['retval'] == 0 and len(copies) == 1 and
                copies[0]['kind'] == 'copy' and copies[0]['bytes'] == expected_bytes,
                phase + ': known readback activity missing/different')
        readbacks[phase] = dict(correlation_id=readback['enter']['correlation_id'], bytes=expected_bytes)
    phases, reproduced = {}, True
    for phase in GEMMS:
        internal = [r for r in memsets if r['phase'] == phase]
        phase_kernels = [a for p in pairs.values() if p['enter']['phase'] == phase
                         for a in p['activities'] if a['kind'] == 'kernel']
        # Parent/child ownership can refer to the same activity; retain one raw seq.
        phase_kernels = list({a['seq']: a for a in phase_kernels}.values())
        adjacent = []
        for memset in internal:
            following = sorted((p for p in pairs.values()
                                if p['enter']['tid'] == memset['tid'] and
                                p['enter']['phase'] == phase and p['depth'] == memset['pair_depth'] and
                                p['enter']['seq'] > memset['exit_seq']),
                               key=lambda p: p['enter']['seq'])
            if following:
                nxt = following[0]
                kernels = [a['name'] for a in nxt['activities'] if a['kind'] == 'kernel']
                adjacent.append(dict(memset_correlation_id=memset['correlation_id'],
                                     next_api=nxt['enter']['name'],
                                     next_correlation_id=nxt['enter']['correlation_id'],
                                     kernel_names=kernels,
                                     representative_family=any(FAMILY in n for n in kernels)))
        matched = bool(internal and adjacent and all(r['representative_family'] for r in adjacent)
                       and len(adjacent) == len(internal))
        reproduced &= matched
        phases[phase] = dict(memsets=internal, kernel_names=[a['name'] for a in phase_kernels],
                             adjacent_launches=adjacent, representative_reproduced=matched)
    result.update(controls=controls, readbacks=readbacks, gemm_phases=phases, memset_calls=memsets,
                  callback_pairing=dict(identity='PID/TID/domain/enter_seq with CBID and strict nesting',
                                        pairs=len(pairs), shared_driver_correlations=shared_correlations),
                  thread_correspondence=dict(method='INFERRED_FROM_UNIQUE_API_IDENTITIES',
                                             directly_sampled_pthread=False,
                                             mappings=[dict(os_tid=t, cupti_tid=c,
                                                            unique_api_matches=thread_evidence[t])
                                                       for t, c in sorted(os_to_cupti.items())]),
                  api_activity_count=len(activity_api), gpu_activity_counts=dict(Counter(a['kind'] for a in gpu)),
                  api_name_aliases=[dict(domain=k[0], cbid=a['cbid'], callback=pairs[k]['enter']['name'],
                                         activity=a['name']) for k, a in activity_api.items()
                                    if a['name'] != pairs[k]['enter']['name']],
                  zero_count_calls=sum(r['count'] == 0 for r in memsets),
                  nonzero_count_calls=sum(r['count'] > 0 for r in memsets),
                  representative_family=FAMILY, local_mechanism_valid=True,
                  status='VALID_AND_REPRODUCED' if reproduced else 'NOT_REPRODUCED',
                  eligible_nsys=bool(reproduced))


def analyze_cupti(log_path):
    result = initial_result('CUPTI_ONLY_FIXED_PARAMETER_OBSERVATION')
    try:
        log = read_log(log_path)
        result['source'] = log['source']
        _cupti(log, result)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, sqlite3.Error) as exc:
        result['errors'].append(type(exc).__name__ + ': ' + str(exc))
    return result


def _nsys(sqlite_path, log, cupti_result, result):
    require(cupti_result.get('eligible_nsys') is True and
            cupti_result.get('status') == 'VALID_AND_REPRODUCED' and not cupti_result.get('errors'),
            'CUPTI-only process was not eligible')
    shared = common(log, 'nsys')
    pid = shared['pid']
    result.update(config=shared['config'], target_pid=pid, algorithms=shared['algorithms'],
                  libraries=shared['groups']['library'],
                  cupti_reference=dict(source=cupti_result.get('source'), target_pid=cupti_result.get('target_pid')))
    # Equal numeric PIDs can be reused after exit. Never use PID equality as a cross-run join.
    algorithm_equal = True
    for phase in GEMMS:
        for field in ('key', 'raw_hex', 'attrs'):
            algorithm_equal &= shared['algorithms'][phase][field] == cupti_result['algorithms'][phase][field]
    path = Path(sqlite_path).absolute()
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode), 'SQLite not regular/non-symlink')
    before = digest(path)
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({'StringIds', 'CUPTI_ACTIVITY_KIND_RUNTIME', 'CUPTI_ACTIVITY_KIND_KERNEL',
                 'CUPTI_ACTIVITY_KIND_MEMCPY', 'NVTX_EVENTS', 'DIAGNOSTIC_EVENT'} <= tables,
                'missing required SQLite table')
        schema = {t: [dict(r) for r in db.execute('PRAGMA table_info("' + t.replace('"', '""') + '")')]
                  for t in sorted(tables)}
        consumed = 0

        def rows(table):
            nonlocal consumed
            answer = []
            for row in db.execute('SELECT rowid AS source_row,* FROM "' + table + '"'):
                consumed += 1
                require(consumed <= MAX_SQL_ROWS, 'SQLite row bound exceeded')
                answer.append(dict(row))
            return answer

        names = {r['id']: r['value'] for r in rows('StringIds')}
        require(len(names) == db.execute('SELECT COUNT(*) FROM StringIds').fetchone()[0],
                'duplicate StringIds')
        metadata = rows('META_DATA_EXPORT') if 'META_DATA_EXPORT' in tables else []
        ranges = {}
        for row in rows('NVTX_EVENTS'):
            text = row.get('text') or names.get(row.get('textId'), '')
            if not text.startswith('mechanism|'):
                continue
            require(((row['globalTid'] >> 24) & 0xFFFFFF) == pid and
                    (row['globalTid'] & 0xFFFFFF) == shared['tid'], 'NVTX identity differs')
            require(text not in ranges and row.get('end') is not None and row['end'] > row['start'] and
                    row.get('endGlobalTid') in (None, row['globalTid']), 'duplicate/unclosed mechanism NVTX')
            ranges[text] = row
        require(set(ranges) == {'mechanism|root'} | {'mechanism|phase=' + p for p in PHASES},
                'missing/unexpected mechanism NVTX labels')
        root = ranges['mechanism|root']
        previous = root['start']
        for phase in PHASES:
            row = ranges['mechanism|phase=' + phase]
            require(previous <= row['start'] < row['end'] <= root['end'], 'phase NVTX order/containment')
            previous = row['end']
        apis = {}
        for row in rows('CUPTI_ACTIVITY_KIND_RUNTIME'):
            if ((row['globalTid'] >> 24) & 0xFFFFFF) != pid or row.get('callchainId') is not None:
                continue
            require(row.get('eventClass') in (0, 1), 'unknown/missing SQLite API eventClass')
            corr = integer(row['correlationId'], 'SQLite correlation', 1)
            require(corr not in apis, 'ambiguous same-process API correlation')
            require(row['end'] >= row['start'], 'invalid API interval')
            row['name'] = names[row['nameId']]
            row['phase'] = None
            for phase in PHASES:
                window = ranges['mechanism|phase=' + phase]
                if (row['globalTid'] == window['globalTid'] and
                        window['start'] <= row['start'] and row['end'] <= window['end']):
                    require(row['phase'] is None, 'ambiguous API phase')
                    row['phase'] = phase
            row['gpu'] = []
            apis[corr] = row
        gpu_counts = Counter()
        for kind, table in (('kernel', 'CUPTI_ACTIVITY_KIND_KERNEL'), ('copy', 'CUPTI_ACTIVITY_KIND_MEMCPY'),
                            ('memset', 'CUPTI_ACTIVITY_KIND_MEMSET')):
            if table not in tables:
                continue
            for row in rows(table):
                if ((row['globalPid'] >> 24) & 0xFFFFFF) != pid:
                    continue
                corr = row['correlationId']
                require(corr in apis, 'orphan Nsight activity')
                require(apis[corr]['returnValue'] == 0 and row['end'] >= row['start'] >= apis[corr]['start'],
                        'bad activity/owner interval or return')
                row['kind'] = kind
                if kind == 'kernel':
                    row['names'] = {k: names.get(row.get(k)) for k in ('mangledName', 'demangledName', 'shortName')
                                    if row.get(k) is not None}
                    require(row['names'], 'kernel has no exported name')
                apis[corr]['gpu'].append(row)
                gpu_counts[kind] += 1
        phase_results = {}
        for phase in PHASES:
            own = sorted([a for a in apis.values() if a['phase'] == phase], key=lambda a: (a['start'], a['source_row']))
            memset = [a for a in own if a['name'].startswith('cudaMemsetAsync')]
            expected_count = 1 if phase in PHASES[:2] else len(cupti_result['gemm_phases'][phase]['memsets'])
            require(len(memset) == expected_count and all(a['returnValue'] == 0 for a in memset),
                    'Nsight phase memset API count/return differs: ' + phase)
            kernels = [g for a in own for g in a['gpu'] if g['kind'] == 'kernel']
            readback = one([a for a in own if api_name(a['name']) == 'cudaMemcpy'], phase + ' Nsight readback')
            require(readback['returnValue'] == 0 and len(readback['gpu']) == 1 and
                    readback['gpu'][0]['kind'] == 'copy' and
                    readback['gpu'][0]['bytes'] == (512 if phase in PHASES[:2] else 384),
                    'Nsight known readback activity missing/different: ' + phase)
            memory = [dict(correlation_id=a['correlationId'], return_value=a['returnValue'],
                           parameters='UNAVAILABLE_IN_NSYS_SQLITE',
                           activities=[{k: g.get(k) for k in ('kind', 'bytes', 'value', 'start', 'end', 'source_row')}
                                       for g in a['gpu']]) for a in memset]
            if phase == 'control_nonzero':
                observed = [g for a in memset for g in a['gpu']]
                require(len(observed) == 1 and observed[0]['kind'] == 'memset' and
                        observed[0]['bytes'] == 512 and observed[0]['value'] == 0x5A,
                        'Nsight known nonzero control activity missing/different')
            if phase == 'control_zero':
                require(all(g.get('bytes') == 0 for a in memset for g in a['gpu']),
                        'Nsight zero control has nonzero activity')
            adjacent = []
            for call in memset:
                following = [a for a in own if a['start'] >= call['end'] and a['source_row'] != call['source_row']]
                next_api = following[0] if following else None
                adjacent.append(dict(memset_correlation_id=call['correlationId'],
                                     next_api=next_api['name'] if next_api else None,
                                     next_kernel_names=[g['names'] for g in next_api['gpu'] if g['kind'] == 'kernel']
                                     if next_api else []))
            representative = (phase not in GEMMS or
                              all(any(FAMILY in n for names_ in a['next_kernel_names']
                                      for n in names_.values() if n) for a in adjacent))
            phase_results[phase] = dict(memsets=memory, kernel_names=[g['names'] for g in kernels],
                                        adjacent_launches=adjacent, representative_reproduced=representative)
        diagnostics = rows('DIAGNOSTIC_EVENT')
        severity = rows('ENUM_DIAGNOSTIC_SEVERITY_LEVEL') if 'ENUM_DIAGNOSTIC_SEVERITY_LEVEL' in tables else []
        profiler_lines = [line for line in log['other_lines'] if
                          re.search(r'warning|error|incomplete|dropped|lost|overflow', line, re.I)]
        reproduced = algorithm_equal and all(phase_results[p]['representative_reproduced'] for p in GEMMS)
        result.update(phase_results=phase_results, api_count=len(apis), gpu_activity_counts=dict(gpu_counts),
                      schema_columns=schema, export_metadata=metadata, diagnostic_events=diagnostics,
                      diagnostic_severity_enum=severity, profiler_log_findings=profiler_lines,
                      warnings_disposition='RETAINED_LOCAL_MECHANISM_ONLY_NO_MODEL_WAIVER',
                      representative_family=FAMILY, local_mechanism_valid=True,
                      status='LOCAL_MECHANISM_OBSERVED' if reproduced else 'NOT_REPRODUCED',
                      cross_process_comparison=dict(config_and_algorithm_equal=algorithm_equal,
                                                    correlation_ids_joined=False,
                                                    cupti_explicit_gemm_counts={p: [r['count'] for r in
                                                        cupti_result['gemm_phases'][p]['memsets']] for p in GEMMS},
                                                    nsys_gemm_counts='PARAMETERS_UNAVAILABLE; no per-call substitution from CUPTI',
                                                    exact_kernel_name_equivalence='RAW_NAMES_RETAINED_NOT_ASSUMED'))
    finally:
        db.close()
        after = digest(path)
        result['sqlite_source'] = dict(path=str(path), sha256_before=before, sha256_after=after)
        require(before == after, 'SQLite changed during read-only analysis')


def analyze_nsys(sqlite_path, log_path, cupti_result):
    result = initial_result('NSYS_SEPARATE_PROCESS_LOCAL_MECHANISM_COMPARISON')
    try:
        log = read_log(log_path)
        result['source'] = log['source']
        _nsys(sqlite_path, log, cupti_result, result)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, sqlite3.Error) as exc:
        result['status'] = 'INVALID'
        result['local_mechanism_valid'] = False
        result['errors'].append(type(exc).__name__ + ': ' + str(exc))
    return result
