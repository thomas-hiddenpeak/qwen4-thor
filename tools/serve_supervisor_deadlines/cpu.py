"""Grouped pure CPU supervision/transport checks; no HTTP/model/GPU."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
BASE=ROOT/'.q4t-work/serve-supervisor-deadlines-20261104'
TESTS=('tools.serve_service.test_deadlines.Deadlines',
    'tools.serve_ops.test_transport_stop.StopTransport',
    'tools.serve_service.test_service',
    'tools.serve_ops.test_ops',
    'tools.serve_operations_listener.test_tools',
    'tools.serve_service.test_ports.Ports',
    'tools.serve_service.test_resources')


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def read(path):return json.loads(Path(path).read_bytes())


def save(path,value):
    with Path(path).open('x') as stream:json.dump(value,stream,indent=2,sort_keys=True);stream.write('\n')


def require(ok,message):
    if not ok:raise RuntimeError(message)


def sources():
    paths=[]
    for folder in ('tools/serve_service','tools/serve_ops','tools/serve_operations_listener',
                   'tools/serve_supervisor_deadlines','tools/serve_gateway','tools/release'):
        paths.extend((ROOT/folder).glob('*.py'))
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}


def run(label):
    out=BASE/label;out.mkdir(parents=True)
    frozen=sources();save(out/'sources.json',frozen)
    save(out/'default.json',{n:sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json')})
    suite=unittest.TestSuite();seen=set()
    def add(value):
        if isinstance(value,unittest.TestSuite):
            for child in value:add(child)
        elif value.id() not in seen:
            seen.add(value.id());suite.addTest(value)
    for name in TESTS:add(unittest.defaultTestLoader.loadTestsFromName(name))
    start=time.monotonic()
    with (out/'unittest.log').open('x') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    passed=result.wasSuccessful() and not result.skipped and sources()==frozen
    save(out/'result.json',dict(passed=passed,tests=result.testsRun,names=sorted(seen),
        failures=[case.id() for case,_ in result.failures],errors=[case.id() for case,_ in result.errors],
        skipped=result.skipped,seconds=time.monotonic()-start,models=0,http=0,gpu=0))
    require(passed,'CPU failure; retain original records')
    print(json.dumps(dict(passed=True,tests=result.testsRun)))


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--label',required=True);run(p.parse_args().label)
