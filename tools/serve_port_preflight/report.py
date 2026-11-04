"""Offline wire, known-port rejection and host-cleanup reconciliation."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_port_preflight import stage as s
from tools.serve_managed_profile import report as host
from tools.serve_operations_managed import report as prior

pkg = s.pkg
ARCHIVE = ROOT / 'docs/evidence/serve-port-preflight-20261104'
BASE_AUDIT, BASE_EXPORT = prior.audit, prior.exported


def audit():
    result = BASE_AUDIT()
    plan = s.check()
    cpu = pkg.read(s.BASE / 'cpu-01/result.json')
    pkg.require(cpu['passed'] and cpu['tests_run'] == len(set(cpu['names'])) > 0 and
        cpu['failures'] == cpu['errors'] == cpu['skipped'] == 0, 'CPU outcome')
    repro = pkg.read(s.BASE / 'reproduction-02.json')
    pkg.require(repro['expected_defect_reproduced'] and repro['failures'] == 1 and
        repro['errors'] == 0 and repro['supervisor_sha256'] == pkg.sha(s.BASE / 'supervisor-before.py') and
        'port conflict reached backend launch' in (s.BASE / 'reproduction-02.log').read_text(),
        'original defect reproduction')
    first = pkg.read(s.OUT / 'first-ready.json')
    checks = []
    for role, key in (('public', 'package'), ('operations', 'negative_package')):
        folder = s.OUT / ('port-check-' + role)
        state, probe, receipt, final = (pkg.read(folder / name) for name in
            ('status.json', 'probe-result.json', 'verified.json', 'resources-final.json'))
        selected = plan[key]
        manifest = pkg.read(Path(selected['package']) / 'manifest.json')
        pkg.require(probe['returncode'] == 1 and
            probe['manifest_sha256'] == selected['manifest_sha256'] and
            probe['supervisor_sha256'] == manifest['files']['source/tools/serve_service/supervisor.py'] and
            probe['owner'] == state['owner'] and state['state'] == 'FAILED' and
            state['attempts'] == state['restarts'] == 0 and state['backend'] is None and
            state['cleanup_confirmed'] and role + ' listener unavailable' in state['error'] and
            not list(folder.glob('attempt-*.json')) and not (folder / 'backend.log').exists(),
            'package pre-exec rejection')
        pkg.require(receipt['passed'] and receipt['seconds'] <= 15 and
            receipt['cleanup']['scope_removed'] and receipt['occupied_by'] == first['backend'] and
            receipt['public_listener'] == first['observation']['process'] and
            receipt['operations_listener'] == first['observation']['operations_process'] and
            not s.base.gateway.alive(probe['owner']) and final['payload_removed'] and
            not final['final']['members'] and final['final']['cgroup.events']['populated'] == 0 and
            final['violation'] is None and not Path(final['record']['root']).exists(),
            'probe isolation and cleanup')
        checks.append(dict(role=role, seconds=receipt['seconds'], error=state['error'],
            attempts=0, manifest_sha256=selected['manifest_sha256'], owner=probe['owner'],
            scope_removed=True, original_listener_unchanged=True))
    binding = pkg.read(s.OUT / 'cgroup-binding.json')
    raw = (s.OUT / 'host-samples.jsonl').read_bytes()
    captured = pkg.read(s.OUT / 'host-capture.json')
    samples = [json.loads(line) for line in raw.splitlines()]
    pkg.require(captured['passed'] and captured['samples'] == len(samples) and
                captured['bytes'] == len(raw), 'host capture')
    observed = host.audit_samples(samples, binding)
    final = pkg.read(s.OUT / 'managed-run/resources-final.json')
    terminal = pkg.read(s.OUT / 'managed-run/terminal.json')
    pkg.require(final['record'] == binding['record'] and
        final['record']['owner'] == first['owner'] and
        final['record']['policy'] == plan['host_resources'] and final['payload_removed'] and
        final['violation'] is None and not final['final']['members'] and
        final['final']['cgroup.events']['populated'] == 0 and
        terminal['host_resource_cleanup']['scope_removed'], 'host lifecycle')
    live = s.OUT / 'business/saturated-capabilities-wire.bin'
    declaration = Path(plan['package']['package']) / 'declarations/capabilities.json'
    pkg.require(pkg.same_json(json.loads(live.read_bytes()), pkg.read(declaration)) and
                pkg.read(s.OUT / 'live-declaration.json')['passed'], 'live declarations')
    pkg.require(result['monitored_shared_lock_seconds'] <= s.base.GPU_SECONDS and
                result['driver_seconds'] <= s.base.LIMITS['seconds'], 'runtime bounds')
    result.update(status='ACCEPT_MANAGED_PORT_PREFLIGHT', cpu_checks=cpu['tests_run'],
        known_conflicts=checks, host_envelope_enabled_in_package=True,
        release_declarations_verified_live=True, host_capture=observed,
        host_policy=plan['host_resources'], host_memory_peak_bytes=final['final']['memory.peak'],
        host_memory_events=final['final']['memory.events'], host_pids_events=final['final']['pids.events'],
        source_change='only tools/serve_service/supervisor.py in runtime packages',
        original_reproduction_is_fixture_failure=True,
        valid_reproduction='reproduction-02.json', future_port_race_eliminated=False)
    result.pop('protocol_migration_registered')
    return result


def exported(path):
    relative = path.relative_to(s.BASE)
    if relative.parts[0] == 'reviewed-source' or path.name == 'supervisor-before.py':
        return True
    if path.suffix == '.txt' and path.parent.name == 'postflight':
        return True
    return BASE_EXPORT(path)


def originals():
    # Collector/verifier output is not business evidence and can still be open.
    excluded = {'archive.stdout', 'archive.stderr', 'verify.stdout', 'verify.stderr'}
    return sorted(p for p in s.BASE.rglob('*') if p.is_file() and p.name not in excluded)


def configure():
    s.configure()
    prior.ARCHIVE, prior.audit, prior.exported = ARCHIVE, audit, exported
    prior.original_files = originals


def archive():
    names = [*(str(p.relative_to(ROOT)) for p in (ROOT / s.HERE).glob('*.py')),
        'tools/serve_service/supervisor.py', 'tools/serve_service/test_ports.py',
        'tools/serve_operations_managed/stage.py', 'tools/serve_operations_managed/report.py',
        'tools/serve_managed_profile/stage.py', 'tools/serve_managed_profile/report.py',
        'docs/SERVE_PORT_PREFLIGHT_2026-11-04.md']
    for name in names:
        path = s.BASE / 'reviewed-source' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write((ROOT / name).read_bytes())
    prior.archive()


if __name__ == '__main__':
    os.umask(0o077)
    configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('postflight', 'archive', 'verify'))
    action = parser.parse_args().action
    if action == 'postflight':
        print(json.dumps(host.postflight()))
    elif action == 'verify':
        prior.verify()
    else:
        archive()
