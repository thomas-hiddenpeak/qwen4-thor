"""Grouped pure-CPU client, pinned SDK and task contracts; no HTTP/GPU."""
import argparse
import ast
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/serve-client-transport-20261105'
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/examples'), str(ROOT/'tools/serve_protocol')]
from tools.serve_service import package as pkg


def sources():
    names = set()
    for directory in ('tools/examples', 'tools/serve_protocol',
                      'tools/serve_client_transport', 'tools/project_tasks'):
        names.update(str(p.relative_to(ROOT)) for p in (ROOT/directory).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.mjs', '.json'))
    return {name: pkg.sha(ROOT/name) for name in sorted(names)}


def source_review():
    old = subprocess.check_output(['git', 'show',
        '07125dde904d2b3bb0fe69ee152c95f2c62451cd:tools/examples/chat_client.py'], cwd=ROOT).decode()
    def functions(raw):
        return {node.name: ast.dump(node, include_attributes=False)
                for node in ast.parse(raw).body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name != 'ChatClient'}
    before = functions(old)
    pkg.require(before == functions((ROOT/'tools/examples/chat_client.py').read_text()),
                'shared response/parser/tool semantics changed')
    from tools.project_tasks import engine
    pkg.require('tools/examples/deadline_http.py' in engine.RUNTIME_FILES and
                len(engine.RUNTIME_FILES) == 18, 'application source closure')
    release = pkg.read(ROOT/'build/q4t.release.json')
    runtime = pkg.read(release['tested_source_manifest'])
    pkg.require(all(pkg.sha(ROOT/name) == digest for name, digest in runtime.items()),
                'native source changed')
    return dict(shared_definitions_unchanged=len(before), application_sources=len(engine.RUNTIME_FILES),
        runtime_sources_unchanged=len(runtime), default_pair={name: pkg.sha(ROOT/'build'/name)
            for name in ('q4t', 'q4t.release.json')}, new_runtime_migration_registered=False)


def run(label):
    os.umask(0o077)
    out = BASE/label
    out.mkdir()
    snapshot = sources()
    pkg.save(out/'sources.json', snapshot)
    for name in snapshot:
        target = out/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    pkg.save(out/'source-review.json', source_review())
    modules = [importlib.import_module(name) for name in
               ('test_chat_client', 'test_bridge', 'tools.project_tasks.test_tasks',
                'tools.serve_client_transport.test_stage')]
    modules[1].BASE = out
    modules[2].BASE = out
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in modules)
    def ids(item):
        return [name for child in item for name in ids(child)] if isinstance(item, unittest.TestSuite) else [item.id()]
    names = ids(suite)
    pkg.require(len(names) == len(set(names)), 'duplicate test identity')
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (out/'python.log').write_text(log.getvalue())
    from tools.serve_gateway.test_gateway import certificate
    private = out/'private'
    private.mkdir(mode=0o700)
    cert, _ = certificate(private)
    env = dict(os.environ, Q4T_NODE_SDK_DIR=str(ROOT/'.q4t-work/serve-protocol-20261103/sdk'),
               Q4T_CLIENT_TEST_DIR=str(out), Q4T_CLIENT_TEST_CA=str(cert))
    for key in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD'):
        env.pop(key, None)
    pkg.require(subprocess.check_output(['node', '--version'], text=True).strip() == 'v24.18.0',
                'pinned Node version')
    js = subprocess.run(['node', '--test', '--test-reporter=tap',
        'tools/examples/js/test_chat_client.mjs', 'tools/examples/js/test_node_transport.mjs'],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    (out/'node.log').write_text(js.stdout)
    (out/'node.stderr').write_text(js.stderr)
    passed = result.wasSuccessful() and not result.skipped and js.returncode == 0 and sources() == snapshot
    record = dict(passed=passed, tests_run=result.testsRun, names=names,
                  failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                  node_returncode=js.returncode, models=0, http=0, gpu=0)
    pkg.save(out/'result.json', record)
    print(json.dumps({k:v for k,v in record.items() if k != 'names'}))
    pkg.require(passed, 'grouped CPU gate failed; preserve attempt')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    pkg.require(args.label.startswith('cpu-') and '/' not in args.label, 'new CPU label')
    run(args.label)
