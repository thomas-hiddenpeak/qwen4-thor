"""Bounded scope ownership, memcg and GPU interruption contracts; GPU lease required."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
STAGE = ROOT / '.q4t-work/offload-foundation-20261020'
OUT = STAGE / 'scope-integration-v2/run'
PACKAGE = ROOT / '.q4t-work/releases/offload-foundation-20261020'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
EXPECTED = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def read(path):
    return json.loads(path.read_text())


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def cpu_contracts(env):
    from isolated_scope import IsolatedScope
    from gpu_exclusive import owned_processes, processes
    results = []
    for case in ('normal', 'terminate', 'kill', 'startup_failure', 'oom'):
        out = OUT / ('cpu-' + case)
        out.mkdir()
        command = ['/usr/bin/sleep', '2' if case == 'normal' else '30']
        if case == 'startup_failure':
            command = ['/q4t-intentionally-missing-scope-payload']
        elif case == 'oom':
            command = ['/usr/bin/python3', '-c',
                'import time; time.sleep(1); x=bytearray(256<<20); time.sleep(30)']
        limit = (64 if case == 'oom' else 128) << 20
        try:
            service = IsolatedScope(command, cwd=ROOT, env=env, log_path=out / 'payload.log',
                unit=f'q4t-scope-{case.replace("_", "-")}-{time.time_ns()}.scope',
                memory_max=limit, evidence=out / 'scope')
        except RuntimeError:
            require(case == 'startup_failure' and
                    read(out / 'scope/cleanup.json')['unit_removed'], 'unexpected startup failure')
            results.append(dict(case=case, expected_startup_failure=True, unit_removed=True))
            continue
        try:
            require(service.pid in owned_processes(processes(), os.getpgrp(), {}),
                    'scope payload escaped caller ancestry')
            props = read(out / 'scope/identity.json')['properties']
            require(int(props['MemoryMax']) == limit and props['MemorySwapMax'] == '0',
                    'memcg limit properties')
            if case == 'terminate':
                service.terminate()
            elif case == 'kill':
                service.kill()
            expected = 0 if case == 'normal' else -signal.SIGTERM if case == 'terminate' else -signal.SIGKILL
            require(service.wait(timeout=15) == expected, 'CPU payload exit')
            final = service.snapshot('after_payload_exit')
            if case == 'oom':
                counters = final['resources']['cgroup']['observations']['memory.events']['value']
                require(counters is not None and counters['oom_kill'] >= 1, 'memcg OOM not observed')
            results.append(dict(case=case, returncode=expected, caller_ancestry=True,
                                memory_max=limit, swap_max=0))
        finally:
            service.close()
        require(service.closed, 'CPU scope not removed')
    save(OUT / 'cpu-contracts.json', results)


def main():
    from isolated_scope import scope_properties, stop_owned_scope
    from run_budget_experiment import run_owned_runner, process_group_state
    import nvml_process_query
    OUT.mkdir(parents=True, exist_ok=False)
    require(sha(PACKAGE / 'q4t') == EXPECTED, 'release binary changed')
    require(read(STAGE / 'baseline-v1/lease/exit.json')['status'] == 'COMPLETED',
            'baseline is not completed')
    sources = {}
    paths = list((ROOT / 'tools/evalscope').glob('*.py')) + [Path(__file__)]
    for path in paths:
        name = str(path.relative_to(ROOT))
        target = OUT / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        sources[name] = sha(path)
    save(OUT / 'sources.json', sources)
    fixtures = OUT / 'fixtures'
    shutil.copytree(STAGE / 'quality-v2/run/reference-off/inputs', fixtures)
    manifest = read(fixtures / 'manifest.json')
    requests = (fixtures / 'requests.jsonl').read_text().splitlines()
    index = next(i for i, row in enumerate(manifest) if row['length'] == 1024)
    save(fixtures / 'manifest.json', [manifest[index]])
    (fixtures / 'requests.jsonl').write_text(requests[index] + '\n')
    shutil.copy2(STAGE / 'quality-v2/run/quality-offload/results.json', OUT / 'reference.json')
    save(OUT / 'hot.json', {})
    save(OUT / 'plan.json', dict(binary_sha256=EXPECTED, loads=2, quality_requests=1,
        interrupted_gpu_loads=1, runner_timeout_seconds=1200, retries=0,
        host_cache_max_bytes=16 << 30, cold_cache=False, physical_ram_acceptance=False,
        cpu_cases=['normal', 'terminate', 'kill', 'startup_failure', 'oom']))
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    state = dict(status='RUNNING', started=time.time(), completed=[])
    save(OUT / 'state.json', state)
    try:
        cpu_contracts(env)
        state['completed'].append('cpu')
        save(OUT / 'state.json', state)
        common = [sys.executable, '-B', 'tools/evalscope/run_budget_experiment.py',
            '--binary', str(PACKAGE / 'q4t'), '--model-dir', MODEL,
            '--hot-list', str(OUT / 'hot.json'), '--fixtures', str(fixtures),
            '--reference', str(OUT / 'reference.json'), '--mode', 'quality',
            '--chunk-order', '0', '--host-cache-max-bytes', str(16 << 30),
            '--runner-timeout-s', '1200', '--port', '8740']
        normal = OUT / 'normal'
        command = common + ['--output', str(normal)]
        save(OUT / 'normal-command.json', command)
        with (OUT / 'normal.log').open('w') as log:
            require(run_owned_runner(command, cwd=ROOT, env=env, log=log, timeout=1260,
                evidence=OUT / 'normal-process.json') == 0, 'normal budget wrapper failed')
        response = read(normal / 'http/results.json')
        require(len(response) == 1 and response[0]['success'] == 1 and
                response[0]['exact_match'] and response[0]['length_match'], 'normal HTTP quality')
        wrapper = read(normal / 'wrapper-exit.json')
        require(wrapper['runner_rc'] == wrapper['monitor_rc'] == 0 and
                not wrapper['cleanup_failed'], 'normal wrapper cleanup')
        state['completed'].append('normal_gpu_quality')
        save(OUT / 'state.json', state)
        interrupted = OUT / 'interrupted'
        command = common + ['--output', str(interrupted)]
        save(OUT / 'interrupt-command.json', command)
        with (OUT / 'interrupted.log').open('w') as log:
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            proof = None
            try:
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    require(proc.poll() is None, 'wrapper exited before interruption')
                    identity_path = interrupted / 'http/isolation/identity.json'
                    if identity_path.exists():
                        identity = read(identity_path)
                        query = nvml_process_query.query(timeout=5)
                        require(query['status'] == 'OK', 'NVML interruption observation failed')
                        if identity['pid'] in query['pids']:
                            proof = dict(pid=identity['pid'], identity=identity, nvml=query,
                                         signal='SIGINT', observed=time.time())
                            break
                    time.sleep(.2)
                require(proof is not None, 'GPU context did not appear in bounded wait')
                save(OUT / 'interrupt-proof.json', proof)
                proc.send_signal(signal.SIGINT)
                require(proc.wait(timeout=120) != 0, 'interrupted wrapper reported success')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
                protocol_path = interrupted / 'protocol.json'
                if protocol_path.exists():
                    unit = read(protocol_path)['unit']
                    if scope_properties(unit)['LoadState'] != 'not-found':
                        spec = read(interrupted / 'http/isolation/spec.json')
                        stop_owned_scope(unit, spec['token'])
                group = process_group_state(proc.pid)
                save(OUT / 'interrupt-process.json', dict(returncode=proc.returncode, group=group))
        require(not group['live_pids'] and not group['errors'], 'interrupted live descendants')
        wrapper = read(interrupted / 'wrapper-exit.json')
        require(not wrapper['cleanup_failed'] and wrapper['unit_after_cleanup']['LoadState'] == 'not-found',
                'interrupted unit not removed')
        state['completed'].append('interrupted_gpu_cleanup')
        for name, digest in sources.items():
            require(sha(ROOT / name) == digest, 'source changed during integration')
        state['status'] = 'PASS_SCOPE_INTEGRATION'
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save(OUT / 'state.json', state)


if __name__ == '__main__':
    main()
