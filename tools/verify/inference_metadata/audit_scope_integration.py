"""Independent evidence checks for bounded scope/GPU integration."""
import csv
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/offload-foundation-20261020/scope-integration-v5'
EXPECTED = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def read(path):
    return json.loads(path.read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def scope_identity(folder, limit, wrapper_cleanup=False):
    identity, spec = read(folder / 'identity.json'), read(folder / 'spec.json')
    payload, props = read(folder / 'payload.json'), identity['properties']
    require(identity['kind'] == 'scope' and
            re.fullmatch(r'q4t-scope-[0-9a-f]{32}', identity['token']) and
            identity['token'] == spec['token'] == payload['token'] == props['Description'],
            'scope owner identity')
    require(identity['pid'] == payload['pid'] and
            identity['start_ticks'] == payload['start_ticks'] > 0 and
            identity['membership'] == payload['membership'] and
            '0::' + props['ControlGroup'] in identity['membership'], 'scope PID binding')
    require(int(props['MemoryMax']) == limit and props['MemorySwapMax'] == '0' and
            props['MemoryAccounting'] == props['IOAccounting'] == 'yes' and
            props['OOMPolicy'] == 'continue', 'scope limits')
    end = read(folder / 'payload-exit.json')
    require(end['token'] == identity['token'], 'payload exit owner')
    if not wrapper_cleanup:
        cleanup = read(folder / 'cleanup.json')
        require(cleanup['unit_removed'] and cleanup['properties_after']['LoadState'] == 'not-found',
                'scope not removed')
    else:
        # SIGTERM can end the inner runner before its Python finally runs.
        # The outer wrapper owns group reaping and exact-unit cleanup.
        outer = folder.parents[1]
        cleanup = read(outer / 'wrapper-exit.json')
        group = read(outer / 'runner-process-group.json')
        require(not cleanup['cleanup_failed'] and
                cleanup['unit_after_cleanup']['LoadState'] == 'not-found' and
                group['runner_reaped'] and group['cleanup_complete'] and
                not group['after_cleanup']['live_pids'] and
                not group['after_cleanup']['errors'] and end['returncode'] == -15,
                'outer wrapper cleanup not proved')
    return identity, end


def main():
    manifest = read(STAGE / 'sealed-files.json')
    for name, expected in manifest.items():
        path = (STAGE / name).resolve()
        require(path.is_relative_to(STAGE) and sha(path) == expected, 'seal: ' + name)
    guard = read(STAGE / 'lease/exit.json')
    require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0, 'GPU lease')
    out = STAGE / 'run'
    state, plan = read(out / 'state.json'), read(out / 'plan.json')
    require(state['status'] == 'PASS_SCOPE_INTEGRATION' and state['completed'] ==
            ['cpu', 'normal_gpu_quality', 'interrupted_gpu_cleanup'], 'driver completion')
    require(plan['binary_sha256'] == EXPECTED and plan['loads'] == 2 and
            plan['quality_requests'] == 1 and plan['retries'] == 0, 'scope budget')
    for name, expected in read(out / 'sources.json').items():
        require(sha(ROOT / name) == sha(out / 'source' / name) == expected,
                'integration source identity: ' + name)
    require(sha(ROOT / 'build/q4t') == EXPECTED, 'default binary identity')
    reuse = read(out / 'cpu-reuse.json')
    previous = Path(reuse['source'])
    require(sha(previous / 'sealed-files.json') == reuse['sealed_manifest_sha256'], 'CPU reuse identity')
    prior_manifest = read(previous / 'sealed-files.json')
    for name, digest in prior_manifest.items():
        require(sha(previous / name) == digest, 'CPU source seal')
        if name.startswith('run/cpu-'):
            require(sha(out / name.removeprefix('run/')) == digest, 'copied CPU evidence')
    cpu = {}
    for case, rc in [('normal', 0), ('terminate', -15), ('kill', -9), ('oom', -9)]:
        folder = out / ('cpu-' + case) / 'scope'
        identity, end = scope_identity(folder, (64 if case == 'oom' else 128) << 20)
        require(end['returncode'] == rc, 'CPU returncode: ' + case)
        if case == 'oom':
            rows = [json.loads(line) for line in (folder / 'endpoints.jsonl').read_text().splitlines()]
            final = next(row for row in rows if row['label'] == 'after_payload_exit')
            events = final['resources']['cgroup']['observations']['memory.events']['value']
            require(events is not None and events['oom_kill'] >= 1, 'controlled memcg OOM')
        cpu[case] = dict(returncode=rc, unit_removed=True)
    failed = out / 'cpu-startup_failure/scope'
    require(read(failed / 'cleanup.json')['unit_removed'] and
            'FileNotFoundError' in (failed / 'launcher.log').read_text() and
            read(failed / 'spec.json')['command'] == ['/q4t-intentionally-missing-scope-payload'],
            'intended startup failure/cleanup')
    cpu['startup_failure'] = dict(expected_exec_failure=True, unit_removed=True)
    for case in ('normal', 'interrupted'):
        gate = read(out / case / 'cache-gate.json')
        require(gate['cold_payload_established'] and not gate['advice_errors'] and
                gate['payload_resident_bytes'] == 0, 'cache advice gate')
    normal = out / 'normal'
    identity, end = scope_identity(normal / 'http/isolation', 64 << 30)
    require(end['returncode'] == 0 and int((normal / 'http/server.pid').read_text()) == identity['pid'],
            'normal GPU payload identity/exit')
    rows, http = read(normal / 'http/results.json'), read(normal / 'http/exit.json')
    require(len(rows) == 1 and rows[0]['actual_input'] == 1024 and rows[0]['success'] == 1 and
            rows[0]['exact_match'] and rows[0]['length_match'] and http['server'] == 0 and
            http['http_output_checks_passed'], 'scope HTTP quality')
    wrapper = read(normal / 'wrapper-exit.json')
    require(wrapper['runner_rc'] == wrapper['monitor_rc'] == 0 and not wrapper['failure'] and
            not wrapper['cleanup_failed'], 'normal wrapper result')
    process = read(out / 'normal-process.json')
    require(process['returncode'] == 0 and process['cleanup_complete'], 'normal runner cleanup')
    peak = read(normal / 'memory/memory-peak.json')
    require(peak['root_pid'] == identity['pid'] and peak['root_start_ticks'] == identity['start_ticks'] and
            peak['sampling_complete'] and peak['prelaunch_sample_present'] and
            peak['gpu_peak_bytes'] is not None and peak['gpu_peak_bytes'] > 0 and
            peak['resource_observations']['binding_mismatch_samples'] == 0 and
            peak['resource_observations']['cgroup_paths'] ==
            ['/sys/fs/cgroup' + identity['properties']['ControlGroup']], 'resource binding/coverage')
    with (normal / 'memory/memory.csv').open() as stream:
        statuses = {row['gpu_status'] for row in csv.DictReader(stream)}
    require(statuses <= {'ok', 'scheduled_skip', 'no_target', 'no_matching_compute_process'},
            'resource NVML query failure')
    interrupted = out / 'interrupted'
    identity, end = scope_identity(interrupted / 'http/isolation', 64 << 30, wrapper_cleanup=True)
    proof = read(out / 'interrupt-proof.json')
    require(proof['signal'] == 'SIGINT' and proof['pid'] == identity['pid'] and
            proof['identity'] == identity and proof['nvml']['status'] == 'OK' and
            identity['pid'] in proof['nvml']['pids'], 'GPU interruption witness')
    wrapper, process = read(interrupted / 'wrapper-exit.json'), read(out / 'interrupt-process.json')
    require(process['returncode'] != 0 and not process['group']['live_pids'] and
            not process['group']['errors'] and wrapper['failure'] and not wrapper['cleanup_failed'] and
            wrapper['unit_after_cleanup']['LoadState'] == 'not-found', 'interrupted cleanup')
    print(json.dumps(dict(status='PASS', sealed_files=len(manifest), guard_status=guard['status'],
        binary_sha256=EXPECTED, cpu=cpu, cpu_reused_from=str(previous), quality_requests=1, gpu_interruptions=1,
        normal_scope_nvml_peak_bytes=peak['gpu_peak_bytes'],
        interrupted_payload_exit=end['returncode'], performance_acceptance=False,
        strict_physical_ram_acceptance=False, lifecycle_six_fixture_rerun=False), indent=2))


if __name__ == '__main__':
    main()
