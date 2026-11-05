"""Prepare the two unexecuted memory-stop requests; run requires approval."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_deploy import stage as s

ORIGINAL = ROOT / '.q4t-work/serve-bounded-deploy-20261105'
BASE = ORIGINAL / 'continuation'
LIMITS = dict(starts=1, generation_attempts=2, controls=80, seconds=240)


def configure():
    s.BASE, s.PLAN, s.OUT = BASE, BASE / 'plan.json', BASE / 'execution'
    s.LIMITS, s.GPU_SECONDS = LIMITS, 180
    s.check = check
    s.configure()


def original():
    plan = s.pkg.read(ORIGINAL / 'plan.json')
    result = s.pkg.read(ORIGINAL / 'execution/result.json')
    s.pkg.require(result['status'] == 'FAILED' and
        result['error'] == "KeyError('q4t_cuda_quota_live_bytes')" and
        result['counts'] == dict(starts=1, generation_attempts=4, controls=3) and
        result['cleanup_confirmed'], 'unexpected original outcome')
    terminal = s.pkg.read(ORIGINAL / 'execution/managed-run/terminal.json')
    s.pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
        terminal['guard']['status'] == 'COMPLETED' and terminal['worker']['attempts'] == 1 and
        terminal['worker']['restarts'] == 0 and
        s.pkg.read(ORIGINAL / 'execution/custody/exit.json')['status'] == 'COMPLETED',
        'original cleanup incomplete')
    raw = s.pkg.read(ORIGINAL / 'execution/business/business-final-http.json')['body']
    quota = s.cuda_accounting(raw)
    metrics, _ = s.base.contracts.parse_metrics(raw, version=5)
    s.pkg.require(metrics['q4t_requests_success_total'] == 4 and
        metrics['q4t_structured_workers_started_total'] ==
        metrics['q4t_structured_workers_reaped_total'] == 4 and
        all(metrics[name] == 0 for name in s.base.client.a.EMPTY), 'reused business accounting')
    return plan, quota


def prepare():
    configure()
    s.pkg.require(not BASE.exists(), 'preserve follow-up attempt')
    old, quota = original()
    BASE.mkdir(mode=0o700)
    tested = BASE / 'cpu-01'
    tested.mkdir()
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_deploy.test_profile.Driver')
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (tested / 'tests.log').write_text(log.getvalue())
    s.pkg.save(tested / 'result.json', dict(passed=result.wasSuccessful() and not result.skipped,
        tests_run=result.testsRun, sources=s.sources(), reused_cpu_checks=13, models=0, http=0))
    s.pkg.require(result.wasSuccessful() and not result.skipped, log.getvalue())
    refs = {str(p): s.pkg.sha(p) for p in ORIGINAL.rglob('*') if p.is_file() and
            not p.is_relative_to(BASE)}
    plan = dict(old, limits=LIMITS, gpu_seconds=180, sources=s.sources(),
        cpu_sha256=s.pkg.sha(tested / 'result.json'), original_references=refs,
        reused_quota=quota, reused_result=str(ORIGINAL / 'execution/result.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                             text=True).strip(),
        planned=dict(models=1, responses=0, real_tools=0, interrupted_streams=2),
        authorization_required='one additional model start for remaining two requests',
        cumulative=dict(models=2, generation_attempts=6, seconds=420, gpu_seconds=300))
    s.pkg.save(s.PLAN, plan)
    check()
    return dict(prepared=True, models=0, gpu=0, cpu_checks=result.testsRun,
                additional_starts=1, remaining_requests=2, gpu_seconds=180)


def check():
    value = s.ORIGINAL_CHECK()
    s.pkg.require(value['limits'] == LIMITS and value['gpu_seconds'] == 180, 'follow-up budget')
    for name, checksum in value['original_references'].items():
        s.pkg.require(s.pkg.sha(name) == checksum, 'original record changed')
    s.profile.check(value['deployment']['profile'], value['deployment']['sha256'])
    return value


class Run(s.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.expected_workers = 1

    def business(self):
        # The unchanged package's four successful responses remain in the
        # original receipt. No repeat of accepted business in this process.
        metrics, raw = self.get_metrics('initial-quota')
        s.pkg.require(metrics['q4t_requests_success_total'] == 0 and
                      metrics['q4t_structured_workers_started_total'] == 0, 'unexpected business')
        s.pkg.save(s.OUT / 'cuda-accounting.json', s.cuda_accounting(raw))


def run(approved):
    s.pkg.require(approved, 'explicit follow-up approval required; no model started')
    configure()
    plan = check()
    s.pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                  'commit reviewed continuation first')
    s.OUT.mkdir(mode=0o700)
    with s.base.sup.processes.child_subreaper():
        return Run(plan).execute()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    parser.add_argument('--approved-followup', action='store_true')
    args = parser.parse_args()
    configure()
    print(json.dumps(run(args.approved_followup) if args.action == 'run' else
                     globals()[args.action]()))
