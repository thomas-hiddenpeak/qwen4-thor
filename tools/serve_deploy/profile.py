"""Check an opt-in, immutable deployment selection without starting services."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as pkg
from tools.serve_gateway import managed as gateway

PROFILE = 'q4t-deployment-selection-v1'
SOURCES = sorted(set((*pkg.V5_SOURCES, *gateway.V4_SOURCES,
                      'tools/serve_deploy/profile.py')))
BOUNDARIES = dict(services_started=False, installation=False,
                  business_acceptance=False, actual_lan_acceptance=False,
                  physical_ram_hard_limit=False, long_run=False)


def reference(path, digest):
    pkg.require(type(digest) is str and re.fullmatch('[a-f0-9]{64}', digest),
                'explicit SHA256 required')
    path = Path(path).absolute()
    pkg.require(path == path.resolve(strict=True), 'canonical package required')
    return dict(package=str(path), manifest_sha256=digest)


def validate_reference(value):
    pkg.require(type(value) is dict and set(value) ==
                {'package', 'manifest_sha256'} and
                type(value['package']) is str, 'invalid package reference')
    pkg.require(value == reference(value['package'], value['manifest_sha256']),
                'package reference changed')


def combination(backend, frontend):
    """Cross-package checks complement each package's existing verifier."""
    cfg = backend['settings']
    pkg.require(cfg['profile']['host'] == '127.0.0.1', 'backend must be loopback')
    if frontend is None:
        return
    policy = frontend['policy']
    pkg.require(frontend['upstream_host'] == '127.0.0.1' and
                policy['upstream_port'] == cfg['port'], 'upstream port differs')
    pkg.require(policy.get('upstream_operations_port', 0) ==
                cfg.get('operations_port', 0), 'operations port differs')
    pkg.require(policy['listen_port'] not in
                (cfg['port'], cfg.get('operations_port', 0)), 'port collision')
    pkg.require(frontend.get('client_policy') == backend.get('client_policy'),
                'gateway/backend client policy differs')
    pkg.require(frontend.get('tls', {}).get('trust') == 'explicit_ca',
                'deployment selection requires explicit CA trust')


def selection(backend, frontend=None):
    validate_reference(backend)
    value = pkg.verify(backend['package'], backend['manifest_sha256'])
    front = None
    if frontend is not None:
        validate_reference(frontend)
        _, front = gateway.verify(frontend['package'], frontend['manifest_sha256'])
    combination(value, front)
    # Only fixed public fields are returned; never include credentials/config.
    summary = dict(binary_sha256=value['files']['q4t'],
                   protocol_sha256=value['protocol_sha256'],
                   settings=value['settings'],
                   host_resources=value.get('host_resources'),
                   client_policy_enabled='client_policy' in value,
                   gateway=None)
    if front is not None:
        policy = front['policy']
        summary['gateway'] = {key: policy.get(key) for key in
            ('listen_host', 'listen_port', 'server_name', 'allowed_client_cidrs',
             'client_ids', 'per_client_rps', 'burst')}
    return summary


def sources():
    return {name: pkg.sha(ROOT / name) for name in SOURCES}


def prepare(output, backend, frontend, rollback_backend, rollback_frontend):
    selected = selection(backend, frontend)
    rollback = selection(rollback_backend, rollback_frontend)
    target = pkg.private_directory(output)
    python = str(Path(sys.executable).resolve())
    value = dict(profile=PROFILE, selected=dict(backend=backend, gateway=frontend),
                 rollback=dict(backend=rollback_backend, gateway=rollback_frontend),
                 summaries=dict(selected=selected, rollback=rollback),
                 sources=sources(), python=dict(path=python, sha256=pkg.sha(python)),
                 boundaries=BOUNDARIES)
    pkg.save(target / 'profile.json', value)
    return dict(profile=str(target / 'profile.json'),
                sha256=pkg.sha(target / 'profile.json'), **BOUNDARIES)


def check(path, digest):
    path = Path(path)
    pkg.require(type(digest) is str and re.fullmatch('[a-f0-9]{64}', digest) and
                pkg.sha(path) == digest, 'deployment profile changed')
    value = pkg.read(path)
    pkg.require(set(value) == {'profile', 'selected', 'rollback', 'summaries',
                              'sources', 'python', 'boundaries'} and
                value['profile'] == PROFILE and
                pkg.same_json(value['boundaries'], BOUNDARIES), 'unknown profile')
    pkg.require(value['sources'] == sources(), 'use the recorded verifier revision')
    pkg.require(value['python'] == dict(path=str(Path(sys.executable).resolve()),
        sha256=pkg.sha(sys.executable)), 'verifier interpreter changed')
    actual = {}
    for name in ('selected', 'rollback'):
        row = value[name]
        pkg.require(type(row) is dict and set(row) == {'backend', 'gateway'},
                    'invalid deployment selection')
        actual[name] = selection(row['backend'], row['gateway'])
    pkg.require(pkg.same_json(actual, value['summaries']), 'profile summary changed')
    return dict(checked=True, summaries=actual, **BOUNDARIES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('prepare')
    create.add_argument('--output', required=True)
    for name in ('backend', 'gateway', 'rollback-backend', 'rollback-gateway'):
        create.add_argument('--' + name, required='backend' in name)
        create.add_argument('--' + name + '-sha256', required='backend' in name)
    verify = commands.add_parser('check')
    verify.add_argument('--profile', required=True)
    verify.add_argument('--sha256', required=True)
    args = parser.parse_args()
    if args.action == 'check':
        result = check(args.profile, args.sha256)
    else:
        refs = {}
        for name in ('backend', 'gateway', 'rollback_backend', 'rollback_gateway'):
            path, digest = getattr(args, name), getattr(args, name + '_sha256')
            pkg.require((path is None) == (digest is None), 'package/SHA pair required')
            refs[name] = reference(path, digest) if path is not None else None
        result = prepare(args.output, refs['backend'], refs['gateway'],
                         refs['rollback_backend'], refs['rollback_gateway'])
    print(json.dumps(result))


if __name__ == '__main__':
    os.umask(0o077)
    main()
