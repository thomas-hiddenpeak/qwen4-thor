"""Opt-in v6 declarations + operations + host envelope, one bounded lease.

Reuse the unchanged SRV-12h lifecycle and wire checks. No model runtime changes,
new installed units, retries, long runs, or implicit resource defaults.
"""
import argparse
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    sys.modules['tools.serve_managed_profile.stage'] = sys.modules[__name__]
from tools.serve_operations_managed import stage as base
from tools.serve_service import package as pkg, resources, supervisor as sup

BASE = ROOT / '.q4t-work/serve-managed-profile-20261104'
PLAN, OUT = BASE / 'plan.json', BASE / 'execution'
OLD = ROOT / '.q4t-work/serve-operations-managed-20261104/plan.json'
HERE = 'tools/serve_managed_profile/'
POLICY = resources.policy(16384, 20480, 512)
ORIGINAL_CHECK = base.check


def configure():
    base.BASE, base.PLAN, base.OUT, base.HERE = BASE, PLAN, OUT, HERE
    base.check = check


def sources():
    values = base.sources()
    values['docs/SERVE_MANAGED_PROFILE_2026-11-04.md'] = pkg.sha(
        ROOT / 'docs/SERVE_MANAGED_PROFILE_2026-11-04.md')
    return values


def cpu():
    configure()
    BASE.mkdir(exist_ok=True)
    target = BASE / 'cpu-01'
    target.mkdir(mode=0o700)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_managed_profile.test_profile')
    log = io.StringIO()
    outcome = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (target / 'tests.log').write_text(log.getvalue())
    result = dict(passed=outcome.wasSuccessful() and not outcome.skipped,
        tests_run=outcome.testsRun, errors=len(outcome.errors),
        failures=len(outcome.failures), skipped=len(outcome.skipped),
        sources=sources(), models_started=0, listeners_started=0)
    pkg.save(target / 'result.json', result)
    pkg.require(result['passed'], log.getvalue())
    return {k: v for k, v in result.items() if k != 'sources'}


def repaired_inputs():
    """Nine unchanged checks remain valid; pin the one targeted repair check."""
    tested = pkg.read(BASE / 'cpu-01/result.json')
    pkg.require(tested['passed'], 'original CPU gate')
    changed = {HERE + name for name in ('stage.py', 'report.py', 'test_profile.py')}
    for name, digest in tested['sources'].items():
        source = (BASE / 'prepare-failure-source' / Path(name).name
                  if name in changed else ROOT / name)
        pkg.require(pkg.sha(source) == digest, 'CPU source changed: ' + name)
    before = ast.parse((BASE / 'prepare-failure-source/test_profile.py').read_text())
    after = ast.parse((ROOT / HERE / 'test_profile.py').read_text())
    for node in before.body:
        if isinstance(node, ast.ClassDef):
            current = next(n for n in after.body if isinstance(n, ast.ClassDef) and n.name == node.name)
            pkg.require(ast.dump(node) == ast.dump(current), 'accepted CPU test changed')
    # Only prepare/support methods changed in stage; runtime capture, Run,
    # cleanup and original CPU implementation must retain their tested bodies.
    before = ast.parse((BASE / 'prepare-failure-source/stage.py').read_text())
    after = ast.parse((ROOT / HERE / 'stage.py').read_text())
    for name in ('Capture', 'Run', 'cpu', 'check'):
        old_node = next(n for n in before.body if getattr(n, 'name', None) == name)
        new_node = next(n for n in after.body if getattr(n, 'name', None) == name)
        pkg.require(ast.dump(old_node) == ast.dump(new_node), 'runtime driver changed: ' + name)
    return tested


def gateway_inputs(old):
    outer = old['gateways'][0]
    managed, inner = base.gateway.verify(outer['package'], outer['manifest_sha256'])
    pkg.require(managed['gateway'] == old['gateway'], 'old gateway binding')
    return Path(inner['nginx']['path']), Path(managed['gateway']['package'])


def cpu_repair():
    configure()
    repaired_inputs()
    target = BASE / 'cpu-repair'
    target.mkdir(mode=0o700)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_managed_profile.test_profile.PrepareRepair')
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (target / 'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful() and not result.skipped,
        tests_run=result.testsRun, errors=len(result.errors), failures=len(result.failures),
        sources=sources(), reused_checks=9, models_started=0, listeners_started=0)
    pkg.save(target / 'result.json', value)
    pkg.require(value['passed'], log.getvalue())
    return {k: v for k, v in value.items() if k != 'sources'}


def prepare():
    configure()
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'],
                                           cwd=ROOT), 'commit reviewed sources')
    repaired_inputs()
    repair = pkg.read(BASE / 'cpu-repair/result.json')
    pkg.require(repair['passed'] and repair['sources'] == sources(), 'repair CPU gate')
    pkg.require('FileNotFoundError' in (BASE / 'prepare.stderr').read_text() and
        'gateway/nginx' in (BASE / 'prepare.stderr').read_text(), 'preserved preparation failure')
    old = pkg.read(OLD)
    pkg.verify(old['package']['package'], old['package']['manifest_sha256'])
    target = BASE / 'prepare-02'
    target.mkdir(mode=0o700)
    private = target / 'private'
    private.mkdir(mode=0o700)
    front = pkg.read(old['frontend'])
    pkg.save(private / 'gateway.json', front)
    path = BASE / 'backend-package'
    digest = pkg.sha(path / 'manifest.json')
    manifest = pkg.verify(path, digest)
    pkg.require(manifest['files']['q4t'] == pkg.sha(ROOT / 'build/q4t') and
        manifest['files']['q4t.release.json'] == pkg.sha(ROOT / 'build/q4t.release.json') and
        manifest['host_resources'] == POLICY and manifest['version'] == 6,
        'already prepared backend changed')
    backend = dict(package=str(path), manifest_sha256=digest,
                   binary_sha256=manifest['files']['q4t'])
    gateways, rejected = [], []
    nginx, old_tls = gateway_inputs(old)
    for label, value in (
            ('good-1', front), ('good-2', front),
            ('missing-operations', {k: v for k, v in front.items()
                                    if k != 'upstream_operations_port'}),
            ('wrong-operations', dict(front, upstream_operations_port=18134))):
        path = private / (label + '.json')
        pkg.save(path, value)
        selected = base.gateway.prepare(path, nginx,
            old_tls / 'certificate.pem', old_tls / 'private-key.pem',
            target / ('gateway-' + label), old['policy'],
            trusted_ca=old_tls / 'certificate.pem')
        (gateways if label.startswith('good') else rejected).append(selected)
    manifest, _ = base.gateway.verify(gateways[0]['package'],
                                      gateways[0]['manifest_sha256'])
    preserved = dict(old['preserved'])
    for path in (ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', OLD,
                 Path(old['frontend']), Path(old['policy']), *base.DAYS):
        preserved[str(path)] = pkg.sha(path)
    # Earlier prepare preserved the then-current sidecar. The current pair is
    # checked above; the original accepted package is separately kept immutable.
    release = pkg.read(ROOT / 'build/q4t.release.json')
    plan = dict(old, package=backend, gateways=gateways,
        rejected_gateways=rejected, gateway=manifest['gateway'],
        frontend=str(private / 'gateway.json'), preserved=preserved,
        sources=sources(), runtime=pkg.read(release['tested_source_manifest']),
        cpu_sha256=pkg.sha(BASE / 'cpu-01/result.json'), host_resources=POLICY,
        repair_cpu_sha256=pkg.sha(BASE / 'cpu-repair/result.json'),
        reused_package=old['package'], source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    pkg.save(PLAN, plan)
    check()
    return dict(prepared=True, models_started=0, listeners_started=0,
                package_manifest=backend['manifest_sha256'])


def check():
    plan = ORIGINAL_CHECK()
    manifest = pkg.verify(plan['package']['package'],
                          plan['package']['manifest_sha256'])
    pkg.require(manifest['version'] == 6 and
        manifest['host_resources'] == plan['host_resources'] == POLICY and
        manifest['release_declarations'] == pkg.DECLARATION_PROFILE and
        manifest['settings']['operations_port'] == base.OPS_PORT and
        manifest['settings']['restart_limit'] == 0 and
        'client_policy' in manifest, 'combined package profile')
    pkg.verify(plan['reused_package']['package'],
               plan['reused_package']['manifest_sha256'])
    certificate = Path(plan['gateway']['package']) / 'certificate.pem'
    pkg.require(subprocess.run(['/usr/bin/openssl', 'x509', '-checkend', '3840',
        '-noout', '-in', str(certificate)], capture_output=True,
        timeout=10).returncode == 0, 'certificate expiry bound')
    return plan


class Capture:
    """Only observe this run's verified delegated group; never change limits."""
    def __init__(self, record):
        self.record = record
        self.stop = threading.Event()
        self.error = None
        self.count = self.bytes = 0
        self.stream = (OUT / 'host-samples.jsonl').open('x', buffering=1)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            while not self.stop.is_set():
                value = resources.sample(self.record)
                members = []
                for pid in value['members']:
                    try:
                        owner = sup.process_identity(pid)
                        argv = Path(f'/proc/{pid}/cmdline').read_bytes()
                        pkg.require(len(argv) <= 16384, 'process argv bound')
                        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                        membership = resources.membership(pid)
                        if owner == sup.process_identity(pid):
                            members.append(dict(owner=owner, parent=int(fields[1]),
                                membership=membership, argv=argv.decode().split('\0')[:-1]))
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                value['processes'] = members
                raw = json.dumps(value) + '\n'
                self.bytes += len(raw.encode())
                self.count += 1
                pkg.require(self.count <= 1024 and self.bytes <= 2*1024**2,
                            'host evidence bound')
                self.stream.write(raw)
                pkg.require(resources.violation(value) is None, 'resource violation')
                self.stop.wait(.25)
        except Exception as error:
            self.error = repr(error)

    def check(self):
        pkg.require(self.error is None, 'host observer failed: ' + str(self.error))

    def close(self):
        self.stop.set()
        self.thread.join(10)
        pkg.require(not self.thread.is_alive(), 'host observer did not exit')
        self.stream.close()
        self.check()
        pkg.save(OUT / 'host-capture.json', dict(passed=True,
                 samples=self.count, bytes=self.bytes))


class Run(base.Run):
    def __init__(self, plan):
        self.capture = None
        super().__init__(plan)

    def budget(self, kind):
        if self.capture is not None:
            self.capture.check()
        super().budget(kind)

    def launch(self):
        super().launch()
        state = pkg.read(OUT / 'first-ready.json')
        control = pkg.read(self.run_dir / 'controller.json')
        record = pkg.read(self.run_dir / 'resources.json')
        root = resources.verify_record(record)
        relative = '/' + str(root.relative_to(resources.CGROUP))
        owners = dict(controller=control['owner'], worker=state['owner'],
                      backend=state['backend'])
        memberships = {k: resources.membership(v['pid']) for k, v in owners.items()}
        pkg.require(record['owner'] == owners['worker'] and
            memberships['worker'] == relative + '/control' and
            memberships['backend'] == relative + '/payload' and
            not memberships['controller'].startswith(relative), 'control/payload isolation')
        pkg.save(OUT / 'cgroup-binding.json', dict(record=record,
                 owners=owners, memberships=memberships))
        self.capture = Capture(record)
        self.capture.thread.start()

    def pressure(self):
        super().pressure()
        observed = json.loads((base.client.OUT /
                               'saturated-capabilities-wire.bin').read_bytes())
        declared = pkg.read(self.package / 'declarations/capabilities.json')
        pkg.require(pkg.same_json(observed, declared), 'live capability declaration differs')
        pkg.save(OUT / 'live-declaration.json', dict(passed=True,
            observed_sha256=pkg.sha(base.client.OUT / 'saturated-capabilities-wire.bin'),
            declared_sha256=pkg.sha(self.package / 'declarations/capabilities.json')))

    def cleanup(self):
        failure = None
        if self.capture is not None:
            try:
                self.capture.close()
            except BaseException as error:
                failure = error
            finally:
                self.capture = None
        # An observation failure must never prevent owned backend/gateway cleanup.
        try:
            super().cleanup()
        finally:
            if failure is not None:
                raise failure


if __name__ == '__main__':
    os.umask(0o077)
    configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'cpu_repair', 'prepare', 'check', 'run'))
    action = parser.parse_args().action
    if action == 'run':
        plan = check()
        OUT.mkdir(mode=0o700)
        with sup.processes.child_subreaper():
            result = Run(plan).execute()
    elif action == 'check':
        check()
        result = dict(checked=True)
    else:
        result = globals()[action]()
    print(json.dumps(result))
