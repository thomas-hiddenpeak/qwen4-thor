"""Local grouped build and CPU gate for native startup listener ownership."""
import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_operations_listener import cpu as common

BASE=ROOT/'.q4t-work/serve-startup-listeners-20261104'
ENGINE,HOST=BASE/'engine',BASE/'host'
read,save,sha,require,command=(getattr(common,n) for n in ('read','save','sha','require','command'))
TESTS=('tools.serve_service.test_declarations.Declarations',
       'tools.serve_service.test_service.PackageTests',
       'tools.verify.startup_delivery.test_deploy.DeployTest')


def runtime():
    old=read(read(ROOT/'build/q4t.release.json')['tested_source_manifest'])
    return {name:sha(ROOT/name) for name in sorted(set(old)|{'include/q4t/server/http_listener.h'})}


old_sources=common.sources

def sources():
    values=old_sources()
    for path in (ROOT/'tools/serve_startup_listeners').glob('*.py'):
        values[str(path.relative_to(ROOT))]=sha(path)
    values['tools/serve_reliability/contract_access.h']=sha(ROOT/'tools/serve_reliability/contract_access.h')
    return values


common.BASE,common.ENGINE,common.HOST=BASE,ENGINE,HOST
common.runtime=runtime
# common.build records runtime delta against a fixed previous manifest; a new
# header is separately included in its source identity and this explicit delta.
def build(label):
    original=common.read
    def read_with_added(path):
        value=original(path)
        old=original(ROOT/'build/q4t.release.json')['tested_source_manifest']
        if str(path)==old:value={**value,'include/q4t/server/http_listener.h':None}
        return value
    common.read=read_with_added
    common.sources=sources
    try:common.build(label)
    finally:common.read=original


def unit():
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in TESTS)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)


def test(label):
    out=BASE/label;out.mkdir(parents=True)
    save(out/'sources.json',sources())
    checks={}
    items=[('python',[sys.executable,'-B',__file__,'unit']),
        ('public-host',[HOST/'q4t_host_tests']),
        ('required-host',[sys.executable,'-B','tools/release/check_host_contracts.py',
         '--test-binary',ENGINE/'q4t_host_tests','--output',out/'host-gate']),
        ('structured',[ENGINE/'q4t_host_tests','structured_decoder_']),
        ('inventory',[sys.executable,'-B','tools/serve_audit/check_inventory.py','--self-check']),
        ('generated',[sys.executable,'-B','tools/serve_protocol/build_contract.py','--check'])]
    for name,argv in items:checks[name]=command(out,name,argv,240)
    passed=all(checks.values()) and sources()==read(out/'sources.json')
    save(out/'result.json',dict(passed=passed,checks=checks,models=0,http=0,gpu=0))
    require(passed,'CPU gate failed; retain original attempt')


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','test','unit'));p.add_argument('--label')
    args=p.parse_args()
    if args.action=='unit':unit()
    else:
        require(args.label and '/' not in args.label,'new label required')
        globals()[args.action](args.label)
