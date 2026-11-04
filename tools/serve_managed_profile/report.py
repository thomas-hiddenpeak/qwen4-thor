"""Add host/declaration reconciliation to the unchanged SRV-12h wire audit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_managed_profile import stage as s
from tools.serve_operations_managed import report as prior

pkg, resources = s.pkg, s.resources
ARCHIVE = ROOT / 'docs/evidence/serve-managed-profile-20261104'
BASE_AUDIT, BASE_EXPORT = prior.audit, prior.exported


def audit_samples(samples, binding):
    pkg.require(bool(samples), 'host samples missing')
    policy = binding['record']['policy']
    expected = resources.limits(policy)
    relative = '/' + str(Path(binding['record']['root']).relative_to(resources.CGROUP))
    model_pid = binding['owners']['backend']['pid']
    processes = {}
    for row in samples:
        pkg.require(row['effective_limits'] == expected and
            resources.violation(row) is None and
            row['memory.current'] <= policy['memory_max_bytes'] and
            row['pids.current'] <= policy['tasks_max'] and
            row['memory.swap.current'] == 0, 'live host limits')
        for member in row['processes']:
            pkg.require(member['membership'] == relative + '/payload', 'member escaped payload')
            pid = member['owner']['pid']
            if pid != model_pid:
                pkg.require(member['parent'] == model_pid and
                    member['argv'][0] == 'q4t-grammar-worker' and
                    str(model_pid) in member['argv'], 'unidentified payload descendant')
            old = processes.setdefault(pid, member)
            pkg.require(old['owner'] == member['owner'], 'member identity changed')
    pkg.require(model_pid in processes and len(processes) == 5,
                'model and four actual grammar workers must be observed')
    return dict(samples=len(samples), processes=list(processes.values()),
        sampled_memory_peak_bytes=max(v['memory.peak'] for v in samples),
        sampled_tasks_peak=max(v['pids.current'] for v in samples))


def audit():
    result = BASE_AUDIT()
    plan = s.check()
    cpu = pkg.read(s.BASE / 'cpu-01/result.json')
    pkg.require(cpu['passed'] and cpu['tests_run'] == 9 and
        cpu['errors'] == cpu['failures'] == cpu['skipped'] == 0, 'combined CPU gate')
    s.repaired_inputs()
    repair = pkg.read(s.BASE / 'cpu-repair/result.json')
    pkg.require(repair['passed'] and repair['tests_run'] == 1 and
        repair['errors'] == repair['failures'] == 0 and
        pkg.sha(s.BASE / 'cpu-repair/result.json') == plan['repair_cpu_sha256'],
        'targeted preparation repair gate')
    binding = pkg.read(s.OUT / 'cgroup-binding.json')
    final = pkg.read(s.OUT / 'managed-run/resources-final.json')
    terminal = pkg.read(s.OUT / 'managed-run/terminal.json')
    first = pkg.read(s.OUT / 'first-ready.json')
    pressure = pkg.read(s.OUT / 'saturated-supervisor.json')
    pkg.require(binding['record'] == final['record'] and
        binding['record']['owner'] == first['owner'] == terminal['worker']['owner'] and
        binding['owners']['backend'] == first['backend'] and
        final['record']['policy'] == plan['host_resources'] and
        final['payload_removed'] and final['violation'] is None and
        final['final']['members'] == [] and final['final']['cgroup.events']['populated'] == 0 and
        terminal['host_resource_cleanup']['scope_removed'], 'resource lifecycle')
    expected = resources.limits(plan['host_resources'])
    pkg.require(final['final']['effective_limits'] == expected and
                resources.violation(final['final']) is None, 'final resource limits')
    relative = '/' + str(Path(binding['record']['root']).relative_to(resources.CGROUP))
    pkg.require(binding['memberships']['worker'] == relative + '/control' and
        binding['memberships']['backend'] == relative + '/payload' and
        not binding['memberships']['controller'].startswith(relative), 'control separation')
    attachments = list((s.OUT / 'managed-run').glob('resource-backend-*.json'))
    pkg.require(len(attachments) == 1, 'one pre-exec resource attachment')
    attached = pkg.read(attachments[0])
    pkg.require(attached['owner'] == first['backend'] and
        attached['supervisor'] == first['owner'] and attached['limits'] == expected and
        attached['membership'] == relative + '/payload', 'pre-exec host attachment')
    pkg.require(pressure['host_resources']['policy'] == plan['host_resources'] and
        pressure['host_resources']['violation'] is None and
        pressure['host_resources']['observation']['sampled_monotonic'] >
        first['host_resources']['observation']['sampled_monotonic'], 'fresh host observation under pressure')
    raw = (s.OUT / 'host-samples.jsonl').read_bytes()
    captured = pkg.read(s.OUT / 'host-capture.json')
    samples = [json.loads(line) for line in raw.splitlines()]
    pkg.require(captured['passed'] and captured['samples'] == len(samples) and
                captured['bytes'] == len(raw), 'capture identity')
    observed = audit_samples(samples, binding)
    live = s.OUT / 'business/saturated-capabilities-wire.bin'
    package = Path(plan['package']['package'])
    declaration = pkg.read(package / 'declarations/capabilities.json')
    receipt = pkg.read(s.OUT / 'live-declaration.json')
    pkg.require(receipt['passed'] and pkg.same_json(json.loads(live.read_bytes()), declaration) and
        receipt['observed_sha256'] == pkg.sha(live) and receipt['declared_sha256'] ==
        pkg.sha(package / 'declarations/capabilities.json'), 'actual declarations')
    log = (s.OUT / 'managed-run/backend.log').read_text()
    pkg.require(log.count('grammar worker reaped=1 ') == 4 and
                'shutdown complete (0 in-flight remaining)' in log, 'backend shutdown')
    pkg.require(result['monitored_shared_lock_seconds'] <= s.base.GPU_SECONDS and
                result['driver_seconds'] <= s.base.LIMITS['seconds'], 'runtime bounds')
    result.update(status='ACCEPT_MANAGED_PROFILE_SHORT', cpu_checks=cpu['tests_run']+1,
        preserved_prepare_failure='nginx executable incorrectly resolved under inner configuration directory; zero starts',
        host_envelope_enabled_in_package=True, release_declarations_verified_live=True,
        host_policy=plan['host_resources'], host_capture=observed,
        host_memory_peak_bytes=final['final']['memory.peak'],
        host_memory_events=final['final']['memory.events'],
        host_pids_events=final['final']['pids.events'],
        scope_cleanup=terminal['host_resource_cleanup'])
    # Protocol migration is outside this stage; avoid inheriting the old
    # report's now-obsolete statement about registration in the repository.
    result.pop('protocol_migration_registered')
    return result


def postflight():
    plan = s.check()
    out = s.BASE / 'postflight'
    out.mkdir(mode=0o700)
    s.base.client.a.require_idle(out / 'gpu.json')
    lock = s.base.service.guard.common_lock()
    info = lock.stat()
    pkg.require((info.st_dev, info.st_ino) == (66305, 3294067) and
                s.base.client.a.lock_available(lock), 'original lock unavailable')
    for port in (s.base.PORT, s.base.OPS_PORT, s.base.TLS_PORT):
        pkg.require(not s.base.inspection.listeners(port), 'listener retained')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
    owners = [pkg.read(path) for path in s.OUT.glob('*-identity.json')]
    first = pkg.read(s.OUT / 'first-ready.json')
    owners += [first['owner'], first['backend']]
    for line in (s.OUT / 'host-samples.jsonl').read_text().splitlines():
        owners += [p['owner'] for p in json.loads(line)['processes']]
    unique = {json.dumps(o, sort_keys=True): o for o in owners}
    for owner in unique.values():
        pkg.require(not s.base.gateway.alive(owner), 'owned identity remains')
    record = pkg.read(s.OUT / 'cgroup-binding.json')['record']
    pkg.require(not Path(record['root']).exists() and
        resources.departed(s.OUT / 'managed-run')['scope_removed'], 'scope retained')
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    raw = subprocess.check_output(['systemctl', '--user', 'show', unit,
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'], text=True)
    (out / 'soar-unit.txt').write_text(raw)
    pkg.require(dict(line.split('=', 1) for line in raw.splitlines()) ==
        dict(LoadState='masked', ActiveState='inactive', SubState='dead', MainPID='0'), 'restore service changed')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps',
        '--format', '{{.ID}} {{.Names}}'], text=True)
    (out / 'containers.txt').write_text(containers)
    pkg.require(not containers.strip(), 'running container')
    pkg.save(out / 'result.json', dict(passed=True, owners=list(unique.values()),
        lock=dict(dev=info.st_dev, inode=info.st_ino, released=True),
        scope_removed=True, original_day_plans_unchanged=True,
        default_pair_unchanged=True, long_run=False))
    return dict(postflight=True, owned_identities=len(unique))


def exported(path):
    rel = path.relative_to(s.BASE)
    if 'private' in rel.parts or any(n.startswith('gateway-') for n in rel.parts):
        return path.name == 'manifest.json'
    if rel.parts[0] in ('reviewed-source', 'prepare-failure-source'):
        return path.suffix in ('.py', '.md')
    if path.suffix == '.txt' and path.parent.name == 'postflight':
        return True
    return BASE_EXPORT(path)


def configure():
    s.configure()
    prior.ARCHIVE, prior.audit, prior.exported = ARCHIVE, audit, exported


def archive():
    # Publish the exact additions and reused audit/driver, without secrets.
    names = [*(str(p.relative_to(ROOT)) for p in (ROOT / s.HERE).glob('*.py')),
        'tools/serve_operations_managed/stage.py',
        'tools/serve_operations_managed/report.py',
        'docs/SERVE_MANAGED_PROFILE_2026-11-04.md']
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
    if action == 'verify':
        prior.verify()
    else:
        value = globals()[action]()
        if value is not None:
            print(json.dumps(value))
