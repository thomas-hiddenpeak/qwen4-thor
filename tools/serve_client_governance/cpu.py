"""Bounded governance build/CPU gate; no model, GPU or listening socket."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_operations_listener import cpu as shared
from tools.serve_operations_listener.cpu import sha, read, save, require, command, runtime

BASE = ROOT/'.q4t-work/serve-client-governance-20261105'
ENGINE, HOST = BASE/'engine', BASE/'host'


def sources():
    result = shared.sources()
    for folder in ('tools/serve_client_governance', 'tools/serve_clients'):
        result.update({str(p.relative_to(ROOT)): sha(p)
                       for p in (ROOT/folder).glob('*.py')})
    result['docs/serve-preflight-v1.schema.json'] = sha(ROOT/'docs/serve-preflight-v1.schema.json')
    return result


def build(label):
    shared.BASE, shared.ENGINE, shared.HOST = BASE, ENGINE, HOST
    shared.build(label)


def test(label):
    out = BASE/label
    out.mkdir()
    save(out/'sources.json', sources())
    checks = {}
    for name, argv in (
        ('python', [sys.executable, '-B', '-m', 'unittest', '-v',
                    'tools.serve_clients.test_packages.Clients',
                    'tools.serve_gateway.test_gateway.GatewayTests',
                    'tools.serve_client_governance.test_driver']),
        ('public-host', [HOST/'q4t_host_tests']),
        ('required-host', [sys.executable, '-B', 'tools/release/check_host_contracts.py',
                           '--test-binary', ENGINE/'q4t_host_tests', '--output', out/'host-gate']),
        ('structured', [ENGINE/'q4t_host_tests', 'structured_decoder_']),
        ('inventory', [sys.executable, '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', [sys.executable, '-B', 'tools/serve_protocol/build_contract.py', '--check'])):
        checks[name] = command(out, name, argv, 240)
    passed = all(checks.values()) and sources() == read(out/'sources.json')
    save(out/'result.json', dict(passed=passed, checks=checks, models=0, gpu=0))
    require(passed, 'CPU failure; preserve original attempt')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'test'))
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    require('/' not in args.label and args.label not in ('', '.', '..'), 'new label')
    globals()[args.action](args.label)
