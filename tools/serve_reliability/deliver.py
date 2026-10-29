"""Accepted-only release packaging, identity-checked install and rollback."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-reliability-20261030'
PACKAGE = BASE/'delivery-package'
OUT = BASE/'deployment'
MARKER = ROOT/'build/serve-reliability-recovery.json'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


h = module('serve_delivery_helpers', ROOT/'tools/verify/startup_delivery/deploy.py')
health = module('serve_delivery_health', Path(__file__).with_name('health.py'))
sys.path.insert(0, str(ROOT/'tools/evalscope'))
import gpu_exclusive


def guarded():
    lease = h.read(gpu_exclusive.common_lock())
    h.require(lease.get('state') == 'held' and lease['pid'] == os.getppid(),
              'deployment/rollback needs outer GPU lease')


def prepare():
    plan, decision = h.read(BASE/'http-plan.json'), h.read(BASE/'decision.json')
    h.require(decision['passed'] and decision['engineering_decision'] ==
              'ACCEPT_BOUNDED_SERVE_RELIABILITY', 'accepted reliability audit')
    h.require(h.read(BASE/'lease/exit.json')['status'] == 'COMMAND_FAILED' and
              h.read(BASE/'remaining-lease/exit.json')['status'] == 'COMPLETED' and
              decision['retained_parser_failure']['passed'], 'measurement guards/audit')
    h.require(time.time() < plan['deadline_unix']-600, 'health deadline reserve')
    for path, digest in plan['files'].items():
        checked = decision['frozen_tool_relocation'].get(path, path)
        h.require(h.sha(checked) == digest, 'frozen identity changed: '+path)
    for path, digest in decision['evidence'].items():
        h.require(h.sha(path) == digest, 'accepted evidence changed: '+path)
    for path, digest in h.read(BASE/'http-remaining-plan.json')['files'].items():
        h.require(h.sha(path) == digest, 'repaired tool/runtime identity changed: '+path)
    baseline = h.read(BASE/'baseline/identity.json')
    old = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
                        baseline['binary_sha256'])
    h.require(h.sha(ROOT/'build/q4t.release.json') == baseline['release_sha256'],
              'installed metadata changed')
    PACKAGE.mkdir()
    for name in ['previous', 'candidate', 'tested-source', 'evidence']:
        (PACKAGE/name).mkdir()
    copies = [('baseline/q4t', 'previous/q4t'),
              ('baseline/q4t.release.json', 'previous/q4t.release.json'),
              ('baseline/CMakeCache.txt', 'previous/CMakeCache.txt'),
              ('frozen/candidate', 'candidate/q4t'),
              ('frozen/CMakeCache.txt', 'candidate/CMakeCache.txt'),
              ('frozen/runtime-source.json', 'candidate/runtime-source.json'),
              ('decision.json', 'decision.json'),
              ('dependency-review.json', 'evidence/dependency-review.json'),
              ('http-plan.json', 'evidence/http-plan.json')]
    for source, destination in copies:
        h.copy_checked(h.identity(BASE/source), PACKAGE/destination)
    h.copy_checked(h.identity(ROOT/'docs/SERVE_RELIABILITY_PLAN_2026-10-30.md'),
                   PACKAGE/'acceptance-contract.md')
    runtime = h.read(PACKAGE/'candidate/runtime-source.json')
    for relative, digest in runtime.items():
        h.require(h.sha(ROOT/relative) == digest, 'runtime source changed')
        target = PACKAGE/'tested-source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT/relative), target)
    shutil.copytree(plan['artifact'], PACKAGE/'artifact')
    for name in ['head.bf16', 'token_ids.bin', 'manifest.json']:
        h.require(h.sha(PACKAGE/'artifact'/name) ==
                  plan['files'][str(Path(plan['artifact'])/name)], 'head identity')
    # A separate receipt-backed copy preserves every raw accepted record.
    archive = BASE/'evidence-archive'
    archive.mkdir()
    for path, digest in decision['evidence'].items():
        relative = Path(path).relative_to(BASE)
        target = archive/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(path), target)
        h.require(h.sha(target) == digest, 'raw archive copy')
    receipt = dict(passed=True, files=len(decision['evidence']),
                   seal=h.seal(archive), original=decision['evidence'])
    h.save(BASE/'archive-receipt.json', receipt)
    h.copy_checked(h.identity(BASE/'archive-receipt.json'), PACKAGE/'evidence/archive-receipt.json')
    metadata = copy.deepcopy(old)
    metadata.update(version='serve-reliability-20261030',
        binary_sha256=h.sha(PACKAGE/'candidate/q4t'), source_commit=plan['source_commit'],
        source_note='Serving reliability only; tested manifest binds source. '
                    'Fault injection disabled in the installed build.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],
        acceptance_snapshot=str(PACKAGE/'acceptance-contract.md'),
        acceptance_snapshot_sha256=h.sha(PACKAGE/'acceptance-contract.md'),
        acceptance_decision=str(PACKAGE/'decision.json'),
        acceptance_decision_sha256=h.sha(PACKAGE/'decision.json'),
        acceptance_audit=str(PACKAGE/'evidence/archive-receipt.json'),
        acceptance_audit_sha256=h.sha(PACKAGE/'evidence/archive-receipt.json'),
        previous_binary=str(PACKAGE/'previous/q4t'),
        previous_release=str(PACKAGE/'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE/'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE/'artifact/manifest.json'),
        serving=dict(utf8='complete scalars per event; invalid terminal bytes replaced',
            send_success='complete local write and request completion wins cancellation',
            ttft='streaming first nonempty content local write',
            target_ready='separate legacy first-target-ready metric',
            logical_write_deadline_seconds=30, terminal_write_budget_ms=250,
            batching='existing all-active selection', fault_injection=False,
            performance_acceptance=False))
    h.save(PACKAGE/'candidate/q4t.release.json', metadata)
    h.verify_pair(PACKAGE/'candidate/q4t', PACKAGE/'candidate/q4t.release.json',
                  decision['candidate_sha256'])
    h.verify_pair(PACKAGE/'previous/q4t', PACKAGE/'previous/q4t.release.json',
                  decision['parent_sha256'], PACKAGE/'previous/CMakeCache.txt')
    for name in ['deliver.py', 'health.py', 'analyze_http.py']:
        h.copy_checked(h.identity(Path(__file__).with_name(name)), PACKAGE/name)
    h.copy_checked(h.identity(ROOT/'tools/evalscope/serve_reliability.py'), PACKAGE/'http-runner.py')
    health_plan = dict(candidate=str(ROOT/'build/q4t'), artifact=str(PACKAGE/'artifact'),
        output=str(OUT/'run'), model=plan['model'], port=plan['port'],
        cache_prompt=plan['cache_prompt'], deadline_unix=plan['deadline_unix'])
    OUT.mkdir()
    h.save(OUT/'health-plan.json', health_plan)
    h.save(PACKAGE/'health-plan.json', health_plan)
    h.save(BASE/'package-receipt.json', dict(seal=h.seal(PACKAGE),
                                           runtime_files=len(runtime)))


def restore():
    h.verify_seal(PACKAGE)
    if MARKER.exists():
        h.require(h.read(MARKER)['package'] == str(PACKAGE), 'foreign marker')
    for name in ['q4t', 'q4t.release.json']:
        destination = ROOT/'build'/name
        old, new = h.sha(PACKAGE/'previous'/name), h.sha(PACKAGE/'candidate'/name)
        current = h.sha(destination)
        h.require(current in [old, new], 'unknown installed identity; no overwrite')
        if current != old:
            h.replace_checked(PACKAGE/'previous'/name, destination, new)
    h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
                  h.sha(PACKAGE/'previous/q4t'))
    if MARKER.exists():
        MARKER.unlink()
        h.fsync_directory(MARKER.parent)


def deploy():
    guarded()
    h.verify_seal(PACKAGE)
    plan = h.read(OUT/'health-plan.json')
    h.require(plan == h.read(PACKAGE/'health-plan.json') and not MARKER.exists(),
              'health plan/recovery identity')
    h.require(time.time() < plan['deadline_unix']-600, 'health time reserve')
    for name in ['q4t', 'q4t.release.json']:
        h.require(h.sha(ROOT/'build'/name) == h.sha(PACKAGE/'previous'/name),
                  'installed parent identity')
    h.save(MARKER, dict(package=str(PACKAGE), candidate=h.sha(PACKAGE/'candidate/q4t')))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING', started=time.time())
    def interrupted(*_):
        raise KeyboardInterrupt('owned deployment interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in ['q4t', 'q4t.release.json']:
            h.replace_checked(PACKAGE/'candidate'/name, ROOT/'build'/name,
                              h.sha(PACKAGE/'previous'/name))
        health.Health(plan).run()
        result = h.read(OUT/'run/result.json')
        h.require(result['passed'] and result['http']==6 and result['starts']==2,
                  'bounded installed health')
        h.verify_seal(PACKAGE)
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        try:
            restore()
            state['rollback'] = 'RESTORED_PREVIOUS'
        except BaseException as failure:
            state.update(status='RECOVERY_REQUIRED', rollback_error=repr(failure))
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUT/'deployment.json', state)


def finalize():
    guard = h.read(OUT/'lease/exit.json')
    h.require(guard['status']=='COMPLETED' and guard['child_returncode']==0,
              'deployment guard')
    state = h.read(OUT/'deployment.json')
    h.require(state['status']=='HEALTH_CHECKED_PENDING_GUARD', 'health state')
    plan = h.read(OUT/'health-plan.json')
    h.require(state['finished'] < plan['deadline_unix'], 'absolute deadline')
    h.verify_seal(PACKAGE)
    h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
                  h.sha(PACKAGE/'candidate/q4t'))
    h.require(h.read(MARKER)['package']==str(PACKAGE), 'owned marker')
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    h.save(OUT/'finalized.json', dict(status='DELIVERED',
        binary_sha256=h.sha(ROOT/'build/q4t'),
        sidecar_sha256=h.sha(ROOT/'build/q4t.release.json'), http=6, starts=2,
        persistent_service=False, finished=time.time()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'deploy', 'finalize', 'rollback'])
    args = parser.parse_args()
    if args.action == 'rollback':
        guarded()
        restore()
    else:
        globals()[args.action]()
