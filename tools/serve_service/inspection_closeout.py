"""Read-only acceptance checks and custody archive for SRV-17."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import inspection_acceptance as phase, package as pkg
from tools.serve_service import acceptance as a
from tools.serve_handoff.stage import h

DEST = ROOT/'docs/evidence/serve-inspection-20261103'


def check():
    plan = phase.check()
    result = pkg.read(phase.OUT/'result.json')
    pkg.require(result['status'] == 'PASS_READONLY_SERVICE_INSPECTION' and
        result['cleanup_confirmed'] and result['starts'] == 1 and
        result['successful_requests'] == 3 and result['real_tool_executions'] == 1 and
        result['counts'] == dict(generations=3, negatives=0, controls=5) and
        result['elapsed_seconds'] < 600 and result['gpu_window_elapsed'] < 220,
        'actual inspection acceptance incomplete')
    reports = {}
    for label in ('legacy', 'live', 'stopped'):
        row = pkg.read(phase.OUT/(label+'-inspection.json'))
        report = reports[label] = json.loads(row['stdout'])
        pkg.require(row['returncode'] == (0 if report['complete'] else 2),
                    'CLI exit code differs from observation completeness')
    pkg.require(reports['legacy']['errors'] == ['legacy_or_different_observation_scope']
        and not reports['legacy']['cleanup_observed'] and
        reports['legacy']['lock']['historical_record']['state'] == 'held',
        'legacy evidence uncertainty lost')
    pkg.require(reports['live']['run_state'] == 'OWNER_PRESENT' and
        reports['live']['lock']['state'] == 'BUSY' and
        reports['stopped']['run_state'] == 'INACTIVE_OBSERVED' and
        reports['stopped']['cleanup_observed'], 'live/stopped observations differ')
    for label in ('ordinary-json', 'project-read', 'project-answer'):
        report = pkg.read(phase.OUT/(label+'-http.json'))
        pkg.require(report['eof'] and report['status'] == 200, 'HTTP incomplete')
    a.healthy_idle(pkg.read(phase.OUT/'idle-metrics.json')['metrics'], 3)
    runtime = phase.OUT/'managed-run'
    terminal = pkg.read(runtime/'terminal.json')
    pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
        terminal['guard']['status'] == 'COMPLETED' and
        terminal['worker']['attempts'] == 1 and terminal['worker']['restarts'] == 0,
        'normal cleanup not confirmed')
    pkg.require(phase.record_hashes(phase.LEGACY) == pkg.read(
        phase.OUT/'legacy-preserved.json'), 'legacy evidence changed')
    pkg.require(phase.record_hashes(runtime) == pkg.read(
        phase.OUT/'stopped-preserved.json')['records'], 'stopped records changed')
    events = [json.loads(line) for line in
              (runtime/'lease/events.jsonl').read_text().splitlines()]
    observations = [r for r in events if r['event'] in ('preflight', 'monitor')]
    pkg.require(observations and all(not r['conflicts'] for r in observations),
                'guard conflict')
    gpu = pkg.read(phase.OUT/'gpu-final.json')
    pkg.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU cleanup missing')
    return plan, dict(guard_observations=len(observations), explicit_controls=5,
                     supervisor_http_upper_bound=188, full_control_upper_bound=193)


def verify():
    h.verify_seal(DEST)
    manifest = pkg.read(DEST/'manifest.json')
    expected = {r['member']:r for r in manifest['records']}
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            pkg.require(member.isfile() and member.name in expected and
                        member.name not in seen, 'unexpected archive member')
            raw, row = archive.extractfile(member).read(), expected[member.name]
            pkg.require(len(raw) == row['bytes'] and
                hashlib.sha256(raw).hexdigest() == row['sha256'], 'archive bytes differ')
            h.check_identity(row)
            seen.add(member.name)
    pkg.require(seen == expected.keys(), 'missing archive member')
    for row in manifest['local_artifacts']:
        h.check_identity(row)
    for name, row in manifest['aliases'].items():
        h.check_identity(row)
        pkg.require(pkg.sha(DEST/name) == row['sha256'], 'alias differs')
    check()
    return dict(passed=True, records=len(seen),
        local_artifacts=len(manifest['local_artifacts']), new_model_or_http=0)


def create():
    plan, stats = check()
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/unit
    pkg.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'SOAR mask missing')
    state = subprocess.check_output(['systemctl', '--user', 'show', unit,
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'UnitFileState'], text=True, timeout=10)
    pkg.require(all(v in state for v in ('MainPID=0', 'ActiveState=inactive',
                                        'UnitFileState=masked')), 'SOAR changed')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '-a',
        '--format', '{{.Names}} {{.Status}}'], text=True, timeout=10)
    pkg.require(not containers.strip(), 'container inventory no longer empty')
    lock = a.guard.common_lock()
    pkg.require(a.lock_available(lock) and not os.path.lexists(
        lock.with_name(lock.name+'.recovery-required')), 'lease or recovery pending')
    pkg.require(lock.stat().st_ino == 3294067, 'original lock inode changed')
    h.save(phase.BASE/'accepted-package.json', dict(status='PASS_READONLY_SERVICE_INSPECTION',
        package=plan['package'], validation_source_commit=plan['source_commit'],
        statistics=stats, acceptance=h.identity(phase.OUT/'result.json'),
        default_pair_unchanged=True, persistent_service_installed=False,
        physical_ram_limit_proved=False, snapshots_do_not_reserve_future_capacity=True))
    h.save(phase.BASE/'postflight.json', dict(passed=True, soar_unit=state,
        container_inventory=containers, lock_inode=lock.stat().st_ino,
        lock_available=True, recovery_marker=False,
        gpu=h.identity(phase.OUT/'gpu-final.json'), new_model_or_http=0))
    h.seal(phase.OUT)
    DEST.mkdir()
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in sorted(phase.BASE.rglob('*')):
                    if not path.is_file():
                        continue
                    pkg.require(not path.is_symlink(), 'unexpected evidence link')
                    row = h.identity(path)
                    with path.open('rb') as source:
                        magic = source.read(4)
                    if magic == b'\x7fELF':
                        local.append(row)
                        continue
                    raw = path.read_bytes()
                    member = str(path.relative_to(phase.BASE))
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
                    archive.addfile(entry, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    aliases = {name+'.json':h.identity(path) for name, path in dict(
        plan=phase.PLAN, result=phase.OUT/'result.json', cpu=phase.BASE/'cpu/result.json',
        accepted_package=phase.BASE/'accepted-package.json',
        postflight=phase.BASE/'postflight.json').items()}
    for name, row in aliases.items():
        h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases=aliases, archiver=h.identity(Path(__file__)),
        scope='Read-only legacy/live/stopped inspection. One actual managed generation, '
        'three successful requests including a real project-record tool loop; '
        'no C++ runtime or default pair change. Legacy uncertainty is preserved.'))
    h.seal(DEST)
    return verify()


if __name__ == '__main__':
    if sys.argv[1:] not in (['create'], ['verify']):
        raise SystemExit('create | verify')
    print(json.dumps(create() if sys.argv[1:] == ['create'] else verify()))
