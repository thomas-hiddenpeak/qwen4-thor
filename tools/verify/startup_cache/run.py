"""Bounded startup verification with temporary, released file-cache locks.

Frozen derivative of startup_plan/run.py: old evidence and runner stay intact.
Each stage has one preparation, one model start, and an uninterrupted GPU lease.
Locks are gone and the original exact vector passes before model creation.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/startup-cache-20261021'
HISTORY_BASE = ROOT / '.q4t-work/startup-plan-20261020'
HISTORY_SEAL_SHA = ('2f2fa5979bdb61c2a4b88d20c4ca1183629d8fb81ae999b27'
                    'e90c7639e65a4d3')
CACHE_SOURCE = ROOT / '.q4t-work/device-reserve-20261020/reserve-v1'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
BUILD_MANIFEST = HISTORY_BASE / 'order-candidate/build.json'
BINARY = HISTORY_BASE / 'order-candidate/build/q4t'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
HOST_LIMIT = 64 << 30
HOT_BYTES = 67987791872
sys.path[:0] = [str(ROOT / 'tools/evalscope'),
               str(ROOT / 'tools/verify/vmm_compare'),
               str(ROOT / 'tools/verify/device_reserve')]
from cache_start import check_vector, expected_vector
from cgroup_observer import observe_process
from locked_warm import LockedWarm, make_manifest, identity
from file_cache import model_files, observe_files
from gpu_exclusive import common_lock
from isolated_scope import IsolatedScope
from offload_policy import BASE_ENVIRONMENT
from reservation_audit import audit_success
from run_acceptance import event, run_case
from run_budget_experiment import (run_owned_runner, prepare_target_cache,
                                   payload_residual)

# Reuse ownership/collector helpers, without changing the previous driver.
_spec = importlib.util.spec_from_file_location(
    'previous_device_reserve_driver', ROOT / 'tools/verify/device_reserve/run.py')
previous = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(previous)
sha, stamp = previous.sha, previous.stamp
OBSERVE = previous.OBSERVE
STAGES = ('mtp-lock-v3', 'restart-lock-v3', 'boundary-lock-v3')
HISTORY = ('plain-v1', 'mtp-v1', 'mtp-v2', 'mtp-v3', 'mtp-order-v1')
LEGACY = (*HISTORY, 'restart-v1', 'boundary-v1')
INTERRUPTED = {
    'mtp-lock-v1': '6348d946242c5d4c74809d8a7b59460a231b0eb261b2d539d0'
                   '556551558c97ff',
    'mtp-lock-v2': '8cad08ff5964c8c429167ec26f4e498b490a00424808b9c6f9'
                   '8f7cc28675fe48',
}
MODEL_START_LIMIT = 7
HTTP_LIMIT = 5
COLLECTOR_PROFILE = 'resources_nvml_application_ledger'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def configuration(stage):
    require(stage in (*STAGES, *LEGACY), 'unknown bounded stage')
    boundary = stage.startswith('boundary-')
    return dict(max_seq=2 if boundary else 1,
                max_prefill=2048 if boundary else 8192,
                max_len=8192 if boundary else 262144,
                resident_slots=128 if boundary else 256,
                mtp=not boundary and stage != 'plain-v1', mtp_k=3,
                l2_slots=1 if boundary else 16,
                mirror_k=0 if boundary else 8,
                load_threads=1 if boundary else 16,
                requests=2 if stage.startswith('mtp-') else 1)


def binary_for(stage):
    return BINARY


def port_for(stage):
    if stage == 'plain-v1':
        return 8840
    return 8843 if stage.startswith('boundary-') else (
        8842 if stage.startswith('restart-') else 8841)


def environment(stage, run, inherited):
    cfg = configuration(stage)
    env = {k: v for k, v in inherited.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env.update(BASE_ENVIRONMENT)
    env.update(Q4T_MOE_CHUNK_ORDER='0',
               Q4T_MOE_L2_SLOTS=str(cfg['l2_slots']),
               Q4T_MOE_MIRROR_K=str(cfg['mirror_k']),
               Q4T_MOE_LOAD_THREADS=str(cfg['load_threads']),
               Q4T_STARTUP_PLAN_OUTPUT=str(run / 'startup-plan.json'))
    return env


def server_command(stage, run):
    cfg = configuration(stage)
    command = [str(binary_for(stage)), 'serve', '--model-dir', str(MODEL),
               '--port', str(port_for(stage)),
               '--max-seq', str(cfg['max_seq']),
               '--max-prefill', str(cfg['max_prefill']),
               '--max-len', str(cfg['max_len']), '--max-tokens', '32',
               '--moe-resident-slots', str(cfg['resident_slots']),
               '--moe-hot-list', str(run / 'hot.json'),
               '--request-deadline-ms', '1800000']
    if not cfg['mtp']:
        command.append('--no-mtp')
    return command


def check_identity(identity):
    path = Path(identity['path']).resolve(strict=True)
    require(path.is_file() and path.stat().st_size == identity['bytes'] and
            sha(path) == identity['sha256'], 'build input differs: ' + str(path))
    return path


def verify_build():
    build = read(BUILD_MANIFEST)
    require(build['status'] == 'COMPLETED', 'build not completed')
    require(build['default_sha256'] == DEFAULT_SHA and
            sha(ROOT / 'build/q4t') == DEFAULT_SHA, 'default identity changed')
    require(check_identity(build['candidate']) == BINARY.resolve() and
            build['candidate_sha256'] == build['candidate']['sha256'],
            'candidate identity differs')
    require(check_identity(build['real_build_cache']) ==
            BINARY.with_name('CMakeCache.txt').resolve(), 'wrong build cache')
    for group in ('source_and_plan_inputs', 'link_inputs'):
        require(bool(build[group]), 'missing build dependency identities')
        for identity in build[group].values():
            check_identity(identity)
    return build


def records(log, prefix):
    return [json.loads(line[len(prefix):]) for line in log.splitlines()
            if line.startswith(prefix)]


def capacity_evidence(log, stage):
    cfg = configuration(stage)
    requested = {key: cfg[key] for key in
                 ('max_len', 'max_seq', 'max_prefill')}
    lines = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', log)
    require(len(lines) == 1, 'one effective capacity report required')
    fields = dict(re.findall(r'(\w+)=(\S+)', lines[0]))
    effective = {key: int(fields['effective_' + key]) for key in requested}
    reported = {key: int(fields['requested_' + key]) for key in requested}
    require(effective == reported == requested and
            fields.get('budget_enabled') == '1' and
            fields.get('budget_feasible') == 'true', 'capacity differs')
    capabilities = re.findall(
        r'\[q4t\]\[capabilities\] effective mtp=(\d+) '
        r'media_allowed=(\d+) vision_loaded=(\d+) max_seq=(\d+)', log)
    require(capabilities == [(str(int(cfg['mtp'])), '0', '0',
                              str(cfg['max_seq']))], 'effective mode differs')
    if cfg['mtp']:
        require(f'MTP loaded (k=3 max_seq={cfg["max_seq"]})' in log and
                'MTP load failed' not in log, 'MTP fallback is not accepted')
    else:
        require('MTP disabled; plain decode' in log, 'plain decode not confirmed')
    return dict(requested=requested, effective=effective,
                reported_requested=reported, matches_requested=True,
                budget_enabled=True, budget_feasible=True,
                effective_mtp=cfg['mtp'])


def physical_order_evidence(log, plan, complete=True):
    sizes = [row['bytes'] for row in plan['allocations']]
    expected = sorted(range(len(sizes)), key=lambda index: (-sizes[index], index))
    order = plan['physical_order']
    require(order['strategy'] == 'stable_descending_bytes' and
            order['indices'] == expected and
            all(type(index) is int for index in order['indices']),
            'physical order must be stable descending bytes')
    rows = records(log, '[reserve-trace] ')
    begins = [row for row in rows
              if row['event'] == 'alloc' and row['phase'] == 'begin']
    ends = [row for row in rows
            if row['event'] == 'alloc' and row['phase'] == 'end']
    require(len(ends) <= len(begins) <= len(sizes) and
            len(begins) - len(ends) <= 1, 'physical prefix is not serial')
    for phase in (begins, ends):
        require([row['index'] for row in phase] == expected[:len(phase)],
                'actual physical order differs from plan')
        for ordinal, row in enumerate(phase):
            require(row['physical_ordinal'] == ordinal and
                    row['bytes'] == sizes[row['index']],
                    'physical ordinal/size differs')
    for begin, end in zip(begins, ends):
        require(end['rc'] == 0 and end['ptr'] and
                begin['mono'] <= end['mono'] and begin['tid'] == end['tid'],
                'physical allocation completion failed')
    if complete:
        require(len(begins) == len(ends) == len(sizes),
                'physical allocation sequence incomplete')
    return dict(strategy=order['strategy'], planned=len(sizes),
                started=len(begins), completed=len(ends),
                complete=len(ends) == len(sizes),
                indices_sha256=hashlib.sha256(json.dumps(
                    expected, separators=(',', ':')).encode()).hexdigest())


def check_plan(log, stage, plan_path):
    cfg = configuration(stage)
    plan = read(plan_path)
    require(plan['schema'] == 1 and
            plan['derivation'] == 'configuration_tensor_metadata',
            'plan derivation differs')
    allocations = plan['allocations']
    require(allocations and plan['count'] == len(allocations), 'plan count')
    require(all(isinstance(row['name'], str) and row['name'] and
                type(row['bytes']) is int and row['bytes'] > 0
                for row in allocations), 'invalid plan allocation')
    require(len({row['name'] for row in allocations}) == len(allocations),
            'duplicate allocation name')
    require(plan['device_bytes'] == sum(row['bytes'] for row in allocations),
            'plan device byte sum differs')
    if stage in STAGES:
        physical_order_evidence(log, plan)
    for key, expected in cfg.items():
        if key != 'requests':
            require(plan['effective'][key] == expected, 'plan config: ' + key)
    require(plan['effective']['initial_scratch_capacity'] ==
            (cfg['mtp_k'] + 1 if cfg['mtp'] else 0), 'initial scratch differs')
    reserve = records(log, '[reserve-trace] ')
    events = [row for row in reserve if row['event'] == 'plan']
    require(len(events) == 1 and events[0]['plan_sha256'] == sha(plan_path) and
            events[0]['count'] == len(allocations) and
            events[0]['total_bytes'] == plan['device_bytes'],
            'reservation/derived-plan identity differs')
    complete = [row for row in reserve if row['event'] == 'startup_complete']
    require(len(complete) == 1, 'startup handoff boundary missing or duplicated')
    completion = complete[0]
    require(completion['status'] == 'PASS' and
            completion['plan_sha256'] == sha(plan_path) and
            completion['count'] == completion['allocated'] ==
            completion['consumed'] == completion['handed_live'] == len(allocations)
            and completion['freed'] == completion['unhanded_remaining'] ==
            completion['release_unknown'] == completion['backend_rc'] == 0 and
            completion['error'] == 'none', 'startup completion ledger failed')
    handoffs = [row for row in reserve if row['event'] == 'handoff']
    require(len(handoffs) == len(allocations) and
            [row['index'] for row in handoffs] == list(range(len(allocations)))
            and [row['bytes'] for row in handoffs] ==
            [row['bytes'] for row in allocations] and
            max(row['mono'] for row in handoffs) <= complete[0]['mono'],
            'startup handoffs incomplete at boundary')
    ready = log.find('[q4t] serving on port ')
    boundary = next((log.find(line) for line in log.splitlines()
                     if line.startswith('[reserve-trace] ') and
                     json.loads(line.split(' ', 1)[1])['event'] ==
                     'startup_complete'), -1)
    require(log.count('[q4t] serving on port ') == 1 and 0 <= boundary < ready,
            'startup reservation completed after ready')
    configured = records(log, '[startup-plan] ')
    require(len(configured) == 1 and configured[0]['event'] == 'configured' and
            configured[0]['plan_sha256'] == sha(plan_path) and
            configured[0]['count'] == plan['count'] and
            configured[0]['device_bytes'] == plan['device_bytes'] and
            configured[0]['effective'] == plan['effective'],
            'one configured startup-plan summary required')
    return dict(status='PASS', plan_sha256=sha(plan_path),
                count=len(allocations), device_bytes=plan['device_bytes'],
                effective=plan['effective'])


def reservation_evidence(log, plan_path):
    """Adapt the prior ledger solely for the new explicit handoff boundary."""
    plan = read(plan_path)
    if 'physical_order' in plan:
        physical_order_evidence(log, plan)
    adapted, sequence = [], 0
    reserve = records(log, '[reserve-trace] ')
    require([row['seq'] for row in reserve] == list(range(len(reserve))),
            'reservation sequence is not contiguous')
    require(sum(row['event'] == 'startup_complete' for row in reserve) == 1,
            'startup boundary count differs')
    for line in log.splitlines():
        if line.startswith('[reserve-trace] '):
            row = json.loads(line.split(' ', 1)[1])
            if row['event'] == 'startup_complete':
                continue
            row['seq'] = sequence
            sequence += 1
            line = '[reserve-trace] ' + json.dumps(row)
        adapted.append(line)
    return audit_success(adapted, sha(plan_path),
                         [row['bytes'] for row in plan['allocations']])


def retained_reservation(log, plan_path):
    reserve = records(log, '[reserve-trace] ')
    plan = read(plan_path)
    require(sum(row['event'] == 'plan' for row in reserve) == 1 and
            sum(row['event'] == 'prepared' for row in reserve) == 1,
            'request re-prepared the startup reservation')
    require(not any(row['event'] in ('free', 'summary') for row in reserve),
            'startup reservation released during request lifetime')
    handoffs = [row for row in reserve if row['event'] == 'handoff']
    require(len(handoffs) == plan['count'], 'request changed handoff count')
    return dict(plan_sha256=sha(plan_path), handed_live=len(handoffs),
                reserved_pointers=[row['ptr'] for row in handoffs])


def prepare_fixtures(run):
    source_run = CACHE_SOURCE / 'run'
    seal = read(CACHE_SOURCE / 'sealed-files.json')
    required = list((source_run / 'fixtures').rglob('*'))
    required += [source_run / name for name in ('hot.json', 'reference.json')]
    identities = {}
    for path in required:
        if path.is_file():
            relative = str(path.relative_to(CACHE_SOURCE))
            require(sha(path) == seal[relative], 'frozen input differs: ' + relative)
            identities[relative] = seal[relative]
    save(run / 'input-identities.json', dict(source=str(CACHE_SOURCE),
         seal_sha256=sha(CACHE_SOURCE / 'sealed-files.json'), inputs=identities))
    shutil.copytree(source_run / 'fixtures', run / 'fixtures')
    for name in ('hot.json', 'reference.json'):
        shutil.copy2(source_run / name, run / name)
    manifest = read(run / 'fixtures/manifest.json')
    lines = (run / 'fixtures/requests.jsonl').read_text().splitlines()
    require(len(manifest) == len(lines) == 1 and manifest[0]['length'] == 1024
            and manifest[0]['expected'] == '710003', 'frozen 1K fixture differs')
    prompt = json.loads(lines[0])['prompt']
    require(hashlib.sha256(prompt.encode()).hexdigest() ==
            manifest[0]['prompt_sha256'], 'fixture prompt identity differs')
    references = [row for row in read(run / 'reference.json')
                  if row['id'] == manifest[0]['id']]
    require(len(references) == 1 and references[0]['text'] == '710003' and
            references[0]['prompt_sha256'] == manifest[0]['prompt_sha256'],
            'quality reference differs')


def http_worker(stage):
    verify_build()
    cfg, run = configuration(stage), BASE / stage / 'run'
    out = run / 'normal/http'
    out.mkdir(parents=True, exist_ok=False)
    env = environment(stage, run, os.environ)
    command = server_command(stage, run)
    plan_path = run / 'startup-plan.json'
    unit = f'q4t-startup-{stage}-{time.time_ns()}.scope'
    save(out / 'server-command.json', dict(argv=command,
         effective_q4t_environment={k: v for k, v in env.items()
                                    if k.startswith('Q4T_')},
         isolation=dict(systemd_unit=unit, host_cache_max_bytes=HOST_LIMIT,
                        swap_max_bytes=0, is_total_physical_ram_limit=False)))
    save(run / 'normal/protocol.json', dict(mode='quality', **cfg,
         host_cache_max_bytes=HOST_LIMIT, swap_max_bytes=0,
         effective_environment={k: v for k, v in env.items()
                                if k.startswith('Q4T_')},
         cache_protocol='temporary_mlock_released_exact_full_hot',
         performance_acceptance=False))
    (out / 'binary.sha256').write_text(sha(BINARY) + '\n')
    shutil.copy2(BINARY.with_name('CMakeCache.txt'), out / 'CMakeCache.txt')
    server, results, failure, cleanup_failure = None, [], None, None
    log_path = out / 'server.log'
    log_path.touch(exist_ok=False)
    try:
        # This is the final gate after build/source checks, immediately before
        # launch. Preparation is deliberately re-established at each restart.
        final_cache_gate(run)
        event(out, 'server_before_start')
        server = IsolatedScope(command, cwd=ROOT, env=env, log_path=log_path,
                               unit=unit, memory_max=HOST_LIMIT,
                               evidence=out / 'isolation')
        (out / 'server.pid').write_text(str(server.pid) + '\n')
        event(out, 'server_started', pid=server.pid)
        deadline = time.monotonic() + 150
        while '[q4t] serving on port ' not in log_path.read_text():
            require(server.poll() is None, 'server exited during startup')
            require(time.monotonic() < deadline, 'startup timeout')
            time.sleep(.2)
        log = log_path.read_text()
        capacity = capacity_evidence(log, stage)
        save(out / 'capacity.json', capacity)
        plan_gate = check_plan(log, stage, plan_path)
        if stage == 'restart-lock-v3':
            require(sha(plan_path) == sha(BASE / 'mtp-lock-v3/run/startup-plan.json'),
                    'restart derived another startup plan')
        save(out / 'plan-ready.json', plan_gate)
        event(out, 'server_ready', pid=server.pid, capacity=capacity)
        original = retained_reservation(log, plan_path)
        retained = []
        expected = read(run / 'fixtures/manifest.json')[0]
        for number in range(cfg['requests']):
            case = out / ('quality' if number == 0 else 'quality-reuse')
            case.mkdir()
            inputs = case / 'requests.jsonl'
            shutil.copy2(run / 'fixtures/requests.jsonl', inputs)
            rows = run_case(out, case, inputs, 1, 1024, 1024, 32, True,
                            ROOT / 'tools/evalscope/.venv/bin/evalscope',
                            MODEL, port_for(stage), env, server,
                            False, capacity, 'quality')
            require(len(rows) == 1, 'one HTTP request required')
            row = rows[0]
            row.update(id=expected['id'], expected=expected['expected'],
                       request_index=number, exact_match=row['text'] == '710003')
            results.append(row)
            save(out / 'results.json', results)
            require(row['exact_match'] and row['actual_input'] == 1024 and
                    row['actual_output'] == 7 and row['finish'] == ['stop'] and
                    row['prompt_sha256'] == expected['prompt_sha256'],
                    'exact 1K HTTP quality failed')
            current = retained_reservation(log_path.read_text(), plan_path)
            require(current == original, 'reserved storage changed across request')
            retained.append(current)
            save(out / 'request-reservation-retention.json', retained)
        require(len({row['response_id'] for row in results}) == cfg['requests'],
                'repeated response identity')
    except BaseException as error:
        failure = repr(error)
    finally:
        event(out, 'server_before_shutdown', pid=server.pid if server else None)
        try:
            if server is not None:
                try:
                    server.terminate()
                    try:
                        server.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
                finally:
                    server.close()
        except BaseException as error:
            cleanup_failure = repr(error)
        event(out, 'server_after_shutdown',
              server_returncode=server.returncode if server else None,
              cleanup_failure=cleanup_failure)
        if failure is None and cleanup_failure is None:
            try:
                save(out / 'reservation-audit.json',
                     reservation_evidence(log_path.read_text(), plan_path))
            except BaseException as error:
                failure = repr(error)
        passed = (failure is None and cleanup_failure is None and
                  server is not None and server.returncode == 0 and
                  len(results) == cfg['requests'])
        save(out / 'exit.json', dict(server=server.returncode if server else None,
             completed=len(results), http_output_checks_passed=passed,
             failure=failure, cleanup_failure=cleanup_failure,
             performance_acceptance=False))
    return 0 if passed else 1


def verified_cache_reference():
    name = 'run/cache-expected.json'
    seal = read(CACHE_SOURCE / 'sealed-files.json')
    require(sha(CACHE_SOURCE / name) == seal[name], 'prior cache evidence changed')
    vector = read(CACHE_SOURCE / name)
    require(sum(vector.values()) == HOT_BYTES, 'prior full-hot bytes differ')
    return vector


def process_snapshot(run, name, who):
    value = observe_process(who['pid'], who['start_ticks'])
    save(run / (name + '-cgroup.json'), value)
    require(value['identity_valid'] and value['ancestors'],
            'preparation process/cgroup identity unknown')
    for row in value['ancestors']:
        # Linux root has no memory limit files; keep their absence explicit.
        if row['cgroup'] != '/':
            require(row['status'] == 'OBSERVED', 'nonroot cgroup data unknown')
    return value


def check_lock_budget(snapshot, total):
    require(total == HOT_BYTES, 'lock target differs from frozen full hot bytes')
    for row in snapshot['ancestors']:
        if row['cgroup'] == '/':
            continue
        fields = row['files']
        current = fields['memory.current']['value']
        for name in ('memory.max', 'memory.high'):
            limit = fields[name]['value']
            require(limit == 'max' or current + total <= limit,
                    'preparation cgroup has insufficient uncharged headroom')
    info = dict((line.split(':', 1)[0], int(line.split()[1]) * 1024)
                for line in Path('/proc/meminfo').read_text().splitlines())
    require(info['MemAvailable'] >= total and info['MemTotal'] > total,
            'available host memory below temporary lock target')
    return info


def final_cache_gate(run):
    """One locked preparation; no repair/retry after unlocking."""
    paths = model_files(MODEL)
    selected = sorted(MODEL.glob('layer-*-experts-*.safetensors'))
    expected = verified_cache_reference()
    require(expected_vector(paths, selected) == expected,
            'selected files differ from original exact hot vector')
    manifest = make_manifest(selected)
    require(manifest['total_lock_bytes'] == HOT_BYTES and
            all(expected[row['path']] == row['lock_bytes']
                for row in manifest['files']), 'manifest exceeds cache contract')
    save(run / 'cache-expected.json', expected)
    save(run / 'cache-before.json', observe_files(paths))
    errors, after, attempts = prepare_target_cache(paths)
    save(run / 'cache-preparation.json', dict(errors=errors, attempts=attempts))
    require(not errors and payload_residual(after) == 0,
            'zero payload prerequisite failed')
    parent = identity(os.getpid())
    snap = process_snapshot(run, 'preparation-parent-before', parent)
    save(run / 'lock-budget.json', check_lock_budget(snap, HOT_BYTES))
    helper = None
    try:
        helper = LockedWarm(run / 'locked-warm', manifest, timeout=120,
                            use_sudo=True)
        snapshot = process_snapshot(run, 'helper-ready', helper.identity)
        require(snapshot['cgroup_before']['value'] ==
                snap['cgroup_before']['value'], 'helper moved cgroup')
        check_lock_budget(snapshot, HOT_BYTES)
        helper.grant(use_sudo=True)
        helper.acquire()
        held_snapshot = process_snapshot(run, 'helper-held', helper.identity)
        require(held_snapshot['cgroup_before']['value'] ==
                snapshot['cgroup_before']['value'], 'helper moved cgroup')
        save(run / 'system-held.json', {p: Path(p).read_text() for p in
             ('/proc/meminfo', '/proc/vmstat', '/proc/buddyinfo')})
        held = observe_files(paths)
        save(run / 'cache-held.json', held)
        check_vector(held, expected)
        released = helper.release()
        require(released['release_success'] and released['reaped'] and
                released['returncode'] == 0,
                'temporary lock release unconfirmed')
    finally:
        if helper is not None:
            helper.close()
    reaped_mono = time.monotonic()
    after_parent = identity(os.getpid())
    require(after_parent['memlock'] == parent['memlock'] and
            after_parent['vmlck_bytes'] == parent['vmlck_bytes'] == 0,
            'preparation changed parent lock limit or retained locks')
    process_snapshot(run, 'preparation-parent-after', parent)
    save(run / 'system-ready.json', {p: Path(p).read_text() for p in
         ('/proc/meminfo', '/proc/vmstat', '/proc/buddyinfo')})
    scan_begin_mono = time.monotonic()
    final = observe_files(paths)
    scan_end_mono = time.monotonic()
    save(run / 'cache-launch.json', final)
    check_vector(final, expected)
    save(run / 'cache-launch-time.json', dict(
         lock_owner_reaped_mono=reaped_mono, scan_begin_mono=scan_begin_mono,
         scan_end_mono=scan_end_mono))
    save(run / 'cache-gate.json', dict(payload_bytes=HOT_BYTES,
         file_vector_exact=True, temporary_locks_released=True,
         physical_fragmentation_equal=False, repair_passes=0))


def worker(stage):
    verify_build()
    run = BASE / stage / 'run'
    run.mkdir(exist_ok=False)
    state = dict(status='RUNNING', started=stamp())
    save(run / 'state.json', state)
    def mark(name):
        with (run / 'milestones.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=name, **stamp())) + '\n')
    try:
        prepare_fixtures(run)
        # All imports/build checks finish before the sole preparation.
        command = [sys.executable, '-B', str(Path(__file__).resolve()),
                   '--stage', stage, '--http-worker']
        save(run / 'command.json', command)
        mark('runner_begin')
        with (run / 'normal.log').open('x') as log:
            rc = run_owned_runner(command, cwd=ROOT,
                 env=environment(stage, run, os.environ), log=log, timeout=280,
                 evidence=run / 'normal-process.json')
        mark('runner_end')
        state.update(status='COMPLETED' if rc == 0 else 'COMMAND_FAILED',
                     returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = stamp()
        save(run / 'state.json', state)


def sealed_attempt(name, base=BASE, collector_interruption=False):
    out = base / name
    seal = read(out / 'sealed-files.json')
    require({'state.json', 'lease/exit.json', 'build.json'} <= seal.keys(),
            'attempt required evidence not sealed')
    actual = {str(path.relative_to(out)) for path in out.rglob('*')
              if path.is_file() and path.name != 'sealed-files.json'}
    require(actual == set(seal), 'attempt file set changed')
    for relative, digest in seal.items():
        path = (out / relative).resolve()
        require(path.is_relative_to(out.resolve()) and sha(path) == digest,
                'attempt sealed evidence changed')
    events_path = out / 'run/normal/http/events.jsonl'
    events = ([json.loads(line) for line in events_path.read_text().splitlines()]
              if events_path.exists() else [])
    state = read(out / 'state.json')
    guard = read(out / 'lease/exit.json')
    if name in STAGES:
        kernel_ok = (state['collector_profile'] == COLLECTOR_PROFILE and
                     state['kernel_timeline'] == 'NOT_COLLECTED' and
                     'kernel' not in state and not (out / 'kernel').exists())
    else:
        kernel = state['kernel']['result']
        kernel_ok = (state['kernel']['returncode'] == 0 and
                     kernel['status'] == 'COMPLETED')
    if collector_interruption:
        require(name in INTERRUPTED and base == BASE and
                sha(out / 'sealed-files.json') == INTERRUPTED[name],
                'collector interruption is not the frozen failed attempt')
        expected_failure = (
            kernel['reason'] == 'CaptureFailure: trace byte limit reached' and
            kernel['max_bytes'] == kernel['trace_bytes'] == 536870912 and
            kernel['trace_unwritten_at_least'] > 0) if name == 'mtp-lock-v1' else (
            kernel['reason'] == 'CaptureFailure: trace events lost: '
            '{"overrun": 5501, "commit overrun": 0, "dropped events": 0}' and
            kernel['max_bytes'] == 2147483648 and
            kernel['loss_totals']['overrun'] == 5501)
        kernel_ok = (kernel['status'] == 'FAILED' and expected_failure and
            kernel['instance_removed']
            and kernel['global_configuration_equal'] and
            state['status'] == 'FAILED' and state['error'] ==
            "RuntimeError('collector exited during guarded stage')" and
            state['scope_final']['after']['ActiveState'] == 'inactive' and
            not state['lock_owner_cleanup']['live_lock_owner'])
    require(state['default_unchanged'] and kernel_ok and
            state['resources']['returncode'] == 0 and
            state['resources']['result']['status'] == 'COMPLETED',
            'prior attempt cleanup was not completed')
    return dict(name=name, status=state['status'], guard=guard['status'],
                candidate_sha256=read(out / 'build.json')['candidate_sha256'],
                model_starts=sum(row['event'] == 'server_started' for row in events),
                http_requests=sum(row['event'] == 'client_before' for row in events),
                manifest_sha256=sha(out / 'sealed-files.json'))


def previous_stage_gate(stage):
    index = STAGES.index(stage)
    require(not any((BASE / name).exists() for name in STAGES[index:]),
            'current/later attempt exists; no implicit retry')
    seal_path = HISTORY_BASE / 'final-evidence.json'
    require(sha(seal_path) == HISTORY_SEAL_SHA, 'historical final seal changed')
    seal = read(seal_path)
    for name in ['build.json'] + [name + '/sealed-files.json' for name in HISTORY]:
        require(sha(HISTORY_BASE / name) == seal[name],
                'historical identity differs from final seal: ' + name)
    history = [sealed_attempt(name, HISTORY_BASE) for name in HISTORY]
    old_sha = read(HISTORY_BASE / 'build.json')['candidate_sha256']
    for row in history:
        name = row['name']
        completed = name == 'plain-v1'
        expected = ('RUNTIME_COMPLETED' if completed else 'RUNTIME_NOT_ACCEPTED',
                    'COMPLETED' if completed else
                    'ENV_ABORT' if name == 'mtp-v3' else 'COMMAND_FAILED',
                    int(name in ('plain-v1', 'mtp-v3')), int(completed),
                    sha(BINARY) if name == 'mtp-order-v1' else old_sha)
        require(tuple(row[key] for key in ('status', 'guard', 'model_starts',
                    'http_requests', 'candidate_sha256')) == expected,
                'historical attempt differs: ' + name)
    for name in INTERRUPTED:
        interrupted = sealed_attempt(name, collector_interruption=True)
        require((interrupted['status'], interrupted['guard'],
                 interrupted['model_starts'], interrupted['http_requests'],
                 interrupted['candidate_sha256']) ==
                ('FAILED', 'ENV_ABORT', 1, 0, sha(BINARY)),
                'collector interruption history differs')
        history.append(interrupted)
    prior = [sealed_attempt(name) for name in STAGES[:index]]
    for row in prior:
        out = BASE / row['name'] / 'run/normal/http'
        require(row['status'] == 'RUNTIME_COMPLETED' and
                row['guard'] == 'COMPLETED' and row['model_starts'] == 1 and
                row['http_requests'] == configuration(row['name'])['requests']
                and row['candidate_sha256'] == sha(BINARY) and
                read(out / 'exit.json')['http_output_checks_passed'] and
                read(out / 'reservation-audit.json')['status'] == 'PASS' and
                read(out.parents[1] / 'cache-gate.json') == dict(
                    payload_bytes=HOT_BYTES, file_vector_exact=True,
                    temporary_locks_released=True,
                    physical_fragmentation_equal=False, repair_passes=0) and
                read(out.parents[1] / 'locked-warm/exit.json')['release_success'],
                'previous new stage did not pass')
    starts = sum(row['model_starts'] for row in history + prior)
    requests = sum(row['http_requests'] for row in history + prior)
    require(starts + 1 <= MODEL_START_LIMIT and
            requests + configuration(stage)['requests'] <= HTTP_LIMIT,
            'bounded model/HTTP budget exhausted')
    return dict(prior=history + prior, prior_model_starts=starts,
                prior_http_requests=requests, model_start_limit=MODEL_START_LIMIT,
                http_limit=HTTP_LIMIT, planned_model_starts=1,
                planned_http_requests=configuration(stage)['requests'],
                preparation_limit=5, prior_new_preparations=index + 2,
                collector_interruptions_retained=2,
                collector_profile=COLLECTOR_PROFILE,
                no_retry=True)


def require_guard(stage):
    """Internal workers may only run under this stage's live GPU supervisor."""
    path = common_lock()
    with path.open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise RuntimeError('internal worker requires a held GPU lease')
        lease = json.load(lock)
    require(lease.get('state') == 'held' and
            lease.get('output') == str(BASE / stage / 'lease'),
            'internal worker belongs to another lease')
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            break
        proc = Path('/proc') / str(pid)
        argv = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if str(ROOT / 'tools/evalscope/gpu_exclusive.py') in argv:
            require('--output' in argv and
                    argv[argv.index('--output') + 1] == str(BASE / stage / 'lease'),
                    'GPU guard ancestry belongs to another stage')
            return
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    raise RuntimeError('internal worker has no matching GPU guard ancestor')


def helper_cleanup_evidence(out):
    path = out / 'run/locked-warm/child-events.jsonl'
    if not path.exists():
        return dict(started=False, live_lock_owner=False)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    ready = [row for row in rows if row['event'] == 'ready']
    require(len(ready) <= 1, 'multiple lock owner identities')
    if not ready:
        require(not any(row['event'] in ('mmap', 'mlock') for row in rows),
                'lock owner started without birth identity')
        return dict(started=False, live_lock_owner=False)
    owner = ready[0]['identity']
    proc = Path('/proc') / str(owner['pid']) / 'stat'
    try:
        fields = proc.read_text().rsplit(')', 1)[1].split()
        alive = int(fields[19]) == owner['start_ticks']
    except FileNotFoundError:
        alive = False
    require(not alive, 'temporary lock owner has not exited')
    return dict(started=True, identity=owner, live_lock_owner=False)


def supervise(stage):
    build = verify_build()
    budget = previous_stage_gate(stage)
    out = BASE / stage
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(BUILD_MANIFEST, out / 'build.json')
    save(out / 'attempt-budget.json', budget)
    sources = set(HERE.glob('*.py')) | set(OBSERVE.glob('*.py'))
    for folder in ('tools/evalscope', 'tools/verify/device_reserve',
                   'tools/verify/vmm_compare', 'tools/verify/startup_plan'):
        sources.update((ROOT / folder).glob('*.py'))
    sources.update(Path(row['path']) for row in
                   build['source_and_plan_inputs'].values())
    identities = {}
    for source in sorted(sources):
        relative = source.resolve().relative_to(ROOT)
        target = out / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        identities[str(relative)] = sha(source)
    save(out / 'sources.json', identities)
    state = dict(status='RUNNING', started=stamp(), configuration=configuration(stage),
                 model_load_budget=1, http_budget=configuration(stage)['requests'],
                 guarded_work_seconds=300, default_sha=DEFAULT_SHA,
                 collector_profile=COLLECTOR_PROFILE,
                 kernel_timeline='NOT_COLLECTED',
                 observer_sha=build['candidate_sha256'],
                 cache_contract='temporary_mlock_released_exact_full_hot')
    save(out / 'state.json', state)
    guard, collectors, logs = None, [], []
    lock_path = common_lock()
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state['lease_acquired'] = stamp()
        recovery = lock_path.with_name(lock_path.name + '.recovery-required')
        require(not recovery.exists(), 'unfinished GPU recovery')
        recovery_errors = []
        try:
            verify_build()
            subprocess.run([sys.executable, '-B', str(OBSERVE / 'driver_identity.py'),
                            '--output', str(out / 'driver-identity')], check=True)
            require(read(out / 'driver-identity/driver_identity.json')
                    ['attribution_valid'], 'driver attribution unavailable')
            # High-volume kernel tracing is diagnostic research, not a
            # prerequisite for configuration/ownership/HTTP reliability.
            # Keep resource accounting and the original NVML lease mandatory.
            for name, script, extra in (
                    ('resources', 'resources.py', ['--binary', str(BINARY),
                                                  '--model-dir', str(MODEL)]),):
                folder, stop = out / name, out / ('stop-' + name)
                log = (out / (name + '-driver.log')).open('x')
                logs.append(log)
                proc = subprocess.Popen(['sudo', '-n', sys.executable, '-B',
                     str(OBSERVE / script), '--output', str(folder),
                     '--stop-file', str(stop), '--max-seconds', '360', *extra],
                     stdout=log, stderr=subprocess.STDOUT)
                collectors.append((name, proc, folder, stop))
                previous.wait_ready(proc, folder)
            command = [sys.executable, '-B', str(ROOT / 'tools/evalscope/gpu_exclusive.py'),
                       '--output', str(out / 'lease'), '--compute-backend', 'nvml',
                       '--lease-fd', str(lock.fileno()), '--', sys.executable,
                       '-B', str(Path(__file__).resolve()), '--stage', stage, '--worker']
            save(out / 'guard-command.json', command)
            log = (out / 'guard.log').open('x')
            logs.append(log)
            guard = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=(lock.fileno(),), start_new_session=True)
            deadline = time.monotonic() + 300
            while guard.poll() is None:
                require(all(proc.poll() is None for _, proc, _, _ in collectors),
                        'collector exited during guarded stage')
                require(time.monotonic() < deadline, 'bounded stage timeout')
                time.sleep(.1)
            state['guard_returncode'] = guard.returncode
            state['guard'] = read(out / 'lease/exit.json')
            require('cleanup_error' not in state['guard'], 'guard cleanup error')
            state['scope'] = previous.cleanup_scope(out)
            time.sleep(2)
            state['status'] = ('RUNTIME_COMPLETED' if guard.returncode == 0 and
                state['guard']['status'] == 'COMPLETED' else 'RUNTIME_NOT_ACCEPTED')
        except BaseException as error:
            state.update(status='FAILED', error=repr(error))
        finally:
            try:
                if guard is not None and guard.poll() is None:
                    guard.send_signal(signal.SIGTERM)
                    try:
                        guard.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        guard.kill()
                        guard.wait(timeout=5)
                        recovery_errors.append('guard exceeded cleanup grace')
                state['scope_final'] = previous.cleanup_scope(out)
            except BaseException as error:
                state.update(status='FAILED', cleanup_error=repr(error))
                recovery_errors.append(repr(error))
            try:
                state['lock_owner_cleanup'] = helper_cleanup_evidence(out)
            except BaseException as error:
                recovery_errors.append('temporary lock owner: ' + repr(error))
            for name, proc, folder, stop in collectors:
                stop.touch()
                try:
                    rc = proc.wait(timeout=15)
                    result = read(folder / 'exit.json')
                    state[name] = dict(returncode=rc, result=result)
                    require(rc == 0 and result['status'] == 'COMPLETED',
                            name + ' collector failed')
                    if name == 'kernel':
                        require(result['instance_removed'] and
                                result['global_configuration_equal'] and
                                not any(result['loss_totals'].values()),
                                'trace cleanup/configuration/loss gate failed')
                    else:
                        require(not result['failures'], 'resource observations failed')
                except BaseException as error:
                    state.update(status='FAILED', **{name + '_cleanup_error': repr(error)})
                    if proc.poll() is None or (name == 'kernel' and not
                            state.get(name, {}).get('result', {}).get('instance_removed')):
                        recovery_errors.append(name + ': cleanup unconfirmed')
            if recovery_errors:
                state.update(status='RECOVERY_REQUIRED', recovery_errors=recovery_errors)
                save(recovery, dict(output=str(out), errors=recovery_errors,
                     guard_pid=guard.pid if guard else None,
                     collectors=[dict(name=n, pid=p.pid) for n, p, _, _ in collectors]))
            for log in logs:
                log.close()
            state['default_unchanged'] = sha(ROOT / 'build/q4t') == DEFAULT_SHA
            if not state['default_unchanged']:
                state['status'] = 'FAILED'
            state['finished'] = stamp()
            save(out / 'state.json', state)
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(dict(state='recovery_required' if recovery_errors
                       else 'released', output=str(out))) + '\n')
            lock.flush()
    save(out / 'sealed-files.json', {str(p.relative_to(out)): sha(p)
         for p in out.rglob('*') if p.is_file()})
    print(json.dumps(dict(status=state['status'], output=str(out))))
    return 0 if state['status'] == 'RUNTIME_COMPLETED' else 1


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('startup-plan validation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, choices=STAGES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--worker', action='store_true')
    mode.add_argument('--http-worker', action='store_true')
    args = parser.parse_args()
    if args.worker or args.http_worker:
        require_guard(args.stage)
    if args.http_worker:
        return http_worker(args.stage)
    return worker(args.stage) if args.worker else supervise(args.stage)


if __name__ == '__main__':
    raise SystemExit(main())
