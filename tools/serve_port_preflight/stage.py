"""One-lease known-port refusal and accepted managed business regression."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    sys.modules['tools.serve_port_preflight.stage'] = sys.modules[__name__]
from tools.serve_managed_profile import stage as profile
from tools.serve_operations_managed import stage as base
from tools.serve_service import package as pkg, resources, supervisor as sup

BASE = ROOT / '.q4t-work/serve-port-preflight-20261104'
PLAN, OUT = BASE / 'plan.json', BASE / 'execution'
OLD = ROOT / '.q4t-work/serve-managed-profile-20261104/plan.json'
HERE = 'tools/serve_port_preflight/'
ORIGINAL_CHECK = base.check
TESTS = ('tools.serve_service.test_ports.Ports',
         'tools.serve_service.test_service.Case',
         'tools.serve_managed_profile.test_profile.CombinedSupervisor',
         'tools.serve_port_preflight.test_stage.Driver')


def configure():
    base.BASE, base.PLAN, base.OUT, base.HERE = BASE, PLAN, OUT, HERE
    profile.BASE, profile.PLAN, profile.OUT = BASE, PLAN, OUT
    base.check = profile.check = check


def sources():
    values = base.sources()
    for name in ('tools/serve_service/test_ports.py',
                 'docs/SERVE_PORT_PREFLIGHT_2026-11-04.md'):
        values[name] = pkg.sha(ROOT / name)
    return values


def cpu():
    target = BASE / 'cpu-01'
    target.mkdir(mode=0o700)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in TESTS)
    def ids(test):
        return ([name for child in test for name in ids(child)]
                if isinstance(test, unittest.TestSuite) else [test.id()])
    names = ids(suite)
    pkg.require(len(names) == len(set(names)), 'duplicate CPU cases')
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (target / 'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful() and not result.skipped,
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped), names=names, sources=sources(), models_started=0, http_requests=0)
    pkg.save(target / 'result.json', value)
    pkg.require(value['passed'], log.getvalue())
    return {k: v for k, v in value.items() if k != 'sources'}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed sources')
    cpu = pkg.read(BASE / 'cpu-01/result.json')
    pkg.require(cpu['passed'] and cpu['sources'] == sources(), 'CPU gate')
    old = pkg.read(OLD)
    prior = pkg.verify(old['package']['package'], old['package']['manifest_sha256'])
    private = BASE / 'private'
    private.mkdir(mode=0o700)
    front = pkg.read(old['frontend'])
    pkg.save(private / 'gateway.json', front)
    def backend(name, port):
        return pkg.prepare(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
            old['model_dir'], BASE / name, port=port, operations_port=base.OPS_PORT,
            client_policy=old['policy'], host_resources=profile.POLICY, restart_limit=0)
    package = backend('backend-package', base.PORT)
    negative = backend('operations-conflict-package', 18134)
    current = pkg.verify(package['package'], package['manifest_sha256'])
    changed = {name for name in current['files'] if current['files'][name] != prior['files'][name]}
    pkg.require(changed == {'source/tools/serve_service/supervisor.py'}, 'unexpected package delta')
    old_front, inner = base.gateway.verify(old['gateways'][0]['package'],
                                           old['gateways'][0]['manifest_sha256'])
    tls = Path(old_front['gateway']['package'])
    gateways, rejected = [], []
    for label, value in (('good-1', front), ('good-2', front),
            ('missing-operations', {k: v for k, v in front.items() if k != 'upstream_operations_port'}),
            ('wrong-operations', dict(front, upstream_operations_port=18134))):
        path = private / (label + '.json')
        pkg.save(path, value)
        item = base.gateway.prepare(path, inner['nginx']['path'], tls / 'certificate.pem',
            tls / 'private-key.pem', BASE / ('gateway-' + label), old['policy'],
            trusted_ca=tls / 'certificate.pem')
        (gateways if label.startswith('good') else rejected).append(item)
    outer, _ = base.gateway.verify(gateways[0]['package'], gateways[0]['manifest_sha256'])
    preserved = dict(old['preserved'])
    preserved[str(OLD)] = pkg.sha(OLD)
    plan = dict(old, package=package, gateways=gateways, rejected_gateways=rejected,
        gateway=outer['gateway'], frontend=str(private / 'gateway.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        sources=sources(), preserved=preserved, reused_package=old['package'],
        cpu_sha256=pkg.sha(BASE / 'cpu-01/result.json'), negative_package=negative,
        port_checks=2, port_check_seconds=15)
    plan.pop('repair_cpu_sha256', None)
    pkg.save(PLAN, plan)
    check()
    return dict(prepared=True, models_started=0, package=package)


def check():
    plan = ORIGINAL_CHECK()
    for key in ('package', 'negative_package', 'reused_package'):
        pkg.verify(plan[key]['package'], plan[key]['manifest_sha256'])
    current = pkg.read(Path(plan['package']['package']) / 'manifest.json')
    old = pkg.read(Path(plan['reused_package']['package']) / 'manifest.json')
    pkg.require({n for n in current['files'] if current['files'][n] != old['files'][n]} ==
        {'source/tools/serve_service/supervisor.py'}, 'only supervisor may change')
    pkg.require(current['host_resources'] == profile.POLICY and
        current['version'] == 6 and current['settings']['operations_port'] == base.OPS_PORT and
        plan['port_checks'] == 2 and plan['port_check_seconds'] == 15, 'frozen port profile')
    return plan


class Run(profile.Run):
    def stop_probes(self):
        for name, process in self.processes.items():
            if name.startswith('port-check-') and process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)

    def cleanup(self):
        try:
            self.stop_probes()
        finally:
            super().cleanup()

    def launch(self):
        super().launch()
        primary = base.sup.identity.process(self.model_owner['pid'], base.PORT)
        operations = base.sup.identity.operations(primary, base.OPS_PORT)
        for role, key in (('public', 'package'), ('operations', 'negative_package')):
            selected = self.plan[key]
            directory = OUT / ('port-check-' + role)
            started = time.monotonic()
            process = self.spawn('port-check-' + role, [sys.executable, '-I', '-B',
                ROOT / HERE / 'probe.py', '--package', selected['package'],
                '--digest', selected['manifest_sha256'], '--output', directory,
                '--parent', os.getpid()])
            code = process.wait(self.plan['port_check_seconds'])
            state = pkg.read(directory / 'status.json')
            pkg.require(code == 1 and state['state'] == 'FAILED' and
                state['attempts'] == state['restarts'] == 0 and state['backend'] is None and
                state['cleanup_confirmed'] and role + ' listener unavailable' in state['error'] and
                not list(directory.glob('attempt-*.json')) and
                not (directory / 'backend.log').exists(), 'known port conflict reached backend')
            cleanup = resources.departed(directory)
            pkg.require(cleanup['scope_removed'] and
                base.sup.identity.process(self.model_owner['pid'], base.PORT) == primary and
                base.sup.identity.operations(primary, base.OPS_PORT) == operations,
                'conflict probe affected original owner')
            pkg.save(directory / 'verified.json', dict(passed=True, role=role,
                seconds=time.monotonic()-started, cleanup=cleanup,
                occupied_by=self.model_owner, public_listener=primary,
                operations_listener=operations))
            self.custody.check()


if __name__ == '__main__':
    os.umask(0o077)
    configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'check', 'run'))
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
