"""One bounded product-validation stage; no automatic retry or deployment.

Uses production lifecycle summaries, NVML and resources, never kernel tracing.
The sole locked-warm fixture is an extreme starting condition, not a product
prerequisite. Inherited stages only observe the naturally retained cache.
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
BASE = ROOT / '.q4t-work/startup-delivery-20261021'
CACHE_SOURCE = ROOT / '.q4t-work/device-reserve-20261020/reserve-v1'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
BUILD_MANIFEST = BASE / 'candidate-v1/build.json'
BINARY = BASE / 'candidate-v1/build/q4t'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
HOST_LIMIT = 64 << 30
HOT_BYTES = 67987791872
OBSERVE = ROOT / 'tools/verify/warm_reclaim'
sys.path[:0] = [str(ROOT / 'tools/evalscope'),
               str(ROOT / 'tools/verify/vmm_compare'),
               str(ROOT / 'tools/verify/startup_cache')]
from cache_start import check_vector, expected_vector
from cgroup_observer import observe_process
from locked_warm import LockedWarm, make_manifest, identity
from file_cache import model_files, observe_files
from gpu_exclusive import common_lock
from isolated_scope import IsolatedScope, scope_properties, stop_owned_scope
from offload_policy import BASE_ENVIRONMENT
from run_acceptance import event, run_case
from run_budget_experiment import (run_owned_runner, prepare_target_cache,
                                   payload_residual, clear_target_cache)

_spec = importlib.util.spec_from_file_location('delivery_build', HERE / 'build.py')
build_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_tools)
sha = build_tools.sha
STAGES = ('default-cold-v1', 'default-inherited-v1', 'c256-hot-v1',
          'c256-inherited-v1', 'boundary-v2', 'off-v1')
RETAINED_FAILURE = 'boundary-v1'
RETAINED_SEAL_SHA = 'd93e6e249fd6f8f370b69c43d401c015e756b0c64ac2c06fe480ab16e076c0cb'
RETAINED_AUDIT_SHA = 'ba445c7ac2506f16c0a3559f6687a398466394d6e0bee0a945beb78db63cc085'
DEPLOY_STAGES = ('deploy-mtp-v1', 'deploy-plain-v1')
COLLECTOR_PROFILE = 'resources_nvml_production_summary'
MODEL_START_LIMIT, HTTP_LIMIT = 6, 9


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def stamp():
    return dict(monotonic=time.monotonic(), wall=time.time())


def configuration(stage):
    require(stage in (*STAGES, *DEPLOY_STAGES), 'unknown bounded stage')
    deploy = stage in DEPLOY_STAGES
    default = stage.startswith('default-') or stage == 'off-v1' or deploy
    boundary = stage == 'boundary-v2'
    return dict(real_default=default, max_seq=2 if boundary else 1,
                max_prefill=0 if default else 2048 if boundary else 8192,
                max_len=0 if default else 8192 if boundary else 262144,
                resident_slots=0 if default else 128 if boundary else 256,
                mtp=not boundary and stage != 'deploy-plain-v1',
                startup_plan='off' if stage == 'off-v1' else 'auto',
                l2_slots=None if default else 1 if boundary else 16,
                mirror_k=None if default else 0 if boundary else 8,
                load_threads=None if default else 1 if boundary else 16,
                memory_max=None if default else HOST_LIMIT,
                cache='inherited' if 'inherited' in stage or deploy else
                      'full_hot' if stage == 'c256-hot-v1' else 'cold',
                requests=2 if stage in ('default-cold-v1', 'c256-hot-v1',
                                        'boundary-v2') else 1)


def port_for(stage):
    return 8850 + (*STAGES, *DEPLOY_STAGES).index(stage)


def environment(stage, run, inherited):
    cfg = configuration(stage)
    env = {k: v for k, v in inherited.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    if not cfg['real_default']:
        env.update(BASE_ENVIRONMENT)
        env.update(Q4T_MOE_CHUNK_ORDER='0',
                   Q4T_MOE_L2_SLOTS=str(cfg['l2_slots']),
                   Q4T_MOE_MIRROR_K=str(cfg['mirror_k']),
                   Q4T_MOE_LOAD_THREADS=str(cfg['load_threads']))
    return env


def server_command(stage, run, *, binary=None):
    cfg = configuration(stage)
    command = [str(binary or BINARY), 'serve', '--model-dir', str(MODEL),
               '--port', str(port_for(stage))]
    if not cfg['real_default']:
        command += ['--max-seq', str(cfg['max_seq']),
                    '--max-prefill', str(cfg['max_prefill']),
                    '--max-len', str(cfg['max_len']),
                    '--moe-resident-slots', str(cfg['resident_slots']),
                    '--moe-hot-list', str(run / 'hot.json')]
    if not cfg['mtp']:
        command.append('--no-mtp')
    if cfg['startup_plan'] == 'off':
        command += ['--startup-plan', 'off']
    return command


def verify_build():
    build_tools.verify(BUILD_MANIFEST)
    build = read(BUILD_MANIFEST)
    require(build_tools.check_identity(build['candidate']) == BINARY.resolve(),
            'candidate path differs')
    return build


def records(log, prefix):
    return [json.loads(line[len(prefix):]) for line in log.splitlines()
            if line.startswith(prefix)]


def capacity_evidence(log, stage):
    cfg = configuration(stage)
    lines = re.findall(r'\[q4t\]\[capacity\] ([^\n]+)', log)
    require(len(lines) == 1, 'one effective capacity report required')
    fields = dict(re.findall(r'(\w+)=(\S+)', lines[0]))
    requested = {k: cfg[k] for k in ('max_len', 'max_seq', 'max_prefill')}
    reported = {k: int(fields['requested_' + k]) for k in requested}
    effective = {k: int(fields['effective_' + k]) for k in requested}
    require(requested == reported and fields.get('budget_enabled') == '1' and
            fields.get('budget_feasible') == 'true', 'requested capacity differs')
    if cfg['real_default']:
        require(1056 <= effective['max_len'] <= 262144 and
                effective['max_seq'] == 1 and effective['max_prefill'] == 2048,
                'actual default capacity differs')
    else:
        require(effective == requested, 'explicit capacity was capped')
    capabilities = re.findall(
        r'\[q4t\]\[capabilities\] effective mtp=(\d+) '
        r'media_allowed=(\d+) vision_loaded=(\d+) max_seq=(\d+)', log)
    require(capabilities == [(str(int(cfg['mtp'])), '0', '0',
                              str(cfg['max_seq']))], 'effective mode differs')
    if cfg['mtp']:
        require(f'MTP loaded (k=3 max_seq={cfg["max_seq"]})' in log and
                'MTP load failed' not in log, 'MTP fallback not accepted')
    else:
        require('MTP disabled; plain decode' in log, 'plain decode not confirmed')
    return dict(requested=requested, reported_requested=reported,
                effective=effective, budget_enabled=True, budget_feasible=True,
                effective_mtp=cfg['mtp'], real_default=cfg['real_default'])


def reservation_evidence(log, stage, finalized=False):
    cfg = configuration(stage)
    rows = records(log, '[startup-reservation] ')
    selection = re.findall(r'\[q4t\]\[startup-plan\] ([^\n]+)', log)
    require(selection == [f'mode={cfg["startup_plan"]} active={int(cfg["startup_plan"] != "off")}'],
            'startup plan selection differs or silently fell back')
    require('[reserve-trace]' not in log and '[memory-trace]' not in log,
            'production executable emitted research API traces')
    if cfg['startup_plan'] == 'off':
        require(not rows, 'off mode activated the reservation')
        return dict(active=False, mode='off', events=[])
    wanted = ['prepared', 'startup_complete'] + (['finalized'] if finalized else [])
    require([r['event'] for r in rows] == wanted, 'reservation lifecycle differs')
    first = rows[0]
    require(type(first['count']) is int and first['count'] > 0 and
            type(first['device_bytes']) is int and first['device_bytes'] > 0 and
            re.fullmatch('[0-9a-f]{64}', first['plan_sha256']) is not None,
            'reservation plan identity invalid')
    for row in rows:
        require(row['schema'] == 1 and row['status'] == 'PASS' and
                row['strategy'] == 'stable_descending_bytes' and
                all(row[k] == first[k] for k in ('count', 'device_bytes',
                                               'plan_sha256')) and
                row['allocated'] == row['count'] and row['error'] == 'none' and
                row['backend_rc'] == row['release_unknown'] == 0 and row['prepared'],
                'reservation ledger failure or changed identity')
    n = first['count']
    require(first['consumed'] == first['freed'] == first['handed_live'] == 0 and
            first['unhanded_remaining'] == n and first['active'] and
            not first['startup_complete'] and not first['finalized'],
            'prepared state differs')
    complete = rows[1]
    require(complete['consumed'] == complete['handed_live'] == n and
            complete['freed'] == complete['unhanded_remaining'] == 0 and
            complete['active'] and complete['startup_complete'] and
            not complete['finalized'], 'startup completion differs')
    ready = log.find('[q4t] serving on port ')
    complete_at = next(log.find(line) for line in log.splitlines()
        if line.startswith('[startup-reservation] ') and
        json.loads(line.split(' ', 1)[1])['event'] == 'startup_complete')
    require(log.count('[q4t] serving on port ') == 1 and 0 <= complete_at < ready,
            'reservation incomplete before ready')
    if finalized:
        last = rows[-1]
        require(last['consumed'] == last['freed'] == n and
                last['unhanded_remaining'] == last['handed_live'] == 0 and
                not last['active'] and last['startup_complete'] and last['finalized'],
                'normal shutdown did not release all handed storage')
    return dict(active=True, count=n, device_bytes=first['device_bytes'],
                plan_sha256=first['plan_sha256'], events=wanted,
                finalized=finalized, status='PASS')


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

def full_hot_gate(run):
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
    errors, after, attempts = prepare_target_cache(paths, max_passes=1)
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

def wait_ready(proc, folder):
    deadline = time.monotonic() + 15
    while not (folder / 'ready.json').exists():
        if proc.poll() is not None:
            raise RuntimeError(f'collector exited before ready: {folder}')
        if time.monotonic() >= deadline:
            raise TimeoutError(f'collector ready deadline: {folder}')
        time.sleep(.05)

def cleanup_scope(out):
    spec = out / 'run/normal/http/isolation/spec.json'
    if not spec.exists():
        return dict(scope_created=False)
    owned = json.loads(spec.read_text())
    launch = json.loads(spec.with_name('launch.json').read_text())
    unit = launch['unit']
    result = dict(unit=unit, before=scope_properties(unit))
    if result['before'].get('LoadState') != 'not-found':
        result['cleanup'] = stop_owned_scope(unit, owned['token'])
    result['after'] = scope_properties(unit)
    save(out / 'supervisor-cleanup.json', result)
    if result['after'].get('LoadState') != 'not-found':
        raise RuntimeError('owned scope remains')
    return result


def cold_residual_files(observation, paths):
    require(observation['complete_file_set_observed'] and not observation['errors'],
            'cold cache observation incomplete')
    rows = observation['files']
    require(len(rows) == len({row['path'] for row in rows}) and
            {row['path'] for row in rows} == {str(path) for path in paths} and
            all(type(row['resident_bytes']) is int and row['resident_bytes'] >= 0
                for row in rows), 'cold cache file set/residency unknown')
    return [Path(row['path']) for row in rows if row['resident_bytes'] > 0 and
            Path(row['path']).suffix in ('.safetensors', '.bin')]


def prepare_cold_cache(paths, run):
    """Two bounded advice passes; the second touches only observed residue."""
    attempts, advised = [], list(paths)
    for number in (1, 2):
        errors = clear_target_cache(advised)
        observation = observe_files(paths)
        attempt = dict(pass_number=number, advised_files=list(map(str, advised)),
                       advice_errors=errors, observation=observation,
                       payload_resident_bytes=None)
        attempts.append(attempt)
        try:
            remaining = cold_residual_files(observation, paths)
            attempt['payload_resident_bytes'] = payload_residual(observation)
        finally:
            save(run / 'cache-preparation.json', dict(errors=errors, attempts=attempts))
        require(not errors, 'cold cache advice failed')
        if not remaining:
            break
        advised = remaining
        if number == 1:
            time.sleep(.1)
    return attempts


def cache_gate(stage, run):
    """One gate attempt; cold alone allows the frozen residual-only pass."""
    mode = configuration(stage)['cache']
    if mode == 'full_hot':
        full_hot_gate(run)
        gate = read(run / 'cache-gate.json')
        gate['mode'] = mode
        save(run / 'cache-gate.json', gate)
        return
    paths = model_files(MODEL)
    begin = time.monotonic()
    before = observe_files(paths)
    save(run / 'cache-before.json', before)
    require(before['complete_file_set_observed'] and not before['errors'],
            'initial cache observation incomplete')
    attempts = []
    if mode == 'cold':
        cold_residual_files(before, paths)
        attempts = prepare_cold_cache(paths, run)
    else:
        require(mode == 'inherited', 'unknown cache mode')
    scan_begin = time.monotonic()
    final = observe_files(paths)
    scan_end = time.monotonic()
    save(run / 'cache-launch.json', final)
    require(final['complete_file_set_observed'] and not final['errors'],
            'launch cache observation incomplete')
    if mode == 'cold':
        cold_residual_files(final, paths)
        require(payload_residual(final) == 0, 'cold payload vector is nonzero')
    save(run / 'cache-launch-time.json', dict(preparation_begin_mono=begin,
         scan_begin_mono=scan_begin, scan_end_mono=scan_end))
    save(run / 'cache-gate.json', dict(mode=mode,
         payload_bytes=payload_residual(final), file_vector_exact=mode == 'cold',
         naturally_inherited=mode == 'inherited', repair_passes=0,
         preparation_passes=len(attempts),
         max_preparation_passes=2 if mode == 'cold' else 0,
         residual_retry_files=attempts[1]['advised_files'] if len(attempts) == 2 else [],
         physical_fragmentation_equal=False, temporary_locks_used=False))


def http_worker(stage, *, run=None, binary=None, verify_callback=None):
    """Own one model scope and fixed requests, called only under a GPU guard.

    Deployment callers supply their own read-only identity verifier and binary;
    they own the outer lease/resource collector and the stage evidence seal.
    """
    verifier = verify_callback or verify_build
    verifier()
    cfg = configuration(stage)
    binary = Path(binary or BINARY).resolve()
    run = run or BASE / stage / 'run'
    out = run / 'normal/http'
    out.mkdir(parents=True, exist_ok=False)
    prepare_fixtures(run)
    env = environment(stage, run, os.environ)
    command = server_command(stage, run, binary=binary)
    unit = f'q4t-delivery-{stage}-{time.time_ns()}.scope'
    save(out / 'server-command.json', dict(argv=command,
         effective_q4t_environment={k: v for k, v in env.items() if k.startswith('Q4T_')},
         isolation=dict(systemd_unit=unit, host_cache_max_bytes=cfg['memory_max'],
                        swap_max_bytes=0, is_total_physical_ram_limit=False)))
    save(run / 'normal/protocol.json', dict(mode='quality', **cfg,
         host_cache_max_bytes=cfg['memory_max'], swap_max_bytes=0,
         effective_environment={k: v for k, v in env.items() if k.startswith('Q4T_')},
         performance_acceptance=False))
    (out / 'binary.sha256').write_text(sha(binary) + '\n')
    server, results, failure, cleanup_failure = None, [], None, None
    logging_errors = []
    log_path = out / 'server.log'
    log_path.touch(exist_ok=False)
    try:
        # All imports, fixture checks and identity reads precede the single
        # cache gate. No payload access is inserted between gate and launch.
        cache_gate(stage, run)
        event(out, 'server_before_start')
        server = IsolatedScope(command, cwd=ROOT, env=env, log_path=log_path,
                               unit=unit, memory_max=cfg['memory_max'],
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
        ready = reservation_evidence(log, stage)
        save(out / 'summary-ready.json', ready)
        predecessor = {'default-inherited-v1': 'default-cold-v1',
                       'c256-inherited-v1': 'c256-hot-v1'}.get(stage)
        if predecessor:
            previous = read(BASE / predecessor / 'run/normal/http/summary-ready.json')
            require(ready == previous, 'same-configuration restart plan differs')
            previous_capacity = read(BASE / predecessor / 'run/normal/http/capacity.json')
            require(capacity == previous_capacity, 'same-configuration capacity differs')
        event(out, 'server_ready', pid=server.pid, capacity=capacity)
        expected = read(run / 'fixtures/manifest.json')[0]
        retained = []
        for number in range(cfg['requests']):
            case = out / ('quality' if number == 0 else 'quality-reuse')
            case.mkdir()
            inputs = case / 'requests.jsonl'
            shutil.copy2(run / 'fixtures/requests.jsonl', inputs)
            rows = run_case(out, case, inputs, 1, 1024, 1024, 32, True,
                            ROOT / 'tools/evalscope/.venv/bin/evalscope',
                            MODEL, port_for(stage), env, server, False, capacity, 'quality')
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
            current = reservation_evidence(log_path.read_text(), stage)
            require(current == ready, 'startup plan changed across request')
            retained.append(current)
            save(out / 'request-reservation-retention.json', retained)
        require(len({row['response_id'] for row in results}) == cfg['requests'],
                'repeated response identity')
    except BaseException as error:
        failure = repr(error)
    finally:
        # Evidence write failures must not skip scope termination/reaping.
        try:
            event(out, 'server_before_shutdown', pid=server.pid if server else None)
        except BaseException as error:
            logging_errors.append(repr(error))
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
        try:
            event(out, 'server_after_shutdown',
                  server_returncode=server.returncode if server else None,
                  cleanup_failure=cleanup_failure)
        except BaseException as error:
            logging_errors.append(repr(error))
        if failure is None and cleanup_failure is None and not logging_errors:
            try:
                save(out / 'summary-final.json',
                     reservation_evidence(log_path.read_text(), stage, finalized=True))
                verifier()
            except BaseException as error:
                failure = repr(error)
        passed = (failure is None and cleanup_failure is None and not logging_errors
                  and server is not None and server.returncode == 0 and
                  len(results) == cfg['requests'])
        save(out / 'exit.json', dict(server=server.returncode if server else None,
             completed=len(results), http_output_checks_passed=passed,
             failure=failure, cleanup_failure=cleanup_failure,
             logging_errors=logging_errors, performance_acceptance=False))
    return 0 if passed else 1


def worker(stage):
    run = BASE / stage / 'run'
    run.mkdir(exist_ok=False)
    state = dict(status='RUNNING', started=stamp())
    save(run / 'state.json', state)
    try:
        verify_build()
        command = [sys.executable, '-B', str(Path(__file__).resolve()),
                   '--stage', stage, '--http-worker']
        save(run / 'command.json', command)
        with (run / 'normal.log').open('x') as log:
            rc = run_owned_runner(command, cwd=ROOT,
                 env=environment(stage, run, os.environ), log=log, timeout=280,
                 evidence=run / 'normal-process.json')
        state.update(status='COMPLETED' if rc == 0 else 'COMMAND_FAILED', returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = stamp()
        save(run / 'state.json', state)


def sealed_attempt(stage):
    out = BASE / stage
    seal = read(out / 'sealed-files.json')
    actual = {str(p.relative_to(out)) for p in out.rglob('*')
              if p.is_file() and p.name != 'sealed-files.json'}
    require(actual == set(seal), 'sealed stage file set differs')
    for relative, digest in seal.items():
        path = (out / relative).resolve(strict=True)
        require(path.is_relative_to(out.resolve()) and sha(path) == digest,
                'sealed stage changed: ' + relative)
    state, guard = read(out / 'state.json'), read(out / 'lease/exit.json')
    audit = read(BASE / ('audit-' + stage + '.json'))
    require(audit['runtime_accepted'] and audit['sealed_manifest_sha256'] ==
            sha(out / 'sealed-files.json'), 'prior independent audit not accepted')
    events = [json.loads(line) for line in
              (out / 'run/normal/http/events.jsonl').read_text().splitlines()]
    starts = sum(row['event'] == 'server_started' for row in events)
    requests = sum(row['event'] == 'client_before' for row in events)
    require(state['status'] == 'RUNTIME_COMPLETED' and guard['status'] == 'COMPLETED'
            and state['guard_returncode'] == 0 and starts == 1 and
            requests == configuration(stage)['requests'], 'prior stage failed')
    return dict(name=stage, model_starts=starts, http_requests=requests,
                manifest_sha256=sha(out / 'sealed-files.json'),
                candidate_sha256=read(out / 'build.json')['candidate_sha256'])


def retained_failure():
    """Only the sealed zero-start fixture failure permits the v2 continuation."""
    out = BASE / RETAINED_FAILURE
    seal_path, audit_path = out / 'sealed-files.json', BASE / 'audit-boundary-v1.json'
    require(sha(seal_path) == RETAINED_SEAL_SHA and
            sha(audit_path) == RETAINED_AUDIT_SHA, 'retained failure identity differs')
    seal = read(seal_path)
    actual = {str(p.relative_to(out)) for p in out.rglob('*')
              if p.is_file() and p.name != 'sealed-files.json'}
    require(actual == set(seal), 'retained failure file set differs')
    for relative, digest in seal.items():
        path = (out / relative).resolve(strict=True)
        require(path.is_relative_to(out.resolve()) and sha(path) == digest,
                'retained failure evidence differs')
    receipt = read(audit_path)
    result = receipt['stages'][RETAINED_FAILURE]
    candidate = sha(BINARY)
    require(receipt['runtime_accepted'] is False and not receipt['errors'] and
            result['runtime_accepted'] is False and not result['errors'] and
            result['model_starts'] == result['http_requests'] == 0 and
            result['preparations'] == 1 and result['sealed_manifest_sha256'] ==
            RETAINED_SEAL_SHA and receipt['candidate_sha256'] == candidate and
            read(out / 'build.json')['candidate_sha256'] == candidate,
            'retained failure was accepted or belongs to another candidate')
    state, guard = read(out / 'state.json'), read(out / 'lease/exit.json')
    owned = read(out / 'run/normal-process.json')
    http = read(out / 'run/normal/http/exit.json')
    events = [json.loads(line) for line in
              (out / 'run/normal/http/events.jsonl').read_text().splitlines()]
    require(state['status'] == 'RUNTIME_NOT_ACCEPTED' and state['default_unchanged']
            and state['guard'] == guard and guard['status'] == 'COMMAND_FAILED'
            and guard['child_returncode'] == state['guard_returncode'] == 1 and
            'cleanup_error' not in guard and guard['finished'] and
            state['scope'] == state['scope_final'] == dict(scope_created=False)
            and state['lock_owner_cleanup'] == dict(started=False, live_lock_owner=False)
            and owned['runner_reaped'] and owned['cleanup_complete'] and
            not owned['after_cleanup']['live_pids'] and
            not owned['after_cleanup']['errors'] and
            not any(row['event'] in ('server_before_start', 'server_started',
                'server_ready', 'client_before', 'client_after') for row in events)
            and not (out / 'run/normal/http/isolation').exists() and
            not (out / 'run/normal/http/server.log').read_text(),
            'retained failure launched work or left unknown cleanup')
    resources = read(out / 'resources/exit.json')
    require(state['resources'] == dict(returncode=0, result=resources) and
            resources['status'] == 'COMPLETED' and not resources['truncated']
            and not resources['failures'] and http['server'] is None and
            http['completed'] == 0 and http['http_output_checks_passed'] is False
            and http['failure'] == "RuntimeError('cold payload vector is nonzero')"
            and http['cleanup_failure'] is None and not http['logging_errors'],
            'retained failure is not the known clean pre-launch cache rejection')
    return dict(name=RETAINED_FAILURE, model_starts=0, http_requests=0,
                preparations=1, manifest_sha256=RETAINED_SEAL_SHA,
                audit_sha256=RETAINED_AUDIT_SHA, candidate_sha256=candidate)


def previous_stage_gate(stage):
    index = STAGES.index(stage)
    require(not any((BASE / name).exists() for name in STAGES[index:]),
            'stage/later attempt already exists; no implicit retry')
    require(not any(path.name not in (RETAINED_FAILURE, 'boundary-v2')
                    for path in BASE.glob('boundary-v*') if path.is_dir()),
            'unapproved additional boundary attempt exists')
    prior = [sealed_attempt(name) for name in STAGES[:index]]
    failures = [retained_failure()] if index >= 4 else []
    require(all(row['candidate_sha256'] == sha(BINARY) for row in prior),
            'candidate changed between matrix stages')
    starts = sum(row['model_starts'] for row in prior)
    requests = sum(row['http_requests'] for row in prior)
    require(starts + 1 <= MODEL_START_LIMIT and
            requests + configuration(stage)['requests'] <= HTTP_LIMIT and
            len(prior) + len(failures) + 1 <= 7,
            'candidate model/HTTP budget exhausted')
    return dict(prior=prior, retained_failures=failures,
                prior_preparations=len(prior) + len(failures),
                prior_model_starts=starts, prior_http_requests=requests,
                model_start_limit=MODEL_START_LIMIT, http_limit=HTTP_LIMIT,
                planned_model_starts=1,
                planned_http_requests=configuration(stage)['requests'],
                preparation_limit=7, full_hot_preparation_limit=1, no_retry=True,
                cold_pass_limit=2, cold_retry_policy='observed_nonzero_files_only')


def seal_stage(out):
    require(not (out / 'sealed-files.json').exists(), 'stage already sealed')
    save(out / 'sealed-files.json', {str(p.relative_to(out)): sha(p)
         for p in out.rglob('*') if p.is_file()})


def guard_cleanup_receipt(out):
    receipt = read(out / 'lease/exit.json')
    require('cleanup_error' not in receipt,
            'GPU guard descendant cleanup is unconfirmed: ' +
            str(receipt.get('cleanup_error')))
    require(receipt.get('finished') is not None,
            'GPU guard completion receipt is incomplete')
    return receipt


def supervise(stage):
    build, budget = verify_build(), previous_stage_gate(stage)
    out = BASE / stage
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(BUILD_MANIFEST, out / 'build.json')
    save(out / 'attempt-budget.json', budget)
    sources = set()
    for folder in (HERE, OBSERVE, ROOT / 'tools/evalscope',
                   ROOT / 'tools/verify/vmm_compare', ROOT / 'tools/verify/startup_cache',
                   ROOT / 'tools/verify/startup_plan'):
        sources.update(folder.glob('*.py'))
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
                 collector_profile=COLLECTOR_PROFILE, kernel_timeline='NOT_COLLECTED',
                 observer_sha=build['candidate_sha256'],
                 cache_contract=configuration(stage)['cache'])
    save(out / 'state.json', state)
    guard, collectors, logs, recovery_errors = None, [], [], []
    lock_path = common_lock()
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+') as lock:
        recovery = lock_path.with_name(lock_path.name + '.recovery-required')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state.update(status='ENV_ABORT', error='GPU lease contention',
                         default_unchanged=sha(ROOT / 'build/q4t') == DEFAULT_SHA,
                         finished=stamp(), no_owned_processes_started=True)
            save(out / 'state.json', state)
            seal_stage(out)
            return 75
        if recovery.exists():
            state.update(status='RECOVERY_REQUIRED',
                         error='unfinished GPU recovery', finished=stamp(),
                         no_owned_processes_started=True,
                         default_unchanged=sha(ROOT / 'build/q4t') == DEFAULT_SHA)
            save(out / 'state.json', state)
            seal_stage(out)
            return 75
        state['lease_acquired'] = stamp()
        try:
            verify_build()
            folder, stop = out / 'resources', out / 'stop-resources'
            log = (out / 'resources-driver.log').open('x')
            logs.append(log)
            proc = subprocess.Popen(['sudo', '-n', sys.executable, '-B',
                str(OBSERVE / 'resources.py'), '--output', str(folder),
                '--stop-file', str(stop), '--max-seconds', '360',
                '--binary', str(BINARY), '--model-dir', str(MODEL)],
                stdout=log, stderr=subprocess.STDOUT)
            collectors.append(('resources', proc, folder, stop))
            wait_ready(proc, folder)
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
                require(all(p.poll() is None for _, p, _, _ in collectors),
                        'resource collector exited during guarded stage')
                require(time.monotonic() < deadline, 'bounded stage timeout')
                time.sleep(.1)
            state['guard_returncode'] = guard.returncode
            state['guard'] = read(out / 'lease/exit.json')
            require('cleanup_error' not in state['guard'], 'guard cleanup error')
            state['scope'] = cleanup_scope(out)
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
            except BaseException as error:
                state.update(status='FAILED', cleanup_error=repr(error))
                recovery_errors.append(repr(error))
            try:
                if guard is not None:
                    state['guard_returncode'] = guard.returncode
                    state['guard'] = guard_cleanup_receipt(out)
            except BaseException as error:
                state.update(status='FAILED', guard_receipt_error=repr(error))
                recovery_errors.append('GPU guard: ' + repr(error))
            try:
                state['scope_final'] = cleanup_scope(out)
            except BaseException as error:
                state.update(status='FAILED', scope_cleanup_error=repr(error))
                recovery_errors.append('model scope: ' + repr(error))
            try:
                state['lock_owner_cleanup'] = helper_cleanup_evidence(out)
            except BaseException as error:
                recovery_errors.append('temporary lock owner: ' + repr(error))
            for name, proc, folder, stop in collectors:
                try:
                    stop.touch()
                    rc = proc.wait(timeout=15)
                    result = read(folder / 'exit.json')
                    state[name] = dict(returncode=rc, result=result)
                    require(rc == 0 and result['status'] == 'COMPLETED' and
                            not result['failures'] and not result['truncated'],
                            'resource observations failed')
                except BaseException as error:
                    state.update(status='FAILED', resources_cleanup_error=repr(error))
                    if proc.poll() is None:
                        recovery_errors.append('resource collector cleanup unconfirmed')
            if recovery_errors:
                state.update(status='RECOVERY_REQUIRED', recovery_errors=recovery_errors)
                save(recovery, dict(output=str(out), errors=recovery_errors,
                     guard_pid=guard.pid if guard else None,
                     collectors=[dict(name=n, pid=p.pid) for n, p, _, _ in collectors]))
            for log in logs:
                try:
                    log.close()
                except BaseException as error:
                    state.update(status='FAILED', log_close_error=repr(error))
            try:
                verify_build()
                state['default_unchanged'] = True
            except BaseException as error:
                state.update(status='FAILED', default_unchanged=False,
                             identity_error=repr(error))
            state['finished'] = stamp()
            save(out / 'state.json', state)
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(dict(state='recovery_required' if recovery_errors
                       else 'released', output=str(out))) + '\n')
            lock.flush()
    seal_stage(out)
    print(json.dumps(dict(status=state['status'], output=str(out))))
    return 0 if state['status'] == 'RUNTIME_COMPLETED' else 1


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('startup delivery validation interrupted')
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
