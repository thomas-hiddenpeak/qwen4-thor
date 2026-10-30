"""Retain one monitoring ENV_ABORT and retry only the unexecuted health group.

No deadline reset, monitor fallback or candidate rebuild. The original frozen
deployment code/package is reused; this receipt accounts for the extra start.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
import stage1 as s

h, BASE, OUT = s.h, s.BASE, s.DEPLOYMENT
RETAINED = BASE / 'deployment-env-abort'
RETRY = BASE / 'health-retry-plan.json'


def prepare():
    plan = h.read(BASE / 'plan.json')
    s.verify_inputs(plan)
    s.verify_delivery()
    outcome = h.read(OUT / 'lease/exit.json')
    state = h.read(OUT / 'deployment.json')
    ledger = [json.loads(line) for line in
              (OUT / 'run/ledger.jsonl').read_text().splitlines()]
    h.require(outcome['status'] == 'ENV_ABORT' and 'CLEANUP_FAILED' in
              outcome['reason'], 'specific retained monitoring failure')
    h.require(state['status'] == 'FAILED' and state['rollback'] == 'RESTORED',
              'verified automatic rollback')
    h.require(len(ledger) == 1 and ledger[0]['kind'] == 'startup' and
              ledger[0]['http'] == 0 and ledger[0]['starts'] == 1,
              'failed attempt consumed exactly one start and no generation')
    failed = [json.loads(line) for line in
              (OUT / 'lease/nvml-queries.jsonl').read_text().splitlines()
              if json.loads(line)['status'] != 'OK']
    h.require(len(failed) == 1 and failed[0]['status'] == 'CLEANUP_FAILED',
              'single failed query')
    # The failed worker is now gone. A fresh query must complete and reap its
    # worker before any retry, and the guard still performs its own preflight.
    h.require(not Path('/proc', str(failed[0]['worker_pid'])).exists(),
              'original query worker still exists')
    fresh = s.nvml_process_query.query(timeout=5)
    h.save(BASE / 'monitor-recovery.json', fresh)
    h.require(fresh['status'] == 'OK' and not fresh['pids'],
              'monitor or device has not recovered')
    h.require(time.time() < plan['deadline_unix'] - 600,
              'original absolute health deadline')
    for name in ['q4t', 'q4t.release.json']:
        h.require(s.sha(s.ROOT / 'build' / name) == s.sha(s.PREVIOUS / name),
                  'default rollback identity')
    h.require(not s.MARKER.exists() and not RETAINED.exists(),
              'recovery state or retained destination')
    before = {str(p.relative_to(OUT)): s.sha(p) for p in OUT.rglob('*')
              if p.is_file()}
    shutil.move(OUT, RETAINED)
    h.copy_checked(h.identity(BASE.parent / 'stage1-deployment-driver.log'),
                   RETAINED / 'driver.log')
    retained_seal = h.seal(RETAINED)
    OUT.mkdir()
    h.copy_checked(h.identity(s.PACKAGE / 'health-plan.json'),
                   OUT / 'health-plan.json')
    h.save(RETRY, dict(original_plan=h.identity(BASE / 'plan.json'),
        tool=h.identity(Path(__file__)), retained_seal=retained_seal,
        original_output=str(OUT), retained_output=str(RETAINED), files=before,
        recovery=h.identity(BASE / 'monitor-recovery.json'),
        deadline_unix=plan['deadline_unix'], retained_http=0, retained_starts=1,
        retry_http=6, retry_starts=2, total_http=22, total_starts=11,
        runtime_and_measurement_reused=True, monitor_unchanged=True))
    print('Only the original six health requests will run; total starts=11/12.')


def finalize():
    retry = h.read(RETRY)
    for name in ['tool', 'original_plan', 'recovery']:
        h.check_identity(retry[name])
    h.require(h.verify_seal(RETAINED) == retry['retained_seal'],
              'retained ENV_ABORT evidence')
    guard, state = h.read(OUT / 'lease/exit.json'), h.read(OUT / 'deployment.json')
    result = h.read(OUT / 'run/result.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'retry deployment guard')
    h.require(state['status'] == 'HEALTH_CHECKED_PENDING_GUARD' and
              state['finished'] < retry['deadline_unix'] and result['passed']
              and result['http'] == 6 and result['starts'] == 2,
              'health coverage within original deadline')
    s.verify_delivery()
    h.verify_pair(s.ROOT / 'build/q4t', s.ROOT / 'build/q4t.release.json',
                  s.sha(s.PACKAGE / 'candidate/q4t'))
    h.require(s.sha(s.ROOT / 'build/q4t.release.json') ==
              s.sha(s.PACKAGE / 'candidate/q4t.release.json'), 'sidecar identity')
    h.require(h.read(s.MARKER)['package'] == str(s.PACKAGE), 'owned marker')
    s.MARKER.unlink()
    h.fsync_directory(s.MARKER.parent)
    h.save(OUT / 'finalized.json', dict(status='DELIVERED',
        binary_sha256=s.sha(s.ROOT / 'build/q4t'),
        sidecar_sha256=s.sha(s.ROOT / 'build/q4t.release.json'),
        total_http=retry['total_http'], total_starts=retry['total_starts'],
        gpu_processes=2, retained_env_abort=retry['retained_seal'],
        retry_plan=h.identity(RETRY), persistent_service=False,
        finished=time.time()))
    archive = BASE / 'health-archive'
    shutil.copytree(OUT, archive)
    h.copy_checked(h.identity(RETRY), archive / 'retry-plan.json')
    h.save(BASE / 'health-archive-receipt.json', dict(seal=h.seal(archive)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'finalize'])
    globals()[parser.parse_args().action]()
