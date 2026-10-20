"""Three bounded ordered-reservation stages; preserve every earlier attempt.

Each stage owns one uninterrupted GPU lease, re-establishes the same complete
expert-file cache vector, and runs real evalscope HTTP inside a 64 GiB scope.
The first default-MTP stage sends two requests to the same model instance.
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
BASE = ROOT / '.q4t-work/startup-plan-20261020'
CACHE_SOURCE = ROOT / '.q4t-work/device-reserve-20261020/reserve-v1'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
BUILD_MANIFEST = BASE / 'order-candidate/build.json'
BINARY = BASE / 'order-candidate/build/q4t'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
HOST_LIMIT = 64 << 30
HOT_BYTES = 67987791872
sys.path[:0] = [str(ROOT / 'tools/evalscope'),
               str(ROOT / 'tools/verify/vmm_compare'),
               str(ROOT / 'tools/verify/device_reserve')]
from cache_start import check_vector, prepare
from file_cache import model_files, observe_files
from gpu_exclusive import common_lock
from isolated_scope import IsolatedScope
from offload_policy import BASE_ENVIRONMENT
from reservation_audit import audit_success
from run_acceptance import event, run_case
from run_budget_experiment import run_owned_runner

# Reuse ownership/collector helpers, without changing the previous driver.
_spec = importlib.util.spec_from_file_location(
    'previous_device_reserve_driver', ROOT / 'tools/verify/device_reserve/run.py')
previous = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(previous)
sha, stamp = previous.sha, previous.stamp
OBSERVE = previous.OBSERVE
STAGES = ('mtp-order-v1', 'restart-order-v1', 'boundary-order-v1')
HISTORY = ('plain-v1', 'mtp-v1', 'mtp-v2', 'mtp-v3')
LEGACY = (*HISTORY, 'restart-v1', 'boundary-v1')
MODEL_START_LIMIT = 5
HTTP_LIMIT = 5


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
    return BASE / 'build/q4t' if stage in LEGACY else BINARY


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
         cache_protocol='re-established exact full-hot vector',
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
        if stage == 'restart-order-v1':
            require(sha(plan_path) == sha(BASE / 'mtp-order-v1/run/startup-plan.json'),
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


def repair_missing_files(observation, expected):
    """Classify a single bounded repair; never accept extra/unknown pages."""
    require(observation['complete_file_set_observed'], 'unknown cache state')
    actual = {row['path']: row['resident_bytes'] for row in observation['files']
              if Path(row['path']).suffix in ('.safetensors', '.bin')}
    require(actual.keys() == expected.keys() and
            all(actual[path] <= expected[path] for path in expected),
            'extra or unobserved cache pages cannot be repaired')
    missing = [Path(path) for path in expected if actual[path] < expected[path]]
    require(len(missing) <= 2 and
            sum(path.stat().st_size for path in missing) <= 1 << 30,
            'final cache repair exceeds two files / 1GiB')
    return missing


def final_cache_gate(run):
    # Imports and build identity checks are already finished in this worker.
    # Allocate the read buffer before observing, then perform at most one
    # repair of small holes. The required final vector is still exact.
    buffer = bytearray(4 << 20)
    paths, expected = model_files(MODEL), verified_cache_reference()
    before = observe_files(paths)
    save(run / 'cache-launch-before.json', before)
    missing = repair_missing_files(before, expected)
    save(run / 'cache-launch-repair.json', dict(
        files=[str(path) for path in missing],
        bytes=sum(path.stat().st_size for path in missing),
        max_passes=1, max_files=2, max_bytes=1 << 30))
    deadline = time.monotonic() + 10
    for path in missing:
        with path.open('rb', buffering=0) as stream:
            while stream.readinto(buffer):
                require(time.monotonic() < deadline,
                        'final cache repair time budget exhausted')
    final = observe_files(paths)
    save(run / 'cache-launch.json', final)
    check_vector(final, expected)


def prepare_cache(run):
    try:
        return prepare(MODEL, run, True, repair=False, limit_gib=64)
    except RuntimeError as error:
        if not str(error).startswith('cache vector differs: '):
            raise
        # The imported helper has its own early exact gate. A bounded hole
        # may be deferred to the sole repair after the HTTP worker imports;
        # this is never a passed cache gate or authorization to load a model.
        expected = read(run / 'cache-expected.json')
        require(expected == verified_cache_reference(), 'cache target changed')
        missing = repair_missing_files(read(run / 'cache-ready.json'), expected)
        return dict(payload_bytes=sum(expected.values()), file_vector_exact=False,
                    physical_fragmentation_equal=False,
                    deferred_to_final_gate=[str(path) for path in missing],
                    early_gate_error=str(error))


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
        mark('prepare_begin')
        gate = prepare_cache(run)
        save(run / 'cache-gate.json', gate)
        require(read(run / 'cache-expected.json') == verified_cache_reference(),
                'full-hot vector differs from accepted prototype')
        mark('prepare_end')
        final = observe_files(model_files(MODEL))
        save(run / 'cache-before-http-worker.json', final)
        # This observation is evidence, not the launch gate: creating the HTTP
        # worker can itself change cache residency. Its final gate is strict.
        command = [sys.executable, '-B', str(Path(__file__).resolve()),
                   '--stage', stage, '--http-worker']
        save(run / 'command.json', command)
        mark('runner_begin')
        with (run / 'normal.log').open('x') as log:
            rc = run_owned_runner(command, cwd=ROOT,
                 env=environment(stage, run, os.environ), log=log, timeout=220,
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


def sealed_attempt(name):
    out = BASE / name
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
    require(state['default_unchanged'] and
            state['kernel']['returncode'] == state['resources']['returncode'] == 0
            and state['kernel']['result']['status'] ==
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
            'current/later attempt already exists; no implicit retry')
    prior = [sealed_attempt(name) for name in (*HISTORY, *STAGES[:index])]
    old_sha = read(BASE / 'build.json')['candidate_sha256']
    require(sha(BASE / 'build/q4t') == old_sha, 'historical candidate changed')
    for row, name in zip(prior, (*HISTORY, *STAGES[:index])):
        if name in HISTORY:
            status = 'RUNTIME_COMPLETED' if name == 'plain-v1' else 'RUNTIME_NOT_ACCEPTED'
            guard = ('COMPLETED' if name == 'plain-v1' else
                     'ENV_ABORT' if name == 'mtp-v3' else 'COMMAND_FAILED')
            starts = int(name in ('plain-v1', 'mtp-v3'))
            requests = int(name == 'plain-v1')
            candidate = old_sha
        else:
            status, guard, starts = 'RUNTIME_COMPLETED', 'COMPLETED', 1
            requests, candidate = configuration(name)['requests'], sha(BINARY)
            out = BASE / name / 'run/normal/http'
            require(read(out / 'exit.json')['http_output_checks_passed'] and
                    read(out / 'reservation-audit.json')['status'] == 'PASS',
                    'previous stage HTTP/reservation gate failed')
        require((row['status'], row['guard'], row['model_starts'],
                 row['http_requests'], row['candidate_sha256']) ==
                (status, guard, starts, requests, candidate),
                'attempt history differs or a previous new stage failed: ' + name)
    starts = sum(row['model_starts'] for row in prior)
    requests = sum(row['http_requests'] for row in prior)
    require(starts + 1 <= MODEL_START_LIMIT and
            requests + configuration(stage)['requests'] <= HTTP_LIMIT,
            'bounded model/HTTP budget exhausted')
    return dict(prior=prior, prior_model_starts=starts,
                prior_http_requests=requests, model_start_limit=MODEL_START_LIMIT,
                http_limit=HTTP_LIMIT, planned_model_starts=1,
                planned_http_requests=configuration(stage)['requests'])


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


def supervise(stage):
    build = verify_build()
    budget = previous_stage_gate(stage)
    out = BASE / stage
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(BUILD_MANIFEST, out / 'build.json')
    save(out / 'attempt-budget.json', budget)
    sources = set(HERE.glob('*.py')) | set(OBSERVE.glob('*.py'))
    for folder in ('tools/evalscope', 'tools/verify/device_reserve',
                   'tools/verify/vmm_compare'):
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
                 guarded_work_seconds=240, default_sha=DEFAULT_SHA,
                 observer_sha=build['candidate_sha256'],
                 cache_contract='re-established exact full-hot vector')
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
            for name, script, extra in (
                    ('kernel', 'kernel_capture.py', ['--max-bytes', '536870912']),
                    ('resources', 'resources.py', ['--binary', str(BINARY),
                                                  '--model-dir', str(MODEL)])):
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
            deadline = time.monotonic() + 240
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
