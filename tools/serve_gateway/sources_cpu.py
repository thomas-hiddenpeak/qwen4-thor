"""Retained CPU/source-policy acceptance, with no HTTP listener or model."""
import argparse
import ast
import io
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import managed as m

BASE = ROOT/'.q4t-work/serve-gateway-sources-20261104'
TESTS = ('tools.serve_gateway.test_gateway', 'tools.serve_gateway.test_managed',
         'tools.serve_gateway.test_tls', 'tools.serve_clients.test_packages',
         'tools.serve_gateway.test_sources', 'tools.serve_gateway.test_sources_stage')


def sources():
    paths = set(m.V4_SOURCES)
    paths.update(str(p.relative_to(ROOT)) for p in
                 (ROOT/'tools/serve_gateway').glob('*.py'))
    paths.update(('tools/serve_clients/test_packages.py',
                  'docs/SERVE_GATEWAY_SOURCES_2026-11-04.md'))
    for module in tuple(sys.modules.values()):
        name = getattr(module, '__file__', None)
        if name and (p := Path(name).resolve()).is_relative_to(ROOT) and p.suffix == '.py':
            paths.add(str(p.relative_to(ROOT)))
    return sorted(paths)


def run(label, tests):
    os.umask(0o077)
    BASE.mkdir(exist_ok=True)
    output = BASE/label
    output.mkdir(mode=0o700)
    suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
    identities = {n: m.pkg.sha(ROOT/n) for n in sources()}
    for name in identities:
        raw = (ROOT/name).read_bytes()
        dest = output/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        if name.endswith('.py'):
            ast.parse(raw, filename=name)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output/'tests.log').write_text(stream.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), tests=tests,
        sources=identities, models_started=0, http_started=0, gpu_started=0)
    m.pkg.save(output/'result.json', value)
    print(stream.getvalue(), end='')
    return 0 if value['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='cpu-01')
    parser.add_argument('--test', action='append')
    args = parser.parse_args()
    if not args.label.startswith('cpu-') or '/' in args.label or '..' in args.label:
        parser.error('private CPU label required')
    raise SystemExit(run(args.label, args.test or list(TESTS)))
