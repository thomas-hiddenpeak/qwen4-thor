"""Reconcile the current package using the previously accepted lifecycle audit."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_current_release import managed as stage
from tools.serve_operations_managed import report

stage.configure()
report.ARCHIVE=ROOT/'docs/evidence/serve-current-managed-20261105'


def archive():
    stage.pkg.require(not report.ARCHIVE.exists(),'preserve accepted archive')
    report.archive()
    plan=stage.pkg.read(stage.PLAN)
    stage.pkg.save(stage.BASE/'accepted-package.json',dict(package=plan['package'],
        acceptance='ACCEPT_CURRENT_MANAGED_SHORT',previous_package=plan['previous_package'],
        normal_cli_managed_lifecycle_accepted=True,installed_service=False,
        long_run=False,task_protocol_migration_accepted=False,
        evidence=str(report.ARCHIVE/'records.tar.gz'),
        archive_sha256=stage.pkg.sha(report.ARCHIVE/'records.tar.gz')))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('archive','verify'))
    action=p.parse_args().action
    if action=='archive':archive()
    else:report.verify()
