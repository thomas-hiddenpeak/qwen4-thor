"""Prepare/check the new default's operator-owned day plan; run is explicit."""
import argparse
import copy
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c
from tools.serve_soak import stage as soak
from tools.serve_service import package as pkg

PHASE = c.BASE / 'operator-soak'
PACKAGE = c.BASE / 'operator-package'
OLD = ROOT / '.q4t-work/serve-soak-20261103'


def configure():
    soak.PHASE = PHASE
    soak.CPU = OLD / 'cpu'
    soak.configure('day')


def prepare():
    configure()
    c.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed handoff code first')
    c.require(not soak.PLAN.exists() and not soak.OUT.exists(), 'preserve prior operator run')
    c.require(c.read(c.BASE / 'execution-v2/finalized.json')['status'] == 'DELIVERED',
              'short validation/default delivery incomplete')
    tested = c.read(soak.CPU / 'result.json')
    c.require(tested['passed'] and tested['tests_run'] == 11, 'soak CPU qualification')
    for name, digest in tested['sources'].items():
        c.require(c.sha(ROOT / name) == digest, 'unchanged soak code required')
    plan = copy.deepcopy(c.read(OLD / 'day/plan.json'))
    model = c.read(c.BASE / 'package-v2/plan.json')['model_dir']
    receipt = pkg.prepare(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                          model, PACKAGE, port=18126, restart_limit=0)
    preparation = dict(status='PREPARED_FOR_OPERATOR_VALIDATION', package=receipt,
        new_binary_short_acceptance=c.identity(c.BASE / 'execution-v2/business/result.json'),
        unchanged_soak_cpu=c.identity(soak.CPU / 'result.json'),
        prior_managed_package=c.identity(ROOT / '.q4t-work/serve-inspection-20261103/accepted-package.json'),
        new_package_managed_e2e_executed=False, new_package_long_run_executed=False,
        note='New default passed direct serve startup/shutdown and business checks. '
             'Unchanged controller/soak code evidence is reused. This new package '
             'has not been run under its manager; operator run also verifies that integration.')
    c.save(c.BASE / 'operator-package-preparation.json', preparation)
    token = secrets.token_hex(10)
    plan.pop('accepted_package_sha256', None)
    plan.update(package=receipt, package_preparation=c.identity(
        c.BASE / 'operator-package-preparation.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                             cwd=ROOT, text=True).strip(),
        default_release_sha256=c.sha(ROOT / 'build/q4t.release.json'),
        unit='q4t-soak-quota-' + token + '.service', token=token,
        handoff_source=c.identity(Path(__file__)),
        execution_note='Prepared only. Explicit operator run; no new manager or day acceptance yet.')
    soak.PLAN.parent.mkdir(parents=True)
    c.save(soak.PLAN, plan)
    check()
    print('PREPARED_ONLY', soak.PLAN)


def check():
    configure()
    plan = soak.check()
    for key in ('handoff_source', 'package_preparation'):
        c.require(c.identity(plan[key]['path']) == plan[key], 'handoff identity changed')
    return plan


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'check', 'run', 'report'])
    parser.add_argument('--operator-run-day', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'check':
        value = check()
        print(json.dumps(dict(prepared=True, plan=str(soak.PLAN),
            binary=value['package']['binary_sha256'],
            execution_exists=soak.OUT.exists(), gpu_or_model_started=False)))
    elif args.action == 'run':
        c.require(args.operator_run_day, 'day execution requires --operator-run-day')
        print(json.dumps(soak.Run(check()).execute()))
    else:
        check()
        from tools.serve_soak import report
        print(json.dumps(report.check(), indent=2))
