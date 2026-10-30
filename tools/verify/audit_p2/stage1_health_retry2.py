"""Final bounded health retry after the user-authorized OpenCode shutdown.

Keep both ENV_ABORT attempts, the unchanged package, and the original deadline.
The two remaining starts consume the original twelve-start maximum.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

import stage1_health_retry as r

s, h = r.s, r.h
FIRST = r.RETAINED
FIRST_PLAN = r.RETRY
r.RETAINED = s.BASE / 'deployment-env-abort-2'
r.RETRY = s.BASE / 'health-retry-plan-2.json'
STOP = s.BASE.parent / 'opencode-stop/stopped.json'


def verify_previous():
    retry = h.read(FIRST_PLAN)
    for name in ['tool', 'original_plan', 'recovery']:
        h.check_identity(retry[name])
    h.require(h.verify_seal(FIRST) == retry['retained_seal'],
              'first aborted attempt changed')
    stop = h.read(STOP)
    h.require(stop['returncode'] == 0 and not stop['original_pid_exists'] and
              stop['authorized_service'] == 'opencode-serve.service',
              'authorized OpenCode shutdown')
    status = subprocess.check_output(['systemctl', '--user', 'show',
        'opencode-serve.service', '-p', 'ActiveState', '--value'], text=True)
    h.require(status.strip() == 'inactive', 'OpenCode service restarted')


def prepare():
    verify_previous()
    plan = h.read(s.BASE / 'plan.json')
    s.verify_inputs(plan)
    s.verify_delivery()
    outcome = h.read(r.OUT / 'lease/exit.json')
    state = h.read(r.OUT / 'deployment.json')
    ledger = [json.loads(line) for line in
              (r.OUT / 'run/ledger.jsonl').read_text().splitlines()]
    h.require(outcome['status'] == 'ENV_ABORT' and 'TIMEOUT' in
              outcome['reason'], 'specific second monitoring failure')
    h.require(state['status'] == 'FAILED' and state['rollback'] == 'RESTORED',
              'second automatic rollback')
    h.require(len(ledger) == 1 and ledger[0]['kind'] == 'startup' and
              ledger[0]['http'] == 0 and ledger[0]['starts'] == 1,
              'second attempt consumed one start and zero HTTP')
    measurement = h.read(s.BASE / 'measurement/result.json')
    h.require(measurement['http'] == 16 and measurement['starts'] == 8,
              'measurement budget accounting')
    fresh = s.nvml_process_query.query(timeout=5)
    recovery = s.BASE / 'monitor-recovery-2.json'
    h.save(recovery, fresh)
    h.require(fresh['status'] == 'OK' and not fresh['pids'],
              'monitor or device has not recovered')
    h.require(time.time() < plan['deadline_unix'] - 600,
              'original absolute health deadline')
    for name in ['q4t', 'q4t.release.json']:
        h.require(s.sha(s.ROOT / 'build' / name) == s.sha(s.PREVIOUS / name),
                  'default rollback identity')
    h.require(not s.MARKER.exists() and not r.RETAINED.exists(),
              'recovery state or retained destination')
    before = {str(p.relative_to(r.OUT)): s.sha(p) for p in r.OUT.rglob('*')
              if p.is_file()}
    shutil.move(r.OUT, r.RETAINED)
    h.copy_checked(h.identity(s.BASE.parent / 'stage1-deployment-driver-retry1.log'),
                   r.RETAINED / 'driver.log')
    retained_seal = h.seal(r.RETAINED)
    r.OUT.mkdir()
    h.copy_checked(h.identity(s.PACKAGE / 'health-plan.json'),
                   r.OUT / 'health-plan.json')
    h.save(r.RETRY, dict(original_plan=h.identity(s.BASE / 'plan.json'),
        tool=h.identity(Path(__file__)), retained_seal=retained_seal,
        first_plan=h.identity(FIRST_PLAN), first_seal=h.verify_seal(FIRST),
        shutdown=h.identity(STOP), original_output=str(r.OUT),
        retained_output=str(r.RETAINED), files=before,
        recovery=h.identity(recovery), deadline_unix=plan['deadline_unix'],
        retained_http=0, retained_starts=2, retry_http=6, retry_starts=2,
        total_http=22, total_starts=12, runtime_and_measurement_reused=True,
        monitor_unchanged=True))
    print('Final original health group: total HTTP=22/26, starts=12/12.')


def finalize():
    verify_previous()
    retry = h.read(r.RETRY)
    for name in ['first_plan', 'shutdown']:
        h.check_identity(retry[name])
    h.require(h.verify_seal(FIRST) == retry['first_seal'], 'first abort seal')
    r.finalize()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'finalize'])
    globals()[parser.parse_args().action]()
