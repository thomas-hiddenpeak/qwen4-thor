"""Accepted-only install, bounded health, exact-pair rollback and declarations.

The shared recovery marker prevents cooperative GPU work from observing a
partially installed release. ELF and sidecar are individually atomic replaces;
they are not a cross-file power-loss transaction. A separate rollback command
can recover any known partial pair without starting a model.
"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import driver
import health

ROOT = driver.ROOT
BASE = ROOT.parent
PACKAGE = BASE / 'delivery-package'
OUT = ROOT / '.q4t-work/scoring-health'
PRIOR = ROOT / '.q4t-work/scoring-continuation/plan.json'
h = driver.module('scoring_deploy_helpers', 'tools/verify/startup_delivery/deploy.py')
pkg = driver.module('scoring_release_package', 'tools/serve_service/package.py')


def marker():
    lock = driver.legacy.gpu_exclusive.common_lock()
    return lock.with_name(lock.name + '.recovery-required')


def prepare():
    prior = h.read(PRIOR)
    decision = h.read(BASE / 'decision.json')
    h.require(decision['status'] == 'PASS_SCORING_ENGINEERING_ACCEPTANCE' and
              decision['passed'] and decision['candidate_sha256'] == prior['candidate_sha256'],
              'explicit performance acceptance required')
    h.require(h.read(BASE / 'archive-receipt.json')['passed'], 'evidence archive')
    driver.legacy.verify_files(prior)
    driver.verify_reuse(prior)
    original = Path(prior['execution_tools_root'])
    baseline = h.read(BASE / 'baseline.json')
    h.require(h.sha(original / 'build/q4t.release.json') == baseline['sidecar'],
              'default sidecar changed')
    old = h.verify_pair(original / 'build/q4t', original / 'build/q4t.release.json',
                        prior['parent_sha256'])
    PACKAGE.mkdir()
    for name in ('previous', 'candidate', 'tested-source'):
        (PACKAGE / name).mkdir()
    for name in ('q4t', 'q4t.release.json'):
        h.copy_checked(h.identity(original / 'build' / name), PACKAGE / 'previous' / name)
    for key, name in [('build_cache', 'CMakeCache.txt'),
                      ('acceptance_decision', 'acceptance.json'),
                      ('tested_source_manifest', 'runtime-source.json')]:
        h.copy_checked(h.identity(old[key]), PACKAGE / 'previous' / name)
    shutil.copytree(old['release_declarations']['directory'], PACKAGE / 'previous/declarations')
    h.copy_checked(old['operations_rules'], PACKAGE / 'previous/q4t.rules.yml')
    h.seal(PACKAGE / 'previous')
    for source, name in [(Path(prior['candidate']), 'q4t'),
                         (BASE / 'build/CMakeCache.txt', 'CMakeCache.txt'),
                         (BASE / 'configure.log', 'configure.log'),
                         (BASE / 'build.log', 'build.log')]:
        h.copy_checked(h.identity(source), PACKAGE / 'candidate' / name)
    for name in ('decision.json', 'review.json', 'archive-receipt.json'):
        h.copy_checked(h.identity(BASE / name), PACKAGE / name)
    runtime = h.read(BASE / 'parent/runtime-source.json')
    current = {}
    for name in runtime:
        current[name] = h.sha(ROOT / name)
        target = PACKAGE / 'tested-source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT / name), target)
    h.save(PACKAGE / 'candidate/runtime-source.json', current)
    shutil.copytree(prior['artifact'], PACKAGE / 'artifact')
    for name in ('head.bf16', 'token_ids.bin', 'manifest.json'):
        h.require(h.sha(PACKAGE / 'artifact' / name) ==
                  prior['files'][str(Path(prior['artifact']) / name)],
                  'subset artifact copy changed')
    h.copy_checked(old['operations_rules'], PACKAGE / 'candidate/q4t.rules.yml')
    plan = copy.deepcopy(prior)
    plan.pop('reuse_record', None)
    plan.update(stage='SCORING_HEALTH', output=str(OUT / 'run'),
        candidate=str(original / 'build/q4t'), artifact=str(PACKAGE / 'artifact'),
        http_requests=6, http_loads=2, stage_timeout_seconds=900,
        maximum_gpu_contracts=0,
        task_deadline_unix=time.time() + 900,
        required_gate=str(PACKAGE / 'decision.json'), steps=[])
    for head in ('full', 'subset'):
        name = 'installed-' + head
        plan['steps'].append(dict(name=name, kind='lifecycle', head=head,
            layout='agent', suffix=True, binary=plan['candidate'], http_requests=3,
            http_loads=1, timeout=420, command=[sys.executable, '-B',
                str(Path(__file__).with_name('health.py')), '--plan',
                str(OUT / 'plan.json'), '--custom-step', name]))
    for path in PACKAGE.rglob('*'):
        if path.is_file():
            plan['files'][str(path)] = h.sha(path)
    for path in (Path(__file__), Path(__file__).with_name('health.py')):
        plan['files'][str(path)] = h.sha(path)
    plan['files'][plan['candidate']] = prior['candidate_sha256']
    health.validate(plan)
    OUT.mkdir()
    h.save(OUT / 'plan.json', plan)
    h.save(PACKAGE / 'health-plan.json', plan)
    h.save(OUT / 'preparation.json', dict(previous=h.identity(PACKAGE / 'previous/q4t'),
        previous_release=h.identity(PACKAGE / 'previous/q4t.release.json'),
        candidate=h.identity(PACKAGE / 'candidate/q4t'), plan=h.identity(OUT / 'plan.json')))


def release(plan):
    old = h.read(PACKAGE / 'previous/q4t.release.json')
    metadata = copy.deepcopy(old)
    declarations = PACKAGE / 'candidate/declarations'
    declarations.mkdir()
    for name in ('capabilities', 'configuration'):
        h.copy_checked(h.identity(OUT / 'run/installed-full' / (name + '.json')),
                       declarations / (name + '.json'))
    shutil.copytree(PACKAGE / 'previous/declarations/schemas', declarations / 'schemas')
    caps, config = h.read(declarations / 'capabilities.json'), h.read(declarations / 'configuration.json')
    protocol = metadata['chat_protocol']
    protocol['protocol_sha256'] = caps['protocol_source_sha256']
    for key in caps.keys() & protocol.keys():
        protocol[key] = copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path = declarations / 'schemas' / (name + '.json')
        protocol[name + '_schema'] = str(path)
        protocol[name + '_schema_sha256'] = h.sha(path)
    protocol['declaration_evidence'] = 'Actual installed scoring health observations. No new managed, durable-task migration or SDK qualification.'
    protocol['sdk_interoperability_acceptance'] = dict(
        current='Not newly qualified by the scoring task',
        new_sdk_qualification=False,
        prior_release=h.identity(PACKAGE / 'previous/q4t.release.json'))
    for key in ('deployment_completion', 'audit_p2', 'main_reaudit',
                'inherited_acceptance', 'tool_acceptance', 'tool_acceptance_sha256'):
        metadata.pop(key, None)
    metadata.update(version='mtp-prefill-scoring-20261104',
        binary_sha256=plan['candidate_sha256'], source_commit=plan['source_commit'],
        tool_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_note='Four scoring runtime files selectively extracted on main. Runtime manifest is authoritative; no client WIP or whole research merge.',
        tested_source_manifest=str(PACKAGE / 'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE / 'candidate/runtime-source.json'),
        build_cache=str(PACKAGE / 'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE / 'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_PREFILL_SCORING',
        acceptance_decision=str(PACKAGE / 'decision.json'),
        acceptance_decision_sha256=h.sha(PACKAGE / 'decision.json'),
        inherited_release=h.identity(PACKAGE / 'previous/q4t.release.json'),
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        observed_configuration=config, subset_artifact=str(PACKAGE / 'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE / 'artifact/manifest.json'),
        operations_rules=h.identity(PACKAGE / 'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE / 'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', performance='bounded paired five-tier and four-agent scoring',
            correctness='independent sliced scoring, representative state and real HTTP quality/cache',
            statistics='descriptive; not statistical noninferiority',
            deployment='default CLI, full and explicit subset MTP health; no persistent service',
            boundaries='Old managed packages and durable tasks stay pinned. No client WIP, SDK, TLS deployment or long soak acceptance.'),
        scoring=dict(uniform_slot_only=True, minimum_rows=5,
            fallback='Q4T_MTP_PREFILL_LEGACY_SCORING=1',
            fallback_acceptance='known scoring behavior; not a second measured release identity'))
    metadata['release_declarations'] = dict(profile=pkg.DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=plan['candidate_sha256'],
        protocol_sha256=protocol['protocol_sha256'],
        files={name: h.sha(declarations / name) for name in pkg.DECLARATION_FILES})
    pkg.verify_declarations(metadata, required=True)
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    h.verify_pair(PACKAGE / 'candidate/q4t', PACKAGE / 'candidate/q4t.release.json', plan['candidate_sha256'])
    h.seal(PACKAGE)


def restore():
    plan = h.read(OUT / 'plan.json')
    original = Path(plan['execution_tools_root'])
    h.verify_seal(PACKAGE / 'previous')
    h.require(h.read(marker())['package'] == str(PACKAGE), 'foreign recovery marker')
    for name in ('q4t', 'q4t.release.json'):
        destination = original / 'build' / name
        old = h.sha(PACKAGE / 'previous' / name)
        candidate = PACKAGE / 'candidate' / name
        allowed = {old, h.sha(candidate)} if candidate.exists() else {old}
        current = h.sha(destination)
        h.require(current in allowed, 'unknown installed identity')
        if current != old:
            h.replace_checked(PACKAGE / 'previous' / name, destination, current)
    h.verify_pair(original / 'build/q4t', original / 'build/q4t.release.json', plan['parent_sha256'])
    marker().unlink()
    h.fsync_directory(marker().parent)


def deploy():
    driver.legacy.require_guard()
    plan = h.read(OUT / 'plan.json')
    health.validate(plan)
    h.require(plan == h.read(PACKAGE / 'health-plan.json'), 'health plan changed')
    original = Path(plan['execution_tools_root'])
    h.verify_seal(PACKAGE / 'previous')
    h.require(not marker().exists(), 'unfinished recovery')
    for name in ('q4t', 'q4t.release.json'):
        h.require(h.sha(original / 'build' / name) == h.sha(PACKAGE / 'previous' / name), 'baseline changed')
    h.save(marker(), dict(kind='mtp_prefill_scoring', package=str(PACKAGE),
                         plan=h.identity(OUT / 'plan.json')))
    h.fsync_directory(marker().parent)
    state, child = dict(status='INSTALLING', started=time.time()), None
    def interrupted(*_):
        raise KeyboardInterrupt('owned scoring delivery interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        h.replace_checked(PACKAGE / 'candidate/q4t', original / 'build/q4t', plan['parent_sha256'])
        with (OUT / 'driver.log').open('x') as log:
            child = subprocess.Popen([sys.executable, '-B',
                str(Path(__file__).with_name('health.py')), '--plan', str(OUT / 'plan.json')],
                cwd=ROOT, env=driver.legacy.clean_env(), stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            code = child.wait(timeout=max(.01, plan['task_deadline_unix'] - time.time()))
        post, run = h.read(OUT / 'postflight.json'), h.read(OUT / 'run/state.json')
        h.require(code == 0 and post['status'] == run['status'] == 'MEASURED_REQUIRES_REVIEW'
                  and run['requests_completed'] == 6 and run['loads_started'] == 2,
                  'installed health failed')
        h.require(post['final_monitor_barrier'] and not any(post[k] for k in
            ('monitor_failures', 'cleanup_errors', 'final_monitor_errors')), 'health monitor failed')
        release(plan)
        h.replace_checked(PACKAGE / 'candidate/q4t.release.json', original / 'build/q4t.release.json',
                          h.sha(PACKAGE / 'previous/q4t.release.json'))
        h.verify_pair(original / 'build/q4t', original / 'build/q4t.release.json', plan['candidate_sha256'])
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        if child is not None and child.poll() is None:
            driver.legacy.stop_group(child)
        state.update(status='FAILED', error=repr(error))
        try:
            restore()
            state['rollback'] = 'RESTORED_PREVIOUS'
        except BaseException as failure:
            state.update(status='RECOVERY_REQUIRED', rollback_error=repr(failure))
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUT / 'deployment.json', state)


def finalize():
    plan = h.read(OUT / 'plan.json')
    h.require(h.read(OUT / 'lease/exit.json')['status'] == 'COMPLETED', 'outer health guard')
    state = h.read(OUT / 'deployment.json')
    h.require(state['status'] == 'HEALTH_CHECKED_PENDING_GUARD' and
              state['finished'] <= plan['task_deadline_unix'], 'health completion/deadline')
    driver.legacy.verify_files(plan)
    h.verify_seal(PACKAGE)
    for step in plan['steps']:
        folder = OUT / 'run' / step['name']
        h.require(h.read(folder / 'health-result.json')['passed'], 'head health output')
    original = Path(plan['execution_tools_root'])
    metadata = h.verify_pair(original / 'build/q4t', original / 'build/q4t.release.json', plan['candidate_sha256'])
    h.require(h.read(marker())['package'] == str(PACKAGE), 'foreign recovery marker')
    marker().unlink()
    h.fsync_directory(marker().parent)
    h.save(OUT / 'finalized.json', dict(status='DELIVERED',
        binary_sha256=plan['candidate_sha256'], release=h.identity(original / 'build/q4t.release.json'),
        protocol=metadata['chat_protocol']['protocol_sha256'], generation_requests=6, model_starts=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'deploy', 'finalize', 'rollback'))
    action = parser.parse_args().action
    if action == 'rollback':
        with driver.legacy.gpu_exclusive.common_lock().open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restore()
    else:
        globals()[action]()
