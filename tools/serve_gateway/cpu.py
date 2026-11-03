"""One preserved CPU acceptance group; no listener, model or GPU is started."""
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import gateway as g

BASE=ROOT/'.q4t-work/serve-gateway-20261103'
SOURCES=('tools/serve_gateway/gateway.py','tools/serve_gateway/test_gateway.py',
         'tools/serve_gateway/cpu.py','docs/SERVE_GATEWAY_2026-11-03.md')


def run():
    output=BASE/'cpu'
    output.mkdir(mode=0o700)
    sources={name:g.sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        dest=output/'source'/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        g.save(dest,(ROOT/name).read_bytes())
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.loadTestsFromName('tools.serve_gateway.test_gateway')
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    g.save(output/'tests.log',stream.getvalue().encode())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),sources=sources,
        vendor_receipt_sha256=g.sha(BASE/'vendor/receipt.json'),
        listener_or_model_or_gpu_started=False)
    g.save(output/'result.json',(json.dumps(value,indent=2)+'\n').encode())
    print(stream.getvalue(),end='')
    g.require(value['passed'],'CPU failed; preserve originals and fix affected cases')
    return value


if __name__=='__main__':
    value=run()
    print(json.dumps({k:v for k,v in value.items() if k!='sources'}))
