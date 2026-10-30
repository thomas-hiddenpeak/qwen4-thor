"""Bounded main re-audit repair acceptance; no performance acceptance.

prepare freezes inputs after CPU checks/builds. run and deploy each require a
complete gpu_exclusive lease. audit/package/finalize never load a model.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/main-reaudit-fixes-20261031'
PREVIOUS = BASE / 'baseline'
PACKAGE = BASE / 'delivery-package'
DEPLOYMENT = BASE / 'deployment'
MARKER = ROOT / 'build/main-reaudit-recovery.json'
sys.path[:0] = [str(ROOT / 'tools/evalscope'),
               str(ROOT / 'tools/verify/startup_delivery')]
import deploy as h
import gpu_exclusive
import nvml_process_query
from serve_reliability import Runner, decoded, fields, save, sha

health = h.module('main_reaudit_health', ROOT / 'tools/serve_reliability/health.py')
CONTRACT = ROOT / 'docs/MAIN_REAUDIT_FIX_PLAN_2026-10-31.md'


def guarded(output):
    # Reuse the deployment helper's flock and ancestor checks, with its exact
    # output/lease identity rather than trusting an environment variable.
    h.require_guard(output)


def verify_inputs(plan):
    for path, digest in plan['files'].items():
        h.require(sha(path) == digest, 'frozen input changed: ' + path)


def prepare():
    baseline = h.read(PREVIOUS / 'identity.json')
    old = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                        baseline['binary_sha256'])
    h.require(sha(ROOT / 'build/q4t.release.json') == baseline['sidecar_sha256'],
              'default metadata changed')
    cpu = BASE / 'cpu'
    cpu.mkdir()
    names = ['io-0.log', 'io-1.log', 'io-2.log', 'io-commands.json',
             'full-0.log', 'full-1.log', 'full-commands.json',
             'host-0.log', 'host-1.log', 'host-2.log', 'host-commands.json',
             'supervisor-0.log', 'supervisor-commands.json',
             'thor-host.log', 'budget-0.log', 'budget-1.log', 'budget-2.log',
             'budget-commands.json', 'rollback.log', 'cpu-audit.json']
    for name in names:
        h.copy_checked(h.identity(BASE / name), cpu / name)
    coverage = h.read(BASE / 'cpu-audit.json')
    h.require(coverage['passed'], 'CPU/build coverage and zero warnings')
    for path, digest in coverage['inputs'].items():
        h.require(sha(path) == digest, 'CPU evidence changed: ' + path)
    for path, digest in coverage['runtime'].items():
        h.require(sha(ROOT / path) == digest, 'tested runtime changed: ' + path)
    frozen = BASE / 'frozen'
    frozen.mkdir()
    for source, name in [(PREVIOUS / 'q4t', 'parent'),
        (BASE / 'build/q4t', 'candidate'),
        (BASE / 'build/q4t_tests', 'q4t_tests'),
        (BASE / 'io-host/q4t_residency_cuda_contracts', 'cuda-contracts'),
        (ROOT / '.q4t-work/audit-p2-20261031/stage1/payload_fault.so',
         'payload_fault.so'),
        (BASE / 'build/CMakeCache.txt', 'CMakeCache.txt')]:
        h.copy_checked(h.identity(source), frozen / name)
    runtime = {str(p.relative_to(ROOT)): sha(p) for folder in ['src', 'include']
               for p in sorted((ROOT / folder).rglob('*')) if p.is_file()}
    h.save(frozen / 'runtime-source.json', runtime)
    prior = h.read(ROOT / '.q4t-work/serve-reliability-20261030/http-plan.json')
    steps = [dict(name=binary + '-' + mode, binary=binary,
                  mode='plain' if mode == 'ssd' else mode,
                  kind='stats' if mode == 'ssd' else 'quality', requests=2,
                  environment=dict(Q4T_MOE_L2_SLOTS='16', Q4T_MOE_MIRROR_K='8',
                                   Q4T_MOE_LOAD_THREADS='16'))
             for binary in ['parent', 'candidate']
             for mode in ['plain', 'full', 'subset', 'ssd']]
    required = [line[len('Q4T_TEST('):].split(')')[0] for line in
        (ROOT / 'tests/quant_moe_residency_test.cpp').read_text().splitlines()
        if line.startswith('Q4T_TEST(residency_')]
    h.require(len(required) == 10, 'explicit real-model contract coverage')
    (frozen / 'required-residency.txt').write_text('\n'.join(required) + '\n')
    tool_paths = [Path(__file__), Path(h.__file__), Path(h.build.__file__),
        Path(health.__file__), ROOT / 'tools/evalscope/serve_reliability.py',
        ROOT / 'tools/evalscope/gpu_exclusive.py',
        ROOT / 'tools/evalscope/nvml_process_query.py',
        ROOT / 'tools/evalscope/monitor_memory.py',
        ROOT / 'tools/evalscope/resource_metrics.py',
        ROOT / 'tools/release/release.py',
        ROOT / 'tools/release/test_supervisor.py',
        ROOT / 'tools/verify/main_reaudit/rollback.py',
        ROOT / 'tools/verify/main_reaudit/test_rollback.py']
    files = list(frozen.iterdir()) + list(cpu.iterdir()) + tool_paths
    files += [ROOT / p for p in runtime]
    files += [ROOT / 'CMakeLists.txt', CONTRACT,
              ROOT / 'tools/release/required_host_tests.txt',
              ROOT / 'tests/io_host/CMakeLists.txt',
              ROOT / 'tests/safetensors_validation_test.cpp',
              ROOT / 'tests/residency_host_contract_test.cpp',
              ROOT / 'tests/weight_loader_contract_test.cpp',
              ROOT / 'tests/runtime_memory_budget_test.cpp',
              ROOT / 'tools/verify/inference_metadata/payload_fault.cpp.in']
    files += [Path(prior['model']) / n for n in
              ['config.json', 'tokenizer.json', 'model.safetensors.index.json']]
    files += [Path(old['subset_artifact']) / n for n in
              ['head.bf16', 'token_ids.bin', 'manifest.json']]
    plan = dict(version=1, model=prior['model'], artifact=old['subset_artifact'],
        quality={str(n): prior['quality'][str(n)] for n in [1024, 8192]},
        cache_prompt=prior['cache_prompt'], steps=steps, port=8764,
        parent=str(frozen / 'parent'), candidate=str(frozen / 'candidate'),
        output=str(BASE / 'measurement/http'), runtime=runtime,
        tools={str(p): sha(p) for p in tool_paths},
        files={str(p): sha(p) for p in files},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                              cwd=ROOT, text=True).strip(),
        planned_http=22, planned_starts=10, maximum_http=26, maximum_starts=12,
        planned_gpu_processes=2, maximum_gpu_processes=3,
        measurement_configuration=dict(max_seq=2, max_prefill=4096,
                                       max_len=65536, requests_in_flight=1),
        performance_acceptance=False, started_unix=time.time())
    plan['deadline_unix'] = plan['started_unix'] + 90 * 60
    h.save(BASE / 'plan.json', plan)
    print(json.dumps(dict(plan=str(BASE / 'plan.json'),
                          deadline=plan['deadline_unix'])))


class RepairRunner(Runner):
    def charge(self, kind, label):
        super().charge(kind, label)
        h.require(self.http_count <= 16 and self.starts <= 8,
                  'stage-one measurement budget')

    def quality_pair(self, step, folder):
        rows = []
        for length in [1024, 8192]:
            fixture = self.plan['quality'][str(length)]
            label = step['name'] + '-' + str(length)
            response = self.http(folder, label,
                                 self.body(label, fixture['prompt']))
            row = decoded(response)
            h.require(row['text'].strip() == fixture['expected'] and
                      row['usage']['prompt_tokens'] == length,
                      'quality and tokenizer oracle')
            row.update(length=length, seconds=response['seconds'],
                       ttft=response['first_content_seconds'])
            rows.append(row)
            self.free()
        save(folder / 'quality.json', rows)
        if step['binary'] == 'candidate':
            previous = self.out / step['name'].replace('candidate-', 'parent-')
            old = h.read(previous / 'quality.json')
            h.require(len(old) == len(rows) and all(
                a[k] == b[k] for a, b in zip(old, rows)
                for k in ['text', 'usage', 'finish']), 'complete response changed')
        save(folder / 'metrics.json', self.http(None, 'metrics', path='/metrics'))


def run():
    guarded(BASE / 'measurement')
    plan = h.read(BASE / 'plan.json')
    verify_inputs(plan)
    out = BASE / 'measurement'
    frozen = BASE / 'frozen'
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    gpu_results = []
    commands = [([str(frozen / 'cuda-contracts')], {}),
                ([str(frozen / 'q4t_tests'), '--required-list',
                  str(frozen / 'required-residency.txt')],
                 dict(LD_PRELOAD=str(frozen / 'payload_fault.so')))]
    for i, (command, extra) in enumerate(commands):
        h.require(time.time() < plan['deadline_unix'] - 600, 'GPU deadline')
        h.save(out / f'gpu-{i}-command.json', dict(argv=command, environment=extra))
        with (out / f'gpu-{i}.log').open('x') as log:
            result = subprocess.run(command, cwd=ROOT, env=env | extra,
                stdout=log, stderr=subprocess.STDOUT, timeout=600)
        gpu_results.append(result.returncode)
        h.require(result.returncode == 0, 'GPU contract process ' + str(i))
        print('gpu-contract-' + str(i) + ': PASS', flush=True)
    runner = RepairRunner(plan)
    try:
        for step in plan['steps']:
            with runner.service(step) as (folder, _):
                runner.quality_pair(step, folder)
            runner.completed.append(step['name'])
            print(step['name'] + ': PASS', flush=True)
        final = nvml_process_query.query(timeout=5)
        save(out / 'final-gpu.json', final)
        h.require(final['status'] == 'OK' and not final['pids'], 'remaining GPU context')
        verify_inputs(plan)
        h.save(out / 'result.json', dict(passed=True, http=runner.http_count,
            starts=runner.starts, gpu_processes=gpu_results,
            completed=runner.completed, finished=time.time()))
    except BaseException as error:
        h.save(out / 'failure.json', dict(error=repr(error),
            traceback=traceback.format_exc(), http=runner.http_count,
            starts=runner.starts, completed=runner.completed))
        raise


def audit():
    plan = h.read(BASE / 'plan.json')
    verify_inputs(plan)
    guard = h.read(BASE / 'measurement/lease/exit.json')
    result = h.read(BASE / 'measurement/result.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'measurement GPU guard')
    h.require(result['passed'] and result['http'] == 16 and result['starts'] == 8
              and result['gpu_processes'] == [0, 0] and
              result['completed'] == [s['name'] for s in plan['steps']],
              'complete bounded matrix')
    comparisons = {}
    for step in plan['steps']:
        folder = BASE / 'measurement/http' / step['name']
        end = h.read(folder / 'exit.json')
        h.require(end['passed'] and end['lifecycle'] and end['server_exit'] == 0
                  and end['requests'] == 2, 'lifecycle coverage')
        raw = (folder / 'server.log').read_text()
        rows = fields(raw, '[q4t][request] ')
        endings = [r for r in rows if r['event'] == 'end']
        h.require(len(endings) == 2 and all(r['released'] == '1' and
            r['outcome'] == 'success' for r in endings), 'successful release')
        finals = [json.loads(line.split('] ', 1)[1]) for line in raw.splitlines()
                  if line.startswith('[startup-reservation] ') and
                  '"event":"finalized"' in line]
        h.require(len(finals) == 1 and finals[0]['status'] == 'PASS' and
            finals[0]['handed_live'] == finals[0]['unhanded_remaining'] == 0,
            'tracked resource finalization')
        comparisons[step['name']] = h.read(folder / 'quality.json')
    for i, count in enumerate([11, 10]):
        raw = (BASE / f'measurement/gpu-{i}.log').read_text()
        h.require(f'{count} tests, {count} passed, 0 failed, 0 skipped' in raw,
                  'GPU exact coverage')
    evidence = {str(p): sha(p) for folder in [BASE / 'measurement', BASE / 'cpu']
                for p in sorted(folder.rglob('*')) if p.is_file()}
    h.save(BASE / 'decision.json', dict(passed=True,
        engineering_decision='ACCEPT_BOUNDED_MAIN_REAUDIT_REPAIR',
        candidate_sha256=sha(plan['candidate']), parent_sha256=sha(plan['parent']),
        performance_acceptance=False, comparisons=comparisons, evidence=evidence,
        source_commit=plan['source_commit'], fixed=['R1', 'R2', 'R3', 'R4'],
        limitations=['Single pair timings only; no statistical noninferiority.',
                     'Logical reads and inherited page cache; no strict SSD/RAM claim.']))


def package():
    plan, decision = h.read(BASE / 'plan.json'), h.read(BASE / 'decision.json')
    verify_inputs(plan)
    h.require(decision['passed'], 'accepted measurement')
    h.require(time.time() < plan['deadline_unix'] - 600, 'health time reserve')
    old = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                        decision['parent_sha256'])
    h.require(sha(ROOT / 'build/q4t.release.json') ==
              sha(PREVIOUS / 'q4t.release.json'), 'old sidecar')
    PACKAGE.mkdir()
    for name in ['previous', 'candidate', 'tested-source', 'evidence', 'tools']:
        (PACKAGE / name).mkdir()
    for name in ['q4t', 'q4t.release.json', 'CMakeCache.txt', 'runtime-source.json']:
        h.copy_checked(h.identity(PREVIOUS / name), PACKAGE / 'previous' / name)
    for source, target in [('frozen/candidate', 'candidate/q4t'),
        ('frozen/CMakeCache.txt', 'candidate/CMakeCache.txt'),
        ('frozen/runtime-source.json', 'candidate/runtime-source.json'),
        ('decision.json', 'decision.json'), ('plan.json', 'evidence/plan.json')]:
        h.copy_checked(h.identity(BASE / source), PACKAGE / target)
    h.copy_checked(h.identity(CONTRACT), PACKAGE / 'acceptance-contract.md')
    for relative in plan['runtime']:
        target = PACKAGE / 'tested-source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT / relative), target)
    shutil.copytree(plan['artifact'], PACKAGE / 'artifact')
    for name in ['head.bf16', 'token_ids.bin', 'manifest.json']:
        h.require(sha(PACKAGE / 'artifact' / name) ==
                  plan['files'][str(Path(plan['artifact']) / name)], 'artifact identity')
    archive = BASE / 'evidence-archive'
    archive.mkdir()
    for path, digest in decision['evidence'].items():
        h.require(sha(path) == digest, 'accepted evidence changed')
        target = archive / Path(path).relative_to(BASE)
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(path), target)
    h.save(BASE / 'archive-receipt.json', dict(seal=h.seal(archive),
                                             original=decision['evidence']))
    h.copy_checked(h.identity(BASE / 'archive-receipt.json'),
                   PACKAGE / 'evidence/archive-receipt.json')
    metadata = copy.deepcopy(old)
    metadata.update(version='main-reaudit-repairs-20261031',
        binary_sha256=decision['candidate_sha256'], source_commit=plan['source_commit'],
        source_note='Expert tensor contracts, hot-list dedup, load exceptions and descendant cleanup.',
        acceptance=decision['engineering_decision'],
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE / 'artifact'),
        subset_manifest_sha256=sha(PACKAGE / 'artifact/manifest.json'))
    for field, relative in [
        ('tested_source_manifest', 'candidate/runtime-source.json'),
        ('build_cache', 'candidate/CMakeCache.txt'),
        ('acceptance_snapshot', 'acceptance-contract.md'),
        ('acceptance_decision', 'decision.json'),
        ('acceptance_audit', 'evidence/archive-receipt.json')]:
        metadata[field] = str(PACKAGE / relative)
        metadata[field + '_sha256'] = sha(PACKAGE / relative)
    metadata['main_reaudit'] = dict(fixed=['R1', 'R2', 'R3', 'R4'], pending=[])
    metadata['tool_source_commit'] = plan['source_commit']
    metadata['tool_acceptance'] = str(PACKAGE / 'decision.json')
    metadata['tool_acceptance_sha256'] = sha(PACKAGE / 'decision.json')
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    for name, digest in [('candidate', decision['candidate_sha256']),
                         ('previous', decision['parent_sha256'])]:
        h.verify_pair(PACKAGE / name / 'q4t', PACKAGE / name / 'q4t.release.json',
                      digest, PACKAGE / name / 'CMakeCache.txt')
    for path in plan['tools']:
        target = PACKAGE / 'tools' / Path(path).relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(path), target)
    h.save(PACKAGE / 'runtime-tool-inputs.json', plan['tools'])
    DEPLOYMENT.mkdir()
    health_plan = dict(candidate=str(ROOT / 'build/q4t'),
        artifact=str(PACKAGE / 'artifact'), output=str(DEPLOYMENT / 'run'),
        model=plan['model'], port=plan['port'], cache_prompt=plan['cache_prompt'],
        deadline_unix=plan['deadline_unix'])
    h.save(DEPLOYMENT / 'health-plan.json', health_plan)
    h.save(PACKAGE / 'health-plan.json', health_plan)
    h.save(BASE / 'package-receipt.json', dict(seal=h.seal(PACKAGE)))


def verify_delivery():
    h.verify_seal(PACKAGE)
    for path, digest in h.read(PACKAGE / 'runtime-tool-inputs.json').items():
        h.require(sha(path) == digest, 'delivery tool changed')


def restore():
    verify_delivery()
    if MARKER.exists():
        h.require(h.read(MARKER)['package'] == str(PACKAGE), 'foreign marker')
    states = []
    for name in ['q4t', 'q4t.release.json']:
        old, new = sha(PACKAGE / 'previous' / name), sha(PACKAGE / 'candidate' / name)
        current = sha(ROOT / 'build' / name)
        h.require(current in [old, new], 'unknown installed identity')
        states.append((name, old, new, current))
    for name, old, new, current in states:
        if current != old:
            h.replace_checked(PACKAGE / 'previous' / name, ROOT / 'build' / name, new)
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                  sha(PACKAGE / 'previous/q4t'))
    if MARKER.exists():
        MARKER.unlink()
        h.fsync_directory(MARKER.parent)


def deploy():
    guarded(DEPLOYMENT)
    verify_delivery()
    plan = h.read(DEPLOYMENT / 'health-plan.json')
    h.require(plan == h.read(PACKAGE / 'health-plan.json') and not MARKER.exists(),
              'health plan or recovery marker')
    h.require(time.time() < plan['deadline_unix'] - 600, 'health time reserve')
    for name in ['q4t', 'q4t.release.json']:
        h.require(sha(ROOT / 'build' / name) == sha(PACKAGE / 'previous' / name),
                  'installed baseline changed')
    h.save(MARKER, dict(package=str(PACKAGE)))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING', started=time.time())
    def interrupted(*_):
        raise KeyboardInterrupt('installation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in ['q4t', 'q4t.release.json']:
            h.replace_checked(PACKAGE / 'candidate' / name, ROOT / 'build' / name,
                              sha(PACKAGE / 'previous' / name))
        health.Health(plan).run()
        result = h.read(DEPLOYMENT / 'run/result.json')
        h.require(result['passed'] and result['http'] == 6 and result['starts'] == 2,
                  'installed health coverage')
        verify_delivery()
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        try:
            restore()
            state['rollback'] = 'RESTORED'
        except BaseException as rollback_error:
            state['rollback_error'] = repr(rollback_error)
        raise
    finally:
        state['finished'] = time.time()
        h.save(DEPLOYMENT / 'deployment.json', state)


def finalize():
    guard, state = h.read(DEPLOYMENT / 'lease/exit.json'), h.read(DEPLOYMENT / 'deployment.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'deployment guard')
    h.require(state['status'] == 'HEALTH_CHECKED_PENDING_GUARD' and
              state['finished'] < h.read(BASE / 'plan.json')['deadline_unix'],
              'health acceptance and deadline')
    verify_delivery()
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                  sha(PACKAGE / 'candidate/q4t'))
    h.require(sha(ROOT / 'build/q4t.release.json') ==
              sha(PACKAGE / 'candidate/q4t.release.json'), 'installed sidecar')
    h.require(h.read(MARKER)['package'] == str(PACKAGE), 'owned recovery marker')
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    h.save(DEPLOYMENT / 'finalized.json', dict(status='DELIVERED',
        binary_sha256=sha(ROOT / 'build/q4t'), sidecar_sha256=sha(ROOT / 'build/q4t.release.json'),
        total_http=22, total_starts=10, gpu_processes=2, persistent_service=False,
        finished=time.time()))
    archive = BASE / 'health-archive'
    shutil.copytree(DEPLOYMENT, archive)
    h.save(BASE / 'health-archive-receipt.json', dict(seal=h.seal(archive)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'audit', 'package',
                                         'deploy', 'finalize', 'rollback'])
    action = parser.parse_args().action
    if action == 'rollback':
        guarded(DEPLOYMENT)
        restore()
    else:
        globals()[action]()
