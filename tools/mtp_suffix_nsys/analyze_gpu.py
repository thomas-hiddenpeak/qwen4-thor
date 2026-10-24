#!/usr/bin/env python3
"""Offline CPU/NVTX/CUDA audit; never collects, exports, or invokes CUDA.

SQLite is opened immutable/read-only. Derived joins use a private disk-backed
SQLite database beneath --scratch-dir and are removed on completion/failure.
This audits recorded trace structure, not the whole stage or HTTP acceptance.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / 'tools/mtp_suffix_execution/analyze_trace.py'
LEGACY_SHA = '42d611a491536ea3e7703755920906f4c142098a0864c6c7a41fe0b6c92e8f39'
MASK = 0xFFFFFFFFFF000000
LOW = 0xFFFFFF
KINDS = {'kernel': 'CUPTI_ACTIVITY_KIND_KERNEL',
         'copy': 'CUPTI_ACTIVITY_KIND_MEMCPY',
         'memset': 'CUPTI_ACTIVITY_KIND_MEMSET'}
MAX_ROWS = 40_000_000
MAX_EXAMPLES = 32
WRAPPER_WARNINGS = {
    'Not all NVTX events might have been collected.',
    'No NVTX events collected. Does the process use NVTX?',
    'CUDA profiling might have not been started correctly.',
    'No CUDA events collected. Does the process use CUDA?',
}
LIMITS = [
    'Trace structure PASS does not verify the stage lease, HTTP, model, or output contract.',
    'Unresolved profiler warnings remain fatal even when recorded correlation joins close.',
    'Both API and activity may be wholly absent; bidirectional joins cannot detect every loss.',
    'CPU wall and thread CPU are paired by identity only, never offset-aligned to Nsight.',
    'Thread CPU includes driver/submission/busy wait; signed wall minus CPU is not GPU time.',
    'Nsight NVTX and API intervals share one trace clock. API wall is not pure submission CPU.',
    'GPU activity belongs to the API owner even if execution occurs after its host span ends.',
    'Phase GPU unions and CPU/API/GPU durations overlap and must not be added as elapsed time.',
    'Return-to-kernel lag is signed; a negative value means execution started before API return.',
    'First/later generation groups use server round ordinal, not exact client TTFT slices.',
    'Unowned application activity remains separate; ordinary tail work is not assigned to MTP.',
    'Missing activity tables or uncorrelated successful memory submissions are not filled with zero.',
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def legacy_module():
    require(sha(LEGACY) == LEGACY_SHA, 'legacy parser identity changed')
    spec = importlib.util.spec_from_file_location('q4t_sealed_trace_parser', LEGACY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table_names(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(db, table):
    return {r['name'] for r in db.execute('PRAGMA table_info("' + table + '")')}


def rows(db, table, suffix=''):
    return db.execute('SELECT rowid AS source_row,* FROM "' + table + '" ' + suffix)


def api_category(name):
    if re.match(r'^(?:cuda|cu)(?:Launch.*Kernel|GraphLaunch)', name):
        return 'launch'
    if re.match(r'^(?:cuda|cu)Memcpy', name):
        return 'copy'
    if re.match(r'^(?:cuda|cu)Memset', name):
        return 'memset'
    if 'Synchronize' in name:
        return 'host_sync'
    if 'StreamWaitEvent' in name:
        return 'device_dependency'
    if 'Query' in name:
        return 'query'
    if any(s in name for s in ('Alloc', 'Malloc', 'Free')):
        return 'allocation'
    return 'other'


class IntervalSum:
    """Streaming union, requiring increasing start for each accumulator."""
    def __init__(self):
        self.count = self.total = self.union = 0
        self.first = self.last = self.previous = None

    def add(self, begin, end):
        require(end >= begin, 'negative time interval')
        require(self.previous is None or begin >= self.previous, 'unsorted interval input')
        self.previous = begin
        self.count += 1
        self.total += end - begin
        if self.first is None:
            self.first = begin
        self.union += max(0, end - max(begin, self.last if self.last is not None else begin))
        self.last = max(end, self.last if self.last is not None else end)

    def result(self):
        hull = 0 if self.first is None else self.last - self.first
        return dict(count=self.count, sum_ns=self.total, union_ns=self.union,
                    first_start_ns=self.first, last_end_ns=self.last,
                    interval_hull_ns=hull, internal_activity_gap_ns=hull - self.union)


class OwnerIndex:
    def __init__(self, nvtx):
        self.by_tid = defaultdict(list)
        for key, row in nvtx.items():
            self.by_tid[row['global_tid']].append((key, row))
        for ranges in self.by_tid.values():
            ranges.sort(key=lambda item: item[1]['start'])
        self.state = {}

    def owner(self, tid, begin, end):
        cursor, active, previous = self.state.get(tid, (0, [], None))
        require(previous is None or begin >= previous, 'API order changed within thread')
        ranges = self.by_tid[tid]
        while cursor < len(ranges) and ranges[cursor][1]['start'] <= begin:
            active.append(ranges[cursor])
            cursor += 1
        active = [(key, r) for key, r in active if r['end'] >= begin]
        self.state[tid] = (cursor, active, begin)
        candidates = [(key, r) for key, r in active if r['end'] >= end]
        if not candidates:
            return None
        depth = max(r['depth'] for _, r in candidates)
        deepest = [key for key, r in candidates if r['depth'] == depth]
        require(len(deepest) == 1, 'ambiguous deepest NVTX owner')
        return deepest[0]


def fixture_nvtx(db, names, pid):
    found, stack_by_tid = {}, defaultdict(list)
    for row in rows(db, 'NVTX_EVENTS', 'ORDER BY globalTid,start,end DESC'):
        name = names.get(row['textId'], row['text'] or '')
        if not name.startswith('k3fixture|'):
            continue
        require(((row['globalTid'] >> 24) & LOW) == pid, 'fixture NVTX pid differs')
        require(row['eventType'] in (59, 70) and row['end'] is not None and
                row['end'] >= row['start'] and
                row['endGlobalTid'] in (None, row['globalTid']), 'invalid fixture NVTX')
        require(name not in found, 'duplicate fixture NVTX label')
        stack = stack_by_tid[row['globalTid']]
        while stack and stack[-1]['end'] <= row['start']:
            stack.pop()
        require(not stack or row['end'] <= stack[-1]['end'], 'fixture NVTX overlap')
        item = dict(start=row['start'], end=row['end'], global_tid=row['globalTid'],
                    depth=len(stack), label=name)
        found[name] = item
        stack.append(item)
    require(found, 'no fixture NVTX ranges')
    return found


def helpers(db, tables, names, nvtx):
    """Names alone do not exempt any API: require an exact flush container."""
    if not {'ThreadNames', 'PROFILER_OVERHEAD'} <= tables:
        return {}, []
    app_tids = {r['global_tid'] for r in nvtx.values()}
    # Include other NVTX labels too: a thread with any application NVTX work
    # cannot become a profiler-only thread merely because its label is outside
    # this observer's naming grammar.
    app_tids.update(r['globalTid'] for r in rows(db, 'NVTX_EVENTS')
                    if r['globalTid'] is not None)
    helper_tids = {r['globalTid'] for r in rows(db, 'ThreadNames')
                   if names.get(r['nameId']) == 'CUPTI worker thread'} - app_tids
    flushes = defaultdict(list)
    proof = []
    for row in rows(db, 'PROFILER_OVERHEAD'):
        if (row['globalTid'] in helper_tids and row['overheadType'] == 3 and
                names.get(row['nameId']) == 'CUPTI Buffer flush'):
            require(row['end'] >= row['start'], 'invalid CUPTI flush interval')
            item = dict(row)
            item['name'] = 'CUPTI Buffer flush'
            proof.append(item)
            flushes[row['globalTid']].append((row['start'], row['end'], row['source_row']))
    return flushes, proof


def role_diagnostics(helper, db, tables, server_text, export_text, wrapper_pid):
    result = helper.diagnostic_audit(db, tables, server_text, export_text)
    allowed, wrapper_counts = [], {}
    if wrapper_pid is not None:
        require(isinstance(wrapper_pid, int) and 0 < wrapper_pid <= LOW,
                'invalid wrapper PID')
        for table in sorted(tables):
            if not (table.startswith('CUPTI_ACTIVITY_KIND_') or table == 'NVTX_EVENTS'):
                continue
            cols = columns(db, table)
            identity = 'globalPid' if 'globalPid' in cols else ('globalTid' if 'globalTid' in cols else None)
            if identity:
                wrapper_counts[table] = db.execute(
                    'SELECT count(*) FROM "' + table + '" WHERE ((' + identity +
                    ' >> 24) & 16777215)=?', (wrapper_pid,)).fetchone()[0]
        bundle = [r for r in result['messages'] if r.get('globalPid') is not None and
                  ((r['globalPid'] >> 24) & LOW) == wrapper_pid and
                  r['severity_name'] == 'Warning']
        texts = Counter(r['text'].strip() for r in bundle)
        if set(texts) <= WRAPPER_WARNINGS and all(n == 1 for n in texts.values()):
            if wrapper_counts and not any(wrapper_counts.values()):
                allowed = bundle
        if not wrapper_counts or any(wrapper_counts.values()):
            result['errors'].append(dict(kind='wrapper_zero_activity_contract_failed',
                                         wrapper_pid=wrapper_pid, counts=wrapper_counts))
    def is_allowed(row):
        return any(row == accepted for accepted in allowed)
    result['errors'] = [r for r in result['errors'] if not is_allowed(r)]
    result['warnings_require_review'] = [r for r in result['warnings_require_review'] if not is_allowed(r)]
    # The legacy parser reports unsupported Unified Memory diagnostics as an
    # auxiliary feature. This stage nevertheless rejects every unapproved
    # Warning, including any such auxiliary message from the actual target.
    for row in result['messages']:
        if (row['severity_name'] not in ('Info', 'Warning', 'Error') and
                row not in result['errors']):
            result['errors'].append(row)
        if (row['severity_name'] == 'Warning' and not is_allowed(row) and
                row not in result['warnings_require_review']):
            result['warnings_require_review'].append(row)
    result['allowed_wrapper_warnings'] = allowed
    result['wrapper_zero_activity_counts'] = wrapper_counts
    result['wrapper_pid'] = wrapper_pid
    result['valid'] = (not result['errors'] and not result['warnings_require_review'] and
                       not result['profiler_log_findings'])
    return result


def phase_groups(spans, batches):
    rounds = sorted((r['wall_begin_ns'], key[0]) for key, r in spans.items()
                    if r['phase'] == 'mtp.step')
    ordinal = {batch: i for i, (_, batch) in enumerate(rounds)}
    groups = {}
    for key, row in spans.items():
        chain, cursor = [], key
        while True:
            ancestor = spans[cursor]
            chain.append(ancestor)
            if ancestor['parent'] < 0:
                break
            cursor = (key[0], ancestor['parent'])
        labels = {r['phase'] for r in chain}
        if key[0] in ordinal:
            stage = 'generation_first_round' if ordinal[key[0]] == 0 else 'generation_later_rounds'
            token = next((r for r in chain if r['phase'] == 'mtp.draft_token'), None)
            step = next(r for r in chain if r['phase'] == 'mtp.step')
            if token:
                role = 'proposal_' + str(token['p'] - step['p'] + 1)
            elif 'mtp.accepted_extend' in labels:
                role = 'accepted_extend'
            elif 'mtp.verify_forward' in labels:
                role = 'target_verify'
            else:
                role = 'step_other'
        elif 'prompt_draft_extend_call' in labels or 'mtp.prompt_extend.total' in labels:
            stage, role = 'prompt_draft_extend', 'forward'
        elif 'target_prefill_call' in labels:
            stage, role = 'target_prefill', 'chunk'
        else:
            stage, role = 'prefill_initialization', batches[key[0]]['label']
        # Runtime request ids are excluded from cross-run group labels.
        if role.startswith('chatcmpl-'):
            role = 'handler_other'
        groups[key] = '/'.join((stage, role, 'T=' + str(row['t']), row['phase']))
    return groups


def build_api_table(raw, work, tables, names, nvtx, groups, flushes):
    work.execute('CREATE TABLE api(aid INTEGER PRIMARY KEY,source TEXT,source_row INT,'
                 'start INT,end INT,tid INT,gpid INT,corr INT,name TEXT,code INT,'
                 'owner TEXT,phase_group TEXT,helper INT,category TEXT,flush_row INT)')
    count, errors, proof = 0, [], []
    for table in ('CUPTI_ACTIVITY_KIND_RUNTIME', 'CUPTI_ACTIVITY_KIND_DRIVER'):
        if table not in tables:
            continue
        require({'start','end','globalTid','correlationId','nameId','returnValue'} <=
                columns(raw, table), 'unsupported CUDA API schema')
        index = OwnerIndex(nvtx)
        pending = []
        for source in rows(raw, table, 'ORDER BY globalTid,start'):
            row = dict(source)
            tid = row['globalTid']
            if tid is None or ((tid >> 24) & LOW) != work.target_pid or row.get('callchainId') is not None:
                continue
            require(row['end'] >= row['start'], 'negative CUDA API interval')
            name = names.get(row['nameId'])
            require(name is not None, 'CUDA API name missing')
            owner = index.owner(tid, row['start'], row['end'])
            containers = [f for f in flushes.get(tid, ())
                          if f[0] <= row['start'] <= row['end'] <= f[1]]
            helper = owner is None and bool(containers)
            flush_row = min(containers, key=lambda x:x[1]-x[0])[2] if helper else None
            category = api_category(name)
            pending_code = 34 if name.startswith('cuda') else 600
            expected_pending = category == 'query' and row['returnValue'] == pending_code
            if row['returnValue'] != 0 and not expected_pending:
                errors.append(dict(kind='api_return_error', source=table,
                    row=row['source_row'], name=name, code=row['returnValue']))
            group = groups[owner] if owner is not None else (
                'profiler_helper' if helper else 'unowned_application')
            count += 1
            require(count <= MAX_ROWS, 'API row budget exhausted')
            pending.append((count, table, row['source_row'], row['start'], row['end'],
                tid, tid & MASK, row['correlationId'], name, row['returnValue'],
                str(owner) if owner is not None else None, group, int(helper), category, flush_row))
            if helper and category in ('launch','copy','memset'):
                proof.append(dict(api_id=count, source=table, source_row=row['source_row'],
                    tid=tid, name=name, start=row['start'], end=row['end'], flush_row=flush_row))
            if len(pending) == 4096:
                work.executemany('INSERT INTO api VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', pending)
                pending.clear()
        work.executemany('INSERT INTO api VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', pending)
    require(count, 'no target CUDA API records')
    work.execute('CREATE INDEX api_corr ON api(gpid,corr)')
    return count, errors, proof


def build_gpu_table(raw, work, tables, names):
    work.execute('CREATE TABLE gpu(gid INTEGER PRIMARY KEY,source TEXT,source_row INT,'
        'kind TEXT,start INT,end INT,gpid INT,corr INT,device INT,context INT,stream INT,'
        'signature TEXT,bytes INT,aid INT)')
    count = 0
    absent = []
    for kind, table in KINDS.items():
        if table not in tables:
            absent.append(kind)
            continue
        require({'start','end','globalPid','correlationId','deviceId','contextId','streamId'} <=
                columns(raw, table), 'unsupported GPU schema: ' + table)
        pending = []
        for source in rows(raw, table):
            row = dict(source)
            if row['globalPid'] is None or ((row['globalPid'] >> 24) & LOW) != work.target_pid:
                continue
            require(row['end'] >= row['start'], 'negative GPU interval')
            count += 1
            require(count <= MAX_ROWS, 'GPU row budget exhausted')
            if kind == 'kernel':
                signature = {k:row.get(k) for k in ('gridX','gridY','gridZ','blockX','blockY','blockZ','staticSharedMemory','dynamicSharedMemory')}
                signature['name'] = names.get(row.get('demangledName'), names.get(row.get('shortName')))
                require(signature['name'] is not None, 'GPU kernel name missing')
            else:
                signature = {k:row.get(k) for k in ('copyKind','srcKind','dstKind','memKind','value')}
            pending.append((count, table, row['source_row'], kind, row['start'], row['end'],
                row['globalPid'], row['correlationId'], row['deviceId'],row['contextId'],
                row['streamId'],json.dumps(signature,sort_keys=True),row.get('bytes',0),None))
            if len(pending) == 4096:
                work.executemany('INSERT INTO gpu VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', pending)
                pending.clear()
        work.executemany('INSERT INTO gpu VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', pending)
    require(count, 'no target GPU execution records')
    work.execute('CREATE INDEX gpu_corr ON gpu(gpid,corr)')
    work.execute('UPDATE gpu SET aid=(SELECT min(a.aid) FROM api a WHERE '
        'a.gpid=gpu.gpid AND a.corr=gpu.corr HAVING count(*)=1)')
    work.execute('CREATE INDEX gpu_api ON gpu(aid)')
    return count, absent


def bounded_findings(db, query, category):
    count = 0
    examples = []
    for row in db.execute(query):
        count += 1
        if len(examples) < MAX_EXAMPLES:
            examples.append(dict(row))
    return dict(kind=category, count=count, examples=examples)


def coverage(work, mode):
    findings = []
    queries = [
        ('gpu_api_missing_or_ambiguous', 'SELECT gid,source,source_row,kind,corr FROM gpu WHERE aid IS NULL'),
        ('gpu_before_api', 'SELECT g.gid,g.source_row,g.start,a.start AS api_start FROM gpu g JOIN api a ON a.aid=g.aid WHERE g.start<a.start'),
        ('application_launch_without_kernel', "SELECT a.* FROM api a WHERE a.category='launch' AND a.code=0 AND a.helper=0 AND NOT EXISTS(SELECT 1 FROM gpu g WHERE g.aid=a.aid AND g.kind='kernel')"),
        ('application_memory_submit_without_activity', "SELECT a.* FROM api a WHERE a.category IN ('copy','memset') AND a.code=0 AND a.helper=0 AND NOT EXISTS(SELECT 1 FROM gpu g WHERE g.aid=a.aid)"),
    ]
    for kind, query in queries:
        item = bounded_findings(work, query, kind)
        if item['count']:
            item['status'] = ('UNKNOWN_CAPABILITY' if
                kind == 'application_memory_submit_without_activity' else 'FAIL')
            if item['status'] == 'UNKNOWN_CAPABILITY':
                item['reason'] = ('The exported API table has no transfer size or '
                    'arguments; absent activity may be a legal no-op or missing '
                    'trace. Neither is established. No exception or GPU comparison '
                    'is authorized by this result.')
                if mode == 'fixture':
                    item['reason'] += (' The caller must also bind the frozen '
                        'nonzero memory-operation ledger; known fixture semantics '
                        'do not establish which profiler capability failed.')
            findings.append(item)
    return findings


def summarize(work, nvtx, spans, groups):
    gpu_groups = defaultdict(IntervalSum)
    gpu_kinds = defaultdict(IntervalSum)
    gpu_streams = defaultdict(IntervalSum)
    gpu_group_streams = defaultdict(IntervalSum)
    overall = IntervalSum()
    application, profiler = IntervalSum(), IntervalSum()
    signatures = defaultdict(lambda:dict(count=0,sum_ns=0,bytes=0))
    lag = defaultdict(lambda:dict(count=0,sum_start_ns=0,sum_return_ns=0,
                                 negative_return_count=0,min_return_ns=None,max_return_ns=None))
    for r in work.execute('SELECT g.*,a.phase_group,a.start AS api_start,a.end AS api_end '
                          'FROM gpu g LEFT JOIN api a ON a.aid=g.aid ORDER BY g.start'):
        group = r['phase_group'] or 'unknown_correlation'
        begin, end = r['start'], r['end']
        overall.add(begin,end); gpu_groups[group].add(begin,end)
        if group == 'profiler_helper':
            profiler.add(begin,end)
        else:
            application.add(begin,end)
        gpu_kinds[r['kind']].add(begin,end)
        stream = f"{r['device']}/{r['context']}/{r['stream']}"
        gpu_streams[stream].add(begin,end)
        gpu_group_streams[group,stream].add(begin,end)
        signature = (group,r['kind'],r['signature'])
        signatures[signature]['count'] += 1
        signatures[signature]['sum_ns'] += end-begin
        signatures[signature]['bytes'] += r['bytes'] or 0
        if r['kind']=='kernel' and r['api_start'] is not None:
            x=lag[group]; value=begin-r['api_end']
            x['count']+=1; x['sum_start_ns']+=begin-r['api_start']; x['sum_return_ns']+=value
            x['negative_return_count']+=value<0
            x['min_return_ns']=value if x['min_return_ns'] is None else min(value,x['min_return_ns'])
            x['max_return_ns']=value if x['max_return_ns'] is None else max(value,x['max_return_ns'])
    api_by_thread = defaultdict(IntervalSum)
    api_functions = defaultdict(IntervalSum)
    for r in work.execute('SELECT * FROM api ORDER BY start'):
        api_by_thread[r['phase_group'],r['tid']].add(r['start'],r['end'])
        api_functions[r['phase_group'],r['category'],r['name'],r['tid']].add(r['start'],r['end'])
    cpu = defaultdict(Counter)
    nvtx_groups = defaultdict(IntervalSum)
    nvtx_exclusive = Counter()
    children = defaultdict(list)
    if spans:
        for key, s in spans.items():
            if s['parent'] >= 0:
                children[(key[0], s['parent'])].append(nvtx[key])
    for key,row in sorted(nvtx.items(),key=lambda kv:kv[1]['start']):
        group=groups[key]
        nvtx_groups[group].add(row['start'],row['end'])
        if spans:
            direct = sorted(children[key], key=lambda v:v['start'])
            child_union = IntervalSum()
            for child in direct:
                child_union.add(child['start'], child['end'])
            require(child_union.total == child_union.union, 'NVTX sibling overlap')
            nvtx_exclusive[group] += row['end'] - row['start'] - child_union.union
        if key in spans:
            s=spans[key]; c=cpu[group]; c['spans']+=1
            for clock in ('wall','cpu'):
                c[clock+'_inclusive_ns']+=s[clock+'_ns']
                c[clock+'_exclusive_ns']+=s[clock+'_exclusive_ns']
            c['nonrunning_exclusive_ns']+=s['non_running_wall_exclusive_ns']
    return dict(gpu_all=overall.result(),gpu_application=application.result(),
        gpu_profiler_helper=profiler.result(),
        gpu_by_kind={k:v.result() for k,v in gpu_kinds.items()},
        gpu_by_stream={k:v.result() for k,v in gpu_streams.items()},
        gpu_by_group_stream=[dict(group=g,stream=s,**v.result()) for (g,s),v in gpu_group_streams.items()],
        groups={key:dict(cpu=dict(cpu[key]) if key in cpu else None,
            nvtx_host=nvtx_groups[key].result() if key in nvtx_groups else None,
            nvtx_host_exclusive_ns=nvtx_exclusive.get(key) if spans else None,
            gpu=gpu_groups[key].result() if key in gpu_groups else None,
            launch_lag=lag.get(key)) for key in sorted(set(groups.values())|set(gpu_groups))},
        api_union_by_group_thread=[dict(group=g,tid=t,**v.result()) for (g,t),v in api_by_thread.items()],
        api_by_function=[dict(group=g,category=c,name=n,tid=t,**v.result()) for (g,c,n,t),v in api_functions.items()],
        kernel_and_memory_signatures=[dict(group=g,kind=k,signature=json.loads(s),**v) for (g,k,s),v in signatures.items()])


class WorkConnection(sqlite3.Connection):
    target_pid = None


def audit_database(sqlite_path, target_pid, *, server_text=None, export_text='',
                   scratch_dir, mode='model', wrapper_pid=None):
    """Return an audit dict; raise ValueError on malformed schema/contracts.

    This does not verify stage/HTTP artifacts. Callers must preserve failures and
    bind this result to the frozen matrix plus their independent stage audit.
    """
    require(mode in ('model','fixture'), 'invalid mode')
    require(isinstance(target_pid,int) and 0<target_pid<=LOW, 'unsupported target PID')
    require(wrapper_pid != target_pid, 'wrapper identity equals actual target')
    helper=legacy_module(); path=Path(sqlite_path).resolve(); before=sha(path)
    root=Path(scratch_dir).resolve(); root.mkdir(parents=True,exist_ok=True)
    require(not path.is_relative_to(root / 'derived'), 'source is inside scratch output')
    with tempfile.TemporaryDirectory(prefix='gpu-audit-',dir=root) as temporary:
        with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True) as raw, \
             sqlite3.connect(str(Path(temporary)/'joins.sqlite'),factory=WorkConnection) as work:
            raw.row_factory=work.row_factory=sqlite3.Row
            raw.execute('PRAGMA query_only=ON'); work.execute('PRAGMA cache_size=-65536')
            work.target_pid=target_pid
            require(raw.execute('PRAGMA quick_check').fetchone()[0]=='ok','SQLite integrity failed')
            tables=table_names(raw)
            require({'StringIds','NVTX_EVENTS','CUPTI_ACTIVITY_KIND_RUNTIME','CUPTI_ACTIVITY_KIND_KERNEL'}<=tables,'missing required trace tables')
            names={r['id']:r['value'] for r in rows(raw,'StringIds')}
            diagnostics=role_diagnostics(helper,raw,tables,server_text or '',export_text,wrapper_pid)
            if mode=='model':
                require(server_text is not None,'model audit requires CPU observer log')
                batches,spans,flush,captures=helper.cpu_records(server_text)
                require(captures[0]['pid']==target_pid,'capture PID differs')
                nvtx=helper.nvtx_records(raw,tables,names,spans,target_pid)
                groups=phase_groups(spans,batches)
            else:
                batches,spans,flush,captures={},{},{},[]
                nvtx=fixture_nvtx(raw,names,target_pid)
                groups={key:key for key in nvtx}
            flush_ranges,helper_ranges=helpers(raw,tables,names,nvtx)
            api_count,api_errors,helper_apis=build_api_table(raw,work,tables,names,nvtx,groups,flush_ranges)
            gpu_count,absent=build_gpu_table(raw,work,tables,names)
            devices=[r[0] for r in work.execute('SELECT DISTINCT device FROM gpu')]
            require(len(devices)==1,'single-device trace required')
            findings=coverage(work,mode)
            result=summarize(work,nvtx,spans,groups)
            count_unowned=work.execute("SELECT count(*) FROM gpu g JOIN api a ON a.aid=g.aid WHERE a.phase_group='unowned_application'").fetchone()[0]
            offset=None
            if spans:
                low=max(spans[k]['wall_end_ns']-v['end'] for k,v in nvtx.items())
                high=min(spans[k]['wall_begin_ns']-v['start'] for k,v in nvtx.items())
                offset=dict(lower_ns=low,upper_ns=high,common_intersection_exists=low<=high)
            fatal=(not diagnostics['valid'] or bool(api_errors) or
                   any(x['status']=='FAIL' for x in findings))
            integrity='FAIL' if fatal else ('UNKNOWN_CAPABILITY' if findings else 'PASS')
            result.update(schema=1,trace_integrity=integrity,
                stage_environment_verified=False,performance_acceptance=False,mode=mode,
                source=dict(path=str(path),sha256=before,target_pid=target_pid,
                    analyzer_sha256=sha(Path(__file__)),legacy_sha256=LEGACY_SHA,
                    server_text_sha256=(hashlib.sha256(server_text.encode()).hexdigest()
                        if server_text is not None else None),
                    export_text_sha256=hashlib.sha256(export_text.encode()).hexdigest()),
                diagnostics=diagnostics,api_errors=api_errors,coverage_findings=findings,
                coverage=dict(api_records=api_count,gpu_records=gpu_count,nvtx_ranges=len(nvtx),
                    cpu_spans=len(spans),unowned_application_gpu=count_unowned,
                    phase_coverage='PARTIAL_UNOWNED_APPLICATION' if count_unowned else 'OWNED_RECORDED_WORK',
                    absent_activity_tables=absent),
                helper_flush_ranges=helper_ranges,helper_api_evidence=helper_apis,
                raw_cpu_spans=[dict(v,batch_key=k[0],span_key=k[1],phase_group=groups[k],
                    nvtx_start_ns=nvtx[k]['start'],nvtx_end_ns=nvtx[k]['end']) for k,v in spans.items()],
                capture=captures,observer_flush=list(flush.values()),
                monotonic_minus_nsight_offset_bounds=offset,clock_alignment_used=False,
                limits=LIMITS)
    require(sha(path)==before,'source SQLite changed during audit')
    result['source_unchanged_after_read']=True
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sqlite',required=True,type=Path);p.add_argument('--pid',required=True,type=int)
    p.add_argument('--wrapper-pid',type=int)
    p.add_argument('--server-log',type=Path);p.add_argument('--export-log',type=Path)
    p.add_argument('--scratch-dir',required=True,type=Path);p.add_argument('--mode',choices=('model','fixture'),default='model')
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();require(not a.output.exists(),'refusing output overwrite')
    require(a.output.resolve()!=a.sqlite.resolve(),'output overlaps source')
    try:
        result=audit_database(a.sqlite,a.pid,server_text=a.server_log.read_text() if a.server_log else None,
            export_text=a.export_log.read_text() if a.export_log else '',scratch_dir=a.scratch_dir,mode=a.mode,wrapper_pid=a.wrapper_pid)
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as error:
        result=dict(schema=1,trace_integrity='FAIL',stage_environment_verified=False,
            performance_acceptance=False,analysis_error=f'{type(error).__name__}: {error}')
    with a.output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
    print(json.dumps(dict(output=str(a.output),trace_integrity=result['trace_integrity'])))
    return 0 if result['trace_integrity']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
