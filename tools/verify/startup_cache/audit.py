"""Offline audit of the bounded temporary-lock startup experiment.

Reads sealed evidence only. No CUDA/NVML, process launches, cache advice or
sampling. Missing stages are NOT_RUN; failure evidence never accepts a candidate.
The output must be a new file in this experiment, outside sealed stage folders.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/startup-cache-20261021'
OLD = ROOT / '.q4t-work/startup-plan-20261020'
STAGES = ('mtp-lock-v3', 'restart-lock-v3', 'boundary-lock-v3')
INTERRUPTED = {
    'mtp-lock-v1': '6348d946242c5d4c74809d8a7b59460a231b0eb261b2d539d0'
                   '556551558c97ff',
    'mtp-lock-v2': '8cad08ff5964c8c429167ec26f4e498b490a00424808b9c6f9'
                   '8f7cc28675fe48',
}
ALL_STAGES = (*INTERRUPTED, *STAGES)
COLLECTOR_PROFILE = 'resources_nvml_application_ledger'
HISTORY = ('plain-v1', 'mtp-v1', 'mtp-v2', 'mtp-v3', 'mtp-order-v1')
HOT_BYTES = 67987791872
DEFAULT = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
HISTORY_SEAL = '2f2fa5979bdb61c2a4b88d20c4ca1183629d8fb81ae999b27e90c7639e65a4d3'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# These modules have no execution side effects; main/worker are never called.
sys.path.insert(0, str(HERE))
old = load_module('startup_cache_old_audit', HERE.parent / 'startup_plan/audit.py')
runner = load_module('startup_cache_audit_runner', HERE / 'run.py')
require, read, sha = old.require, old.read, old.sha
one, finite = old.one_event, old.finite
SealedStage = old.SealedStage


def configuration(name):
    require(name in ALL_STAGES, 'unknown stage')
    boundary = name.startswith('boundary-')
    return dict(max_seq=2 if boundary else 1,
                max_prefill=2048 if boundary else 8192,
                max_len=8192 if boundary else 262144,
                resident_slots=128 if boundary else 256,
                mtp=not boundary, mtp_k=3,
                l2_slots=1 if boundary else 16,
                mirror_k=0 if boundary else 8,
                load_threads=1 if boundary else 16,
                requests=2 if name.startswith('mtp-') else 1)


def frozen_helpers(stage, build):
    relative = 'source/tools/verify/startup_cache/run.py'
    tree = ast.parse(stage.text(relative))
    names = {'require', 'read', 'records', 'capacity_evidence', 'check_plan',
             'reservation_evidence', 'environment', 'server_command',
             'configuration', 'binary_for', 'port_for', 'physical_order_evidence'}
    definitions = [n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name in names]
    require({n.name for n in definitions} == names,
            'frozen runner helper set is incomplete')
    namespace = dict(json=json, Path=Path, re=re, hashlib=hashlib, ROOT=ROOT,
                     BASE=BASE, BINARY=Path(build['candidate']['path']),
                     MODEL=runner.MODEL, STAGES=ALL_STAGES,
                     LEGACY=(*HISTORY, 'restart-v1', 'boundary-v1'),
                     BASE_ENVIRONMENT=runner.BASE_ENVIRONMENT, sha=sha,
                     audit_success=runner.audit_success)
    exec(compile(ast.Module(body=definitions, type_ignores=[]),
                 str(stage.path / relative), 'exec'), namespace)
    helper = SimpleNamespace(**namespace)
    require(helper.configuration(stage.path.name) == configuration(stage.path.name),
            'sealed runner configuration differs from frozen matrix')
    http = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'http_worker')
    calls = [(n.func.id, n.lineno) for n in ast.walk(http)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    gate = [line for name, line in calls if name == 'final_cache_gate']
    launch = [line for name, line in calls if name == 'IsolatedScope']
    require(len(gate) == len(launch) == 1 and gate[0] < launch[0],
            'frozen HTTP worker does not gate preparation before model creation')
    return helper, dict(sealed_runner_sha256=stage.manifest[relative],
                        pure_functions=sorted(names), gate_precedes_scope=True)


def ordered(rows, label):
    require(rows and [r['seq'] for r in rows] == list(range(len(rows))),
            label + ': event sequence differs')
    require(all(finite(r['mono']) for r in rows) and
            all(a['mono'] <= b['mono'] for a, b in zip(rows, rows[1:])),
            label + ': event clock differs')


def birth(identity):
    return identity['pid'], identity['start_ticks']


def helper_identity(identity, owner, expected, locked):
    require(birth(identity) == birth(expected) and
            identity['uid'] == owner['uid'] == [owner['uid'][0]] * 4 and
            identity['uid'][0] > 0 and identity['gid'][:2] == owner['gid'][:2]
            and identity['state'] != 'Z', 'lock helper process identity differs')
    require(not any(int(identity['capabilities'][key], 16) for key in
                    ('CapEff', 'CapPrm', 'CapInh', 'CapAmb', 'CapBnd')) and
            identity['no_new_privs'] == 1 and identity['memlock_error'] is None
            and identity['memlock'] == [HOT_BYTES, HOT_BYTES] and
            identity['vmlck_bytes'] == locked,
            'lock helper privilege, quota or VmLck contract differs')


def audit_locks(stage, complete):
    prefix = 'run/locked-warm/'
    manifest = stage.document(prefix + 'manifest.json')
    files = manifest['files']
    paths = [str(runner.MODEL / (
        f'layer-{layer:05d}-experts-{lo:04d}-{lo+127:04d}.safetensors'))
        for layer in range(48) for lo in range(0, 512, 128)]
    page = manifest['page_size']
    require(manifest['schema'] == 1 and manifest['fixture'] is False and
            type(page) is int and page > 0 and page & (page - 1) == 0 and
            [r['path'] for r in files] == paths and
            len({(r['dev'], r['inode']) for r in files}) == len(files) and
            all(type(r['size']) is int and r['size'] > 0 and
                r['lock_bytes'] == (r['size'] + page - 1) // page * page
                for r in files) and
            manifest['total_lock_bytes'] == sum(r['lock_bytes'] for r in files)
            == HOT_BYTES, 'lock selection is not the exact frozen production set')
    expected = stage.document('run/cache-expected.json')
    require(expected == runner.verified_cache_reference() and
            {path for path, size in expected.items() if size} == set(paths) and
            all(expected[r['path']] == r['lock_bytes'] for r in files),
            'lock selection differs from the sealed complete-hot vector')
    child = stage.lines(prefix + 'child-events.jsonl')
    parent = stage.lines(prefix + 'parent-events.jsonl')
    ordered(child, 'child')
    ordered(parent, 'parent')
    ready = one(child, 'ready')
    owner = stage.document(prefix + 'owner-before.json')
    identity = ready['identity']
    require(birth(ready['owner']) == birth(owner) and
            ready['owner']['uid'] == owner['uid'] and
            owner['vmlck_bytes'] == 0 and
            ready['manifest_sha256'] == sha(stage.file(prefix + 'manifest.json'))
            and all(r['pid'] == identity['pid'] for r in child),
            'lock owner/manifest identity differs')
    helper_identity(identity, owner, identity, 0)
    parent_ready = one(parent, 'ready')
    helper_identity(parent_ready['identity'], owner, identity, 0)
    chain = parent_ready['launch_chain']
    require(birth(chain[0]) == birth(identity) and birth(chain[-1]) == birth(owner)
            and all(a['ppid'] == b['pid'] for a, b in zip(chain, chain[1:])),
            'helper is not in the recorded owner launch ancestry')
    command = stage.document(prefix + 'command.json')
    for item in ('/usr/bin/sudo', '/usr/bin/prlimit', '/usr/bin/setpriv',
                 '--memlock=' + str(HOT_BYTES) + ':' + str(HOT_BYTES),
                 '--reuid=' + str(owner['uid'][0]),
                 '--regid=' + str(owner['gid'][0]), '--bounding-set=-all',
                 '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
                 '--owner-pid', str(owner['pid']), '--owner-start-ticks',
                 str(owner['start_ticks'])):
        require(item in command, 'helper launch contract missing: ' + item)
    mappings, live, locked, resident, released, unmapped = [], [], set(), set(), [], []
    for row in child:
        event = row['event']
        if event == 'mmap':
            index = row['index']
            require(index == len(mappings) < len(files) and row['rc'] == 0 and
                    row['ptr'] not in [r['ptr'] for r in mappings] and
                    all(row[key] == files[index][key] for key in
                        ('bytes', 'lock_bytes', 'path') if key != 'bytes') and
                    row['bytes'] == files[index]['size'], 'mmap ledger differs')
            mappings.append(row)
            live.append(row['ptr'])
        elif event == 'mlock':
            require(mappings and row['ptr'] == mappings[-1]['ptr'] and
                    row['bytes'] == mappings[-1]['bytes'] and
                    row['ptr'] not in locked, 'mlock ledger differs')
            if row['rc'] == 0:
                require(row['errno'] == 0, 'successful mlock has errno')
                locked.add(row['ptr'])
        elif event == 'mincore':
            require(mappings and row['ptr'] == mappings[-1]['ptr'] and
                    row['index'] == mappings[-1]['index'] and
                    row['ptr'] in locked and row['ptr'] not in resident and
                    row['pages'] == files[row['index']]['lock_bytes'] // page,
                    'mincore mapping/length differs')
            if row['rc'] == 0 and row['resident_pages'] == row['pages']:
                resident.add(row['ptr'])
        elif event in ('munlock', 'munmap'):
            require(live and row['ptr'] == live[-1] and
                    row['bytes'] == mappings[len(live) - 1]['bytes'],
                    'release was not reverse mmap order')
            if row['rc'] == 0:
                require(row['errno'] == 0, 'successful release has errno')
                if event == 'munlock':
                    require(row['ptr'] not in released, 'duplicate munlock')
                    released.append(row['ptr'])
                else:
                    require(row['ptr'] in released, 'munmap without munlock')
                    unmapped.append(live.pop())
        elif event not in ('ready', 'granted', 'start', 'held', 'summary'):
            raise ValueError('unknown child event: ' + event)
    summary = one(child, 'summary')
    exit_info = stage.document(prefix + 'exit.json')
    parent_exit = one(parent, 'exit')
    require(exit_info['summary'] == summary and all(parent_exit[k] == value
            for k, value in exit_info.items()) and exit_info['reaped'] and
            not exit_info['recovery_required'] and not exit_info['logging_errors']
            and not live and summary['remaining_mappings'] == 0 and
            summary['final_identity']['vmlck_bytes'] == 0 and
            summary['mono'] <= exit_info['finished_mono'] <= parent_exit['mono'],
            'helper cleanup is incomplete or receipts disagree')
    helper_identity(summary['final_identity'], owner, identity, 0)
    state = stage.document('state.json')['lock_owner_cleanup']
    require(state['started'] and not state['live_lock_owner'] and
            birth(state['identity']) == birth(identity),
            'supervisor did not independently confirm helper disappearance')
    if complete:
        granted, start, held = (one(child, event) for event in
                                ('granted', 'start', 'held'))
        helper_identity(granted['identity'], owner, identity, 0)
        helper_identity(start['identity'], owner, identity, 0)
        helper_identity(held['identity'], owner, identity, HOT_BYTES)
        require(len(mappings) == len(locked) == len(resident) == len(files) and
                len(released) == len(unmapped) == len(files) and
                held['files'] == len(files) and held['locked_bytes'] == HOT_BYTES
                and ready['mono'] <= granted['mono'] <= start['mono'] <=
                mappings[0]['mono'] <= held['mono'] <= summary['mono'] and
                summary['status'] == 'PASS' and summary['reason'] == 'released'
                and summary['held'] and not summary['errors'] and
                exit_info['release_success'] and exit_info['returncode'] == 0,
                'complete lock/acquire/release contract failed')
        require([r['command'] for r in parent if r['event'] == 'command'] ==
                ['LIMIT ' + str(HOT_BYTES), 'START', 'RELEASE'],
                'unexpected lock control commands')
        require([row['event'] for row in child] ==
                ['ready', 'granted', 'start'] +
                ['mmap', 'mlock', 'mincore'] * len(files) + ['held'] +
                ['munlock', 'munmap'] * len(files) + ['summary'],
                'successful child did not follow the exact per-file lifecycle')
    return dict(status=summary['status'], helper=identity, owner=owner,
                selected_files=len(files), selected_bytes=HOT_BYTES,
                mapped_files=len(mappings), locked_files=len(locked),
                fully_resident_files=len(resident), unmapped_files=len(unmapped),
                exit=exit_info, parent_exit_mono=parent_exit['mono'],
                parent_exit_wall=parent_exit['wall'], cleanup_confirmed=True)


def snapshot(stage, name, identity):
    row = stage.document('run/' + name + '-cgroup.json')
    require(row['pid'] == identity['pid'] and row['identity_valid'] and
            row['start_ticks'] == row['expected_start_ticks'] == identity['start_ticks']
            and row['stat_before']['value'] == row['stat_after']['value'] ==
            identity['start_ticks'] and row['cgroup_before']['status'] ==
            row['cgroup_after']['status'] == 'OBSERVED' and
            row['cgroup_before']['value'] == row['cgroup_after']['value'] and
            row['mount']['status'] == 'OBSERVED' and
            row['mount']['mount_root'] == '/', 'preparation cgroup identity unknown')
    group = row['cgroup_before']['value']
    require(row['cgroup_before']['raw'] == identity['cgroup'],
            'preparation cgroup differs from helper birth identity')
    expected = ['/' + '/'.join(group.strip('/').split('/')[:depth])
                for depth in range(len(group.strip('/').split('/')), -1, -1)]
    if group == '/':
        expected = ['/']
    require([g['cgroup'] for g in row['ancestors']] == expected,
            'preparation ancestor hierarchy incomplete')
    for ancestor in row['ancestors']:
        if ancestor['cgroup'] != '/':
            require(ancestor['status'] == 'OBSERVED' and
                    all(value['status'] == 'OBSERVED' for value in
                        ancestor['files'].values()),
                    'preparation ancestor memory boundary unknown')
    require(row['started']['monotonic'] <= row['finished']['monotonic'],
            'cgroup snapshot clock differs')
    return row


def audit_preparation(stage, locks, events):
    expected = stage.document('run/cache-expected.json')
    require(expected == runner.verified_cache_reference(), 'cache vector changed')
    before = snapshot(stage, 'preparation-parent-before', locks['owner'])
    ready = snapshot(stage, 'helper-ready', locks['helper'])
    held = snapshot(stage, 'helper-held', locks['helper'])
    after = snapshot(stage, 'preparation-parent-after', locks['owner'])
    require(len({r['cgroup_before']['value'] for r in
                 (before, ready, held, after)}) == 1,
            'helper/parent preparation cgroup changed')
    for row in (before, ready):
        for ancestor in row['ancestors']:
            if ancestor['cgroup'] == '/':
                continue
            values = {key: value['value'] for key, value in ancestor['files'].items()}
            require(all(values[key] == 'max' or
                        values['memory.current'] + HOT_BYTES <= values[key]
                        for key in ('memory.max', 'memory.high')),
                    'preparation was started without recorded cgroup headroom')
    budget = stage.document('run/lock-budget.json')
    require(type(budget['MemAvailable']) is int and
            type(budget['MemTotal']) is int and
            budget['MemAvailable'] >= HOT_BYTES and budget['MemTotal'] > HOT_BYTES,
            'recorded physical headroom check failed')
    for name in ('cache-held.json', 'cache-launch.json'):
        observation = stage.document('run/' + name)
        runner.check_vector(observation, expected)
        observed = {row['path']: row for row in observation['files']}
        require(not observation['errors'] and len(observed) ==
                len(observation['files']), 'cache errors or duplicate paths')
        manifest = stage.document('run/locked-warm/manifest.json')
        require(observation['page_size_bytes'] == manifest['page_size'] and
                all(observed[row['path']]['dev'] == row['dev'] and
                    observed[row['path']]['inode'] == row['inode'] and
                    observed[row['path']]['size_bytes'] == row['size']
                    for row in manifest['files']),
                'cache observation refers to other lock-selected files')
    preparation = stage.document('run/cache-preparation.json')
    attempts = preparation['attempts']
    require(not preparation['errors'] and 1 <= len(attempts) <= 3 and
            [row['pass_number'] for row in attempts] ==
            list(range(1, len(attempts) + 1)) and all(
                not row['advice_errors'] and
                row['observation']['complete_file_set_observed'] and
                not row['observation']['errors'] for row in attempts) and
            attempts[-1]['payload_resident_bytes'] == 0 and
            sum(row['resident_bytes'] for row in attempts[-1]['observation']['files']
                if Path(row['path']).suffix in ('.safetensors', '.bin')) == 0,
            'bounded zero-payload prerequisite differs')
    launch = stage.document('run/cache-launch.json')
    gate = stage.document('run/cache-gate.json')
    require(gate == dict(payload_bytes=HOT_BYTES, file_vector_exact=True,
                temporary_locks_released=True, physical_fragmentation_equal=False,
                repair_passes=0), 'cache gate contract differs')
    before_start = one(events, 'server_before_start')
    require(locks['parent_exit_wall'] <= launch['start_t'] <= launch['end_t'] <=
            before_start['unix_seconds'], 'unlock/cache/model wall order differs')
    require(locks['exit']['finished_mono'] <= after['started']['monotonic'] <=
            after['finished']['monotonic'] <= before_start['monotonic_seconds'],
            'helper exit/parent recheck/model monotonic order differs')
    started = one(events, 'server_started')
    require(before_start['monotonic_seconds'] <= started['monotonic_seconds'],
            'model creation event order differs')
    clock = stage.document('run/cache-launch-time.json')
    require(locks['exit']['finished_mono'] <= clock['lock_owner_reaped_mono'] <=
            clock['scan_begin_mono'] <= clock['scan_end_mono'] <=
            before_start['monotonic_seconds'] <= started['monotonic_seconds'],
            'unlock/cache-scan/model monotonic order differs')
    return dict(exact_full_hot_gate=True, payload_bytes=HOT_BYTES,
                temporary_locks_gone_before_model=True, repair_passes=0,
                preparation_cgroup=ready['cgroup_before']['value'],
                preparation_ancestors=ready['ancestors'],
                model_scope_limit_is_not_preparation_limit=True,
                physical_fragmentation_equal=False,
                clock_evidence='helper exit, cache scan and model creation '
                'monotonic timestamps; sealed gate-before-scope control flow',
                monotonic_cache_launch=clock,
                parent_after_limit_evidence='sealed runner checked equal '
                'RLIMIT_MEMLOCK and VmLck=0; after identity values were not persisted',
                observation_limits=ready['limitations'])


def audit_owned(stage):
    owned = stage.document('run/normal-process.json')
    require(owned['runner_reaped'] and owned['cleanup_complete'] and
            not owned['unexpected_live_descendants_after_runner_exit'] and
            not owned['after_cleanup']['live_pids'] and
            not owned['after_cleanup']['zombie_pids'],
            'HTTP worker ownership cleanup incomplete')
    return {key: owned[key] for key in ('returncode', 'runner_reaped',
            'cleanup_complete', 'failure')}


def audit_runtime(stage, helper, log, build):
    path = stage.file('run/startup-plan.json')
    plan = read(path)
    reservation = helper.reservation_evidence(log, path)
    require(stage.document('run/normal/http/reservation-audit.json') == reservation,
            'reservation receipt differs from independent ledger')
    require(stage.text('run/normal/http/binary.sha256').strip() ==
            build['candidate_sha256'] and
            sha(stage.file('run/normal/http/CMakeCache.txt')) ==
            build['real_build_cache']['sha256'], 'launched program identity differs')
    logical = old.audit_logical_trace(stage.file('run/normal/http/server.log'))
    require(not logical['errors'] and not logical['failed_calls'] and
            not logical['incomplete_calls'], 'logical allocation lifecycle failed')
    return dict(plan_sha256=sha(path), plan_count=plan['count'],
                planned_device_bytes=plan['device_bytes'],
                reservation=reservation,
                physical_order=helper.physical_order_evidence(log, plan),
                http=old.audit_http(stage, stage.path.name, log, path, helper),
                timing=old.timing(stage, log),
                logical_trace={k: v for k, v in logical.items()
                               if k not in ('normalized', 'remaining')})


def audit_reservation_prefix(stage, helper):
    """Validate the captured pre-handoff ledger without inventing exit frees."""
    path = stage.file('run/startup-plan.json')
    plan = read(path)
    log = stage.text('run/normal/http/server.log')
    rows = helper.records(log, '[reserve-trace] ')
    configured = one(helper.records(log, '[startup-plan] '), 'configured')
    allocations = plan['allocations']
    require(plan['schema'] == 1 and plan['derivation'] ==
            'configuration_tensor_metadata' and plan['count'] == len(allocations)
            and len({row['name'] for row in allocations}) == len(allocations)
            and all(type(row['bytes']) is int and row['bytes'] > 0
                    for row in allocations) and plan['device_bytes'] ==
            sum(row['bytes'] for row in allocations), 'partial metadata plan differs')
    cfg = configuration(stage.path.name)
    require(all(plan['effective'][key] == value for key, value in cfg.items()
                if key != 'requests') and plan['effective']['initial_scratch_capacity']
            == (4 if cfg['mtp'] else 0), 'partial plan effective configuration differs')
    require(configured['plan_sha256'] == sha(path) and
            configured['effective'] == plan['effective'] and
            configured['count'] == plan['count'] and
            configured['device_bytes'] == plan['device_bytes'],
            'partial configured/plan identity differs')
    ordered(rows, 'reservation')
    header = one(rows, 'plan')
    require(rows[0] == header and header['plan_sha256'] == sha(path) and
            header['count'] == plan['count'] and
            header['total_bytes'] == plan['device_bytes'],
            'partial physical plan identity differs')
    order = sorted(range(len(allocations)),
                   key=lambda index: -allocations[index]['bytes'])
    require(plan['physical_order'] == dict(strategy='stable_descending_bytes',
                                         indices=order), 'partial physical order differs')
    if any(row['event'] not in ('plan', 'alloc') for row in rows):
        return dict(plan_sha256=sha(path), status='PARTIAL_AFTER_PREPARATION',
                    ownership_complete=False,
                    reason='failure progressed beyond the pre-handoff prefix; '
                    'no successful release claim', events=len(rows))
    completed, opened, spans, pointers = [], None, [], set()
    for row in rows[1:]:
        ordinal = len(completed)
        require(row['event'] == 'alloc' and row['api'] == 'cudaMalloc' and
                ordinal < len(order) and row['physical_ordinal'] == ordinal and
                row['index'] == order[ordinal] and
                row['bytes'] == allocations[row['index']]['bytes'],
                'physical allocation prefix index/size differs')
        if row['phase'] == 'begin':
            require(opened is None and row['ptr'] == 0,
                    'duplicate or invalid allocation begin')
            opened = row
        else:
            require(row['phase'] == 'end' and opened is not None and
                    row['mono'] >= opened['mono'] and row['rc'] == 0 and
                    row['ptr'] != 0 and row['ptr'] not in pointers,
                    'partial physical completion differs')
            pointers.add(row['ptr'])
            spans.append(row['mono'] - opened['mono'])
            completed.append(row)
            opened = None
    require('[q4t] serving on port ' not in log and
            not helper.records(log, '[memory-trace] '),
            'pre-handoff failure performed logical calls or became ready')
    return dict(status='INCOMPLETE_PHYSICAL_RESERVATION', plan_sha256=sha(path),
                count=plan['count'], planned_bytes=plan['device_bytes'],
                completed=len(completed), completed_bytes=sum(row['bytes']
                for row in completed), incomplete=opened, handoffs=0,
                maximum_completed_seconds=max(spans, default=0),
                application_release_status='UNKNOWN_PROCESS_ABORTED',
                physical_driver_reclamation='NOT_PROVEN_BY_SCOPE_CLEANUP')


def audit_stage(name, build):
    path = BASE / name
    if not path.exists():
        return dict(name=name, status='NOT_RUN', runtime_accepted=False,
                    model_starts=0, http_requests=0)
    stage = SealedStage(path)
    old.stage_identity(stage, build)
    require(sha(HERE.parent / 'startup_plan/audit.py') ==
            stage.manifest['source/tools/verify/startup_plan/audit.py'],
            'reused offline audit functions differ from stage snapshot')
    helper, helper_record = frozen_helpers(stage, build)
    state = stage.document('state.json')
    require(state['configuration'] == configuration(name) and
            state['model_load_budget'] == 1 and
            state['http_budget'] == configuration(name)['requests'] and
            state['cache_contract'] == 'temporary_mlock_released_exact_full_hot',
            'stage configuration/budget differs')
    passed = state['status'] == 'RUNTIME_COMPLETED'
    events = (stage.lines('run/normal/http/events.jsonl') if
              'run/normal/http/events.jsonl' in stage.manifest else [])
    starts = sum(r['event'] == 'server_started' for r in events)
    requests = sum(r['event'] == 'client_before' for r in events)
    require(starts <= 1 and requests <= configuration(name)['requests'],
            'stage exceeded model/request budget')
    checks = {}

    def check(label, function):
        try:
            checks[label] = dict(status='PASS', result=function())
        except Exception as error:
            checks[label] = dict(status='FAILED', error=repr(error))

    def guard_check():
        guard = stage.document('lease/exit.json')
        require(guard['compute_backend'] == 'nvml' and guard['external_lease'] and
                guard['poll_interval_seconds'] == 1 and
                'cleanup_error' not in guard and
                ('guard' not in state or state['guard'] == guard),
                'guard identity/cleanup differs')
        if passed:
            require(guard['status'] == 'COMPLETED' and
                    guard['child_returncode'] == state['guard_returncode'] == 0,
                    'completed stage has unsuccessful guard')
        return guard

    def collector_check():
        resources = stage.document('resources/exit.json')
        require(resources['status'] == 'COMPLETED' and
                not resources['failures'] and not resources['truncated'] and
                state['resources']['returncode'] == 0 and
                state['resources']['result'] == resources,
                'resources or supervisor receipt differs')
        system = stage.lines('resources/resources.jsonl')
        cgroups = stage.lines('resources/cgroup.jsonl')
        caches = sorted(key for key in stage.manifest
                        if key.startswith('resources/cache/') and key.endswith('.json'))
        require(len(system) == resources['system_samples'] > 0 and
                len(cgroups) == resources['cgroup_samples'] > 0 and
                len(caches) == resources['cache_samples'] > 0 and
                all(not row['failures'] for row in system + cgroups),
                'resource observation count or per-sample failures differ')
        for path in caches:
            cache = stage.document(path)
            require(cache['complete_file_set_observed'] and not cache['errors'],
                    'resource cache observation incomplete')
        require(stage.document('driver-identity/driver_identity.json')
                ['attribution_valid'], 'driver attribution failed')
        if not starts:
            for row in system:
                require(not row['failures'] and not any(target['kind'] == 'model'
                        for target in row['targets']),
                        'model observed despite no launch event')
        evidence = dict(driver_attribution_valid=True,
                resource_samples={key: resources[key] for key in
                    ('system_samples', 'cache_samples', 'cgroup_samples',
                     'skipped_system_intervals', 'skipped_cache_intervals')})
        if name in STAGES:
            require(state['collector_profile'] == COLLECTOR_PROFILE and
                    state['kernel_timeline'] == 'NOT_COLLECTED' and
                    'kernel' not in state and not (stage.path / 'kernel').exists()
                    and not any(key.startswith('kernel/') or key in
                        ('kernel-driver.log', 'stop-kernel') for key in stage.manifest),
                    'resources-only stage has unexpected kernel capture evidence')
            source = ast.parse(stage.text('source/tools/verify/startup_cache/run.py'))
            supervisor = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                              and n.name == 'supervise')
            require(not any(isinstance(n, ast.Constant) and
                    n.value == 'kernel_capture.py' for n in ast.walk(supervisor)),
                    'frozen resources-only supervisor still launches kernel collector')
            return dict(**evidence, timeline_status='NOT_COLLECTED',
                        collector_profile=COLLECTOR_PROFILE,
                        kernel_attribution_available=False)
        kernel = stage.document('kernel/exit.json')
        require(not passed and kernel['status'] == 'FAILED' and
                state['kernel']['result'] == kernel and
                kernel['instance_removed'] and kernel['global_configuration_equal'],
                'known failed collector status/cleanup differs')
        if name == 'mtp-lock-v1':
            require(kernel['reason'] == 'CaptureFailure: trace byte limit reached'
                    and kernel['trace_bytes'] == kernel['max_bytes'] == 512 << 20
                    and kernel['trace_unwritten_at_least'] > 0 and
                    not any(kernel['loss_totals'].values()),
                    'known byte-cap interruption differs')
        else:
            require(name == 'mtp-lock-v2' and kernel['reason'] ==
                    'CaptureFailure: trace events lost: '
                    '{"overrun": 5501, "commit overrun": 0, "dropped events": 0}'
                    and kernel['loss_totals'] == dict(overrun=5501,
                        **{'commit overrun': 0, 'dropped events': 0}) and
                    kernel['trace_bytes'] == 207554304 < kernel['max_bytes'] == 2 << 30
                    and kernel['trace_unwritten_at_least'] == 0,
                    'known ring-overrun interruption differs')
        return dict(**evidence, timeline_status='INCOMPLETE',
                    trace_loss=kernel['loss_totals'], instance_removed=True,
                    global_trace_configuration_unchanged=True,
                    written_bytes=kernel['trace_bytes'],
                    unwritten_at_least=kernel.get('trace_unwritten_at_least', 0),
                    collector_status=kernel['status'], reason=kernel['reason'])

    check('guard', guard_check)
    check('nvml', lambda: old.audit_nvml(stage, must_succeed=passed))
    check('collectors', collector_check)
    if 'run/normal-process.json' in stage.manifest or passed:
        check('owned_runner', lambda: audit_owned(stage))
    if starts or passed:
        check('model_scope', lambda: old.audit_scope(stage, accepted=passed))
    else:
        check('no_model_scope', lambda: require(
            state['scope_final'] == {'scope_created': False},
            'zero model starts has a model scope'))
    locks = None
    if 'run/locked-warm/manifest.json' in stage.manifest or passed:
        check('temporary_locks', lambda: audit_locks(stage,
              complete=passed or 'run/cache-gate.json' in stage.manifest))
        if checks['temporary_locks']['status'] == 'PASS':
            locks = checks['temporary_locks']['result']
    else:
        check('no_lock_owner', lambda: require(state['lock_owner_cleanup'] ==
              dict(started=False, live_lock_owner=False),
              'pre-helper failure has an unaccounted lock owner'))
    if starts or passed:
        check('preparation', lambda: audit_preparation(stage, locks, events))
    elif 'run/cache-launch.json' in stage.manifest:
        check('unlaunched_cache', lambda: runner.check_vector(
            stage.document('run/cache-launch.json'),
            stage.document('run/cache-expected.json')))
    if passed:
        require(starts == 1 and requests == configuration(name)['requests'],
                'completed stage did not execute its full matrix entry')
        check('runtime', lambda: audit_runtime(stage, helper,
              stage.text('run/normal/http/server.log'), build))
    elif 'run/startup-plan.json' in stage.manifest:
        check('reservation_prefix', lambda: audit_reservation_prefix(stage, helper))
    failures = {key: value['error'] for key, value in checks.items()
                if value['status'] == 'FAILED'}
    accepted = passed and not failures
    report = dict(name=name, status='PASS_BOUNDED_RELIABILITY' if accepted else
                  'AUDIT_FAILED' if passed else 'RUNTIME_NOT_ACCEPTED',
                  runtime_accepted=accepted, candidate_quality_no_go=False,
                  recorded_status=state['status'], model_starts=starts,
                  http_requests=requests, checks=checks, audit_errors=failures,
                  offline_helpers=helper_record,
                  candidate_sha256=build['candidate_sha256'],
                  default_unchanged=True, sealed_files=len(stage.manifest),
                  sealed_manifest_sha256=sha(stage.manifest_path),
                  performance_acceptance=False)
    for relative in ('run/state.json', 'run/normal/http/exit.json'):
        if relative in stage.manifest:
            report.setdefault('recorded_outcomes', {})[relative] = stage.document(relative)
    report['direct_inputs_sha256'] = {key: stage.manifest[key]
                                     for key in sorted(stage.used)}
    return report


def audit_budget(reports):
    require(sha(OLD / 'final-evidence.json') == HISTORY_SEAL,
            'historical final seal identity changed')
    historical_seal = read(OLD / 'final-evidence.json')
    history = []
    for name in HISTORY:
        seal = OLD / name / 'sealed-files.json'
        require(sha(seal) == historical_seal[name + '/sealed-files.json'],
                'historical stage seal differs')
        manifest = read(seal)
        relative = 'run/normal/http/events.jsonl'
        events = []
        if relative in manifest:
            path = OLD / name / relative
            require(sha(path) == manifest[relative], 'historical events changed')
            events = [old.decode(line) for line in path.read_text().splitlines()]
        history.append(dict(name=name, model_starts=sum(row['event'] ==
            'server_started' for row in events), http_requests=sum(row['event'] ==
            'client_before' for row in events), manifest_sha256=sha(seal)))
    prior, stopped = list(history), False
    for report in reports:
        if report['status'] == 'NOT_RUN':
            stopped = True
            continue
        require(not stopped, 'stage ran after a missing or failed predecessor')
        receipt = read(BASE / report['name'] / 'attempt-budget.json')
        name = report['name']
        interrupted = name in INTERRUPTED
        ordinal = ALL_STAGES.index(name)
        version = 1 if name == 'mtp-lock-v1' else 2 if name == 'mtp-lock-v2' else 3
        require(receipt['model_start_limit'] == 4 + version and
                receipt['http_limit'] == 5 and
                receipt['preparation_limit'] == 2 + version and
                receipt['no_retry'] and receipt['prior_new_preparations'] == ordinal
                and receipt['prior_model_starts'] == sum(r['model_starts'] for r in prior)
                and receipt['prior_http_requests'] == sum(r['http_requests'] for r in prior)
                and [r['name'] for r in receipt['prior']] == [r['name'] for r in prior],
                'attempt budget does not bind all historical/new attempts')
        for actual, expected in zip(receipt['prior'], prior):
            require(all(actual[key] == expected[key] for key in
                    ('name', 'model_starts', 'http_requests', 'manifest_sha256')),
                    'attempt budget references another prior stage')
        if interrupted:
            require(report['sealed_manifest_sha256'] == INTERRUPTED[name] and
                    report['recorded_status'] == 'FAILED' and
                    report['model_starts'] == 1 and report['http_requests'] == 0
                    and report['checks']['collectors']['result']['timeline_status']
                    == 'INCOMPLETE' and not report['audit_errors'],
                    'known collector interruption differs')
        if version == 2:
            require(receipt['collector_retest_limit'] == 1 and
                    receipt['trace_max_bytes'] == 2 << 30,
                    'collector correction budget differs')
        elif version == 3:
            require(receipt['collector_interruptions_retained'] == 2 and
                    receipt['collector_profile'] == COLLECTOR_PROFILE,
                    'reliability observation profile differs')
        prior.append(dict(name=report['name'], model_starts=report['model_starts'],
            http_requests=report['http_requests'],
            manifest_sha256=report['sealed_manifest_sha256']))
        stopped = not interrupted and report['recorded_status'] != 'RUNTIME_COMPLETED'
    starts, requests = (sum(row[key] for row in prior)
                        for key in ('model_starts', 'http_requests'))
    require(starts <= 7 and requests <= 5, 'cumulative model/HTTP budget exceeded')
    return dict(model_starts=starts, http_requests=requests,
                model_start_limit=7, http_limit=5, new_preparation_limit=5,
                collector_interruptions_retained=2, interrupted_stage_seals=INTERRUPTED,
                historical_manifest_sha256=HISTORY_SEAL)


def main():
    require(__debug__, 'ledger assertions require Python without -O')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(BASE.resolve()) and not output.exists() and
            not any(output.is_relative_to(BASE / name) for name in ALL_STAGES),
            'output must be new and in the new BASE, outside sealed stage folders')
    require(not any((parent / 'sealed-files.json').exists()
                    for parent in output.parents if parent.is_relative_to(BASE)),
            'cannot write into a sealed evidence directory')
    require(sha(ROOT / 'build/q4t') == DEFAULT, 'default binary changed')
    build_path = OLD / 'order-candidate/build.json'
    build = read(build_path)
    build_record = old.verify_build_record(build, build_path)
    reports = [audit_stage(name, build) for name in ALL_STAGES]
    budget = audit_budget(reports)
    accepted = all(row['runtime_accepted'] for row in reports[2:])
    if reports[3]['runtime_accepted']:
        require(sha(BASE / STAGES[0] / 'run/startup-plan.json') ==
                sha(BASE / STAGES[1] / 'run/startup-plan.json'),
                'restart derived plan differs from initial MTP plan')
    result = dict(schema=1, status='PASS_BOUNDED_RELIABILITY' if accepted else
                  'NOT_RUN' if all(r['status'] == 'NOT_RUN' for r in reports)
                  else 'INCOMPLETE_OR_NOT_ACCEPTED', runtime_accepted=accepted,
                  stages=reports, build=build_record, budget=budget,
                  default_sha256=DEFAULT, default_unchanged=True,
                  collector_profile=COLLECTOR_PROFILE,
                  acceptance_kernel_timeline='NOT_COLLECTED',
                  performance_acceptance=False, gpu_execution=False,
                  audit_source_sha256=sha(Path(__file__)),
                  reused_audit_sha256=sha(HERE.parent / 'startup_plan/audit.py'),
                  limitations=['Exact page-cache vectors do not establish equal '
                      'physical fragmentation or NVIDIA pool state.',
                      'Preparation observations belong to the recorded helper '
                      'cgroup; the 64 GiB model scope is a different boundary.',
                      'The v3 reliability profile does not collect a kernel '
                      'timeline; vmstat/cgroup samples cannot replace per-PID '
                      'compaction or shrinker attribution. Prior trace failures '
                      'remain incomplete and do not accept the candidate.',
                      'Success applies only to this preparation method, physical '
                      'allocation ordering and the tested configurations/text.'])
    old.write_new(output, result)
    print(json.dumps(dict(status=result['status'], output=str(output))))
    return 0 if not any(row.get('audit_errors') for row in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
