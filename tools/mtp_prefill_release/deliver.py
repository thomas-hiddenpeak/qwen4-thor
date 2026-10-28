"""Accepted-only packaging, identity-checked install, bounded health, rollback."""
import argparse
import copy
import importlib.util
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('release_delivery_driver', HERE/'run_stage.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
ROOT = driver.ROOT
h = driver.module('release_delivery_helpers', 'tools/verify/startup_delivery/deploy.py')
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'
PACKAGE = BASE/'delivery-package'
OUT = BASE/'phase-h'
MARKER = ROOT/'build/mtp-prefill-scoring-recovery.json'
OLD = '6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965'
NEW = 'c0ed0800bb36f8c8b7dcf9155ae463dd146d73b217294839f0776bef138f6d2d'


def prepare():
    decision = h.read(BASE/'final-decision.json')
    audit = h.read(BASE/'final-audit.json')
    h.require(decision['passed'] and decision['candidate_sha256'] == NEW and
              decision['engineering_decision'] == 'ACCEPT_BOUNDED_PREFILL_SCORING',
              'formal candidate acceptance')
    h.require(audit['passed'] and audit['cumulative_http'] == 296 and
              audit['cumulative_starts'] == 20 and audit['new_gpu_fixtures'] == 0,
              'complete fixed validation and remaining health budget')
    for phase in ['a', 'b', 's', 'c']:
        gate = h.read(BASE/('gate-'+phase+'.json'))
        h.require(gate['passed'] and gate['candidate_sha256'] == NEW, 'phase gate')
    prior = h.read(BASE/'phase-a/plan.json')
    driver.legacy.verify_files(prior)
    old = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD)
    h.require(time.time() < prior['task_deadline_unix'], 'release deadline')
    PACKAGE.mkdir()
    for name in ['previous', 'candidate', 'tested-source', 'evidence']:
        (PACKAGE/name).mkdir()
    copies = [(ROOT/'build/q4t', 'previous/q4t'),
        (ROOT/'build/q4t.release.json', 'previous/q4t.release.json'),
        (Path(old['build_cache']), 'previous/CMakeCache.txt'),
        (Path(prior['candidate']), 'candidate/q4t'),
        (BASE/'frozen/CMakeCache.txt', 'candidate/CMakeCache.txt'),
        (BASE/'final-decision.json', 'decision.json'),
        (BASE/'final-audit.json', 'evidence/final-audit.json'),
        (ROOT/'docs/MTP_PREFILL_RELEASE_RESULT_2026-10-29.md', 'acceptance.md'),
        (OUT/'plan.json', 'health-template.json'),
        (Path(__file__), 'deliver.py'), (HERE/'run_stage.py', 'run_stage.py')]
    for source, name in copies:
        h.copy_checked(h.identity(source), PACKAGE/name)
    shutil.copytree(prior['artifact'], PACKAGE/'artifact')
    for name in ['head.bf16', 'token_ids.bin', 'manifest.json']:
        h.require(h.sha(PACKAGE/'artifact'/name) == prior['files'][str(Path(prior['artifact'])/name)],
                  'subset artifact')
    runtime = {str(Path(path).relative_to(ROOT)): digest for path, digest in prior['files'].items()
        if Path(path).is_relative_to(ROOT/'src') or Path(path).is_relative_to(ROOT/'include') or
        Path(path) == ROOT/'CMakeLists.txt'}
    for name, digest in runtime.items():
        h.require(h.sha(ROOT/name) == digest, 'runtime source changed')
        target = PACKAGE/'tested-source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT/name), target)
    h.save(PACKAGE/'candidate/runtime-source.json', runtime)
    for name in ['configure.log', 'build.log', 'build-final.log']:
        h.copy_checked(h.identity(ROOT/'.q4t-work/mtp-prefill-scoring-20261029'/name),
                       PACKAGE/'candidate'/name)
    metadata = copy.deepcopy(old)
    metadata.update(version='mtp-prefill-scoring-k3-20261029', binary_sha256=NEW,
        source_commit=prior['runtime_source_commit'],
        source_note='Unchanged research binary, rebuilt at c9d4475. Runtime manifest binds tested sources; release tools and documentation were added later.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],
        acceptance_snapshot=str(PACKAGE/'acceptance.md'), acceptance_snapshot_sha256=h.sha(PACKAGE/'acceptance.md'),
        acceptance_decision=str(PACKAGE/'decision.json'), acceptance_decision_sha256=h.sha(PACKAGE/'decision.json'),
        acceptance_audit=str(PACKAGE/'evidence/final-audit.json'),
        acceptance_audit_sha256=h.sha(PACKAGE/'evidence/final-audit.json'),
        previous_binary=str(PACKAGE/'previous/q4t'), previous_release=str(PACKAGE/'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE/'artifact'), subset_manifest_sha256=h.sha(PACKAGE/'artifact/manifest.json'),
        scope=decision['accepted_scope'],
        prefill_scoring=dict(uniform_slot_rows_greater_than=4, pooled_state_addressing=True,
            scoring='existing BF16 GEMM', legacy_diagnostic_env='Q4T_MTP_PREFILL_LEGACY_SCORING=1',
            arbitrary_input_bitwise_equivalence=False))
    h.save(PACKAGE/'candidate/q4t.release.json', metadata)
    h.verify_pair(PACKAGE/'candidate/q4t', PACKAGE/'candidate/q4t.release.json', NEW)
    h.verify_pair(PACKAGE/'previous/q4t', PACKAGE/'previous/q4t.release.json', OLD,
                  PACKAGE/'previous/CMakeCache.txt')
    plan = h.read(OUT/'plan.json')
    template_sha = h.sha(OUT/'plan.json')
    plan['artifact'] = str(PACKAGE/'artifact')
    for path in [*PACKAGE.rglob('*'), Path(__file__), HERE/'run_stage.py',
                 *(ROOT/'tools/verify/startup_delivery').glob('*.py')]:
        if path.is_file():
            plan['files'][str(path)] = h.sha(path)
    for name in ['q4t', 'q4t.release.json']:
        plan['files'][str(ROOT/'build'/name)] = h.sha(PACKAGE/'candidate'/name)
    driver.validate(plan)
    h.save(PACKAGE/'health-plan.json', plan)
    h.replace_checked(PACKAGE/'health-plan.json', OUT/'plan.json', template_sha)
    h.save(BASE/'package-receipt.json', dict(seal=h.seal(PACKAGE),
        runtime_files=len(runtime), requests=6, starts=2, original_health_template_sha256=template_sha,
        health_plan_sha256=h.sha(OUT/'plan.json'), request_scope_and_deadline_unchanged=True))


def restore():
    h.verify_seal(PACKAGE)
    if MARKER.exists():
        h.require(h.read(MARKER)['package'] == str(PACKAGE),
                  'foreign recovery marker')
    for name in ['q4t', 'q4t.release.json']:
        path = ROOT/'build'/name
        old, new = h.sha(PACKAGE/'previous'/name), h.sha(PACKAGE/'candidate'/name)
        current = h.sha(path)
        h.require(current in [old, new], 'unknown installed identity')
        if current != old:
            h.replace_checked(PACKAGE/'previous'/name, path, new)
    h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD)
    if MARKER.exists():
        marker = h.read(MARKER)
        h.require(marker['package'] == str(PACKAGE), 'foreign recovery marker')
        MARKER.unlink()
        h.fsync_directory(MARKER.parent)


def deploy():
    h.require_guard(OUT)
    h.verify_seal(PACKAGE)
    plan = h.read(OUT/'plan.json')
    driver.validate(plan)
    h.require(plan == h.read(PACKAGE/'health-plan.json') and not MARKER.exists(),
              'frozen health/recovery state')
    for name in ['q4t', 'q4t.release.json']:
        h.require(h.sha(ROOT/'build'/name) == h.sha(PACKAGE/'previous'/name), 'baseline identity')
    h.save(MARKER, dict(package=str(PACKAGE), candidate=NEW, previous=OLD))
    h.fsync_directory(MARKER.parent)
    state, child = dict(status='INSTALLING', started=time.time()), None
    def interrupted(*_):
        raise KeyboardInterrupt('owned deployment interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in ['q4t', 'q4t.release.json']:
            h.replace_checked(PACKAGE/'candidate'/name, ROOT/'build'/name,
                              h.sha(PACKAGE/'previous'/name))
        h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', NEW)
        with (OUT/'health-driver.log').open('x') as log:
            child = subprocess.Popen([sys.executable, '-B', str(HERE/'run_stage.py'),
                '--plan', str(OUT/'plan.json')], cwd=ROOT, env=driver.legacy.clean_env(),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            code = child.wait(timeout=max(.01, min(900, plan['task_deadline_unix']-time.time())))
        h.require(code == 0, 'installed health failed')
        post, run = h.read(OUT/'postflight.json'), h.read(OUT/'run/state.json')
        h.require(post['status'] == run['status'] == 'MEASURED_REQUIRES_REVIEW' and
                  run['requests_completed'] == 6 and run['loads_started'] == 2, 'health completion')
        h.require(not any(post[key] for key in ['monitor_failures', 'cleanup_errors', 'final_monitor_errors'])
                  and bool(post['final_monitor_barrier']), 'health final monitor')
        h.verify_seal(PACKAGE)
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
        h.save(OUT/'deployment.json', state)


def finalize():
    h.require(h.read(OUT/'lease/exit.json')['status'] == 'COMPLETED', 'outer guard')
    h.require(h.read(OUT/'deployment.json')['status'] == 'HEALTH_CHECKED_PENDING_GUARD', 'health state')
    h.require(h.read(BASE/'gate-h.json')['passed'], 'installed HTTP audit')
    audit = h.read(BASE/'final-audit-health.json')
    h.require(audit['passed'] and audit['cumulative_http'] == 302 and
              audit['cumulative_starts'] == 22 and audit['new_gpu_fixtures'] == 0,
              'complete final identity, usage, environment and monitor audit')
    h.verify_seal(PACKAGE)
    h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', NEW)
    marker = h.read(MARKER)
    h.require(marker['candidate'] == NEW and marker['package'] == str(PACKAGE), 'recovery identity')
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    h.save(OUT/'finalized.json', dict(status='DELIVERED', binary_sha256=NEW,
        sidecar_sha256=h.sha(ROOT/'build/q4t.release.json'), health_requests=6,
        health_starts=2, persistent_service=False, finished=time.time()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'deploy', 'finalize', 'rollback'])
    args = parser.parse_args()
    if args.action == 'rollback':
        driver.legacy.require_guard()
        restore()
    else:
        globals()[args.action]()
