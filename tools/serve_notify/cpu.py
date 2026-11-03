"""Record original CPU outcomes and complete source snapshots without HTTP."""
import argparse
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_notify import notify as n

BASE = ROOT/'.q4t-work/serve-notify-20261104'
SOURCES = (*n.SOURCES, 'tools/serve_notify/cpu.py',
           'tools/serve_notify/test_notify.py', 'docs/SERVE_NOTIFY_2026-11-04.md')


def run(output, names):
    output = n.pkg.private_directory(output)
    sources = {name:n.pkg.sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        path = output/'source'/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT/name).read_bytes())
    stream = io.StringIO()
    tests = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(tests)
    (output/'tests.log').write_text(stream.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), tests=names,
        sources=sources, model_gpu_http_started=False)
    n.pkg.save(output/'result.json', value)
    print(stream.getvalue(), flush=True)
    n.require(value['passed'], 'preserve CPU failures; correct affected cases')
    return {k:value[k] for k in ('passed', 'tests_run', 'failures', 'errors')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE/'cpu')
    parser.add_argument('tests', nargs='*', default=['tools.serve_notify.test_notify'])
    args = parser.parse_args(); print(json.dumps(run(args.output, args.tests)))
