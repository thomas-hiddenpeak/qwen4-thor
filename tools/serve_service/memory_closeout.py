"""Verify and seal the existing memory-stop run without any new model/HTTP."""
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
from tools.serve_service import memory_acceptance as phase, package as pkg
from tools.serve_service import acceptance as a
from tools.serve_handoff.stage import h

DEST = ROOT/'docs/evidence/serve-memory-stop-20261103'


def check():
    plan = phase.check()
    result = pkg.read(phase.OUT/'result.json')
    pkg.require(result['status'] == 'PASS_MANAGED_MEMORY_STOP' and
        result['cleanup_confirmed'] and result['starts'] == 1 and
        result['successful_requests'] == 4 and result['real_tool_executions'] == 1
        and result['counts'] == dict(generations=4, negatives=1, controls=15)
        and result['elapsed_seconds'] < phase.LIMITS['seconds'] and
        result['gpu_window_elapsed'] < a.GPU_WINDOW, 'acceptance incomplete')
    phase.protected_terminal(pkg.read(phase.OUT/'managed-run/terminal.json'))
    a.healthy_idle(pkg.read(phase.OUT/'idle-before-pressure-metrics.json')['metrics'], 4)
    rows = [json.loads(line) for line in
            (phase.OUT/'pressure.jsonl').read_text().splitlines()]
    pkg.require(rows[-1]['final'] and rows[-1]['allocated'] == 0 and
                rows[-1]['error'] is None, 'pressure helper cleanup missing')
    samples = [r[k] for r in rows for k in ('before', 'after') if k in r]
    minimum_available = min(r['MemAvailable'] for r in samples)
    minimum_free = min(r['MemFree'] for r in samples)
    peak = max(r['allocated'] for r in rows)
    pkg.require(minimum_available >= phase.pressure.MIN_AVAILABLE and
        minimum_free >= phase.pressure.MIN_FREE and
        peak <= phase.pressure.MAX_BYTES, 'pressure safety budget violated')
    removed = pkg.read(phase.OUT/'unit-removed.json')['stdout']
    pkg.require('LoadState=not-found' in removed and 'MainPID=0' in removed,
                'temporary unit not removed')
    gpu = pkg.read(phase.OUT/'gpu-final.json')
    pkg.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU remained occupied')
    events = [json.loads(line) for line in
        (phase.OUT/'managed-run/lease/events.jsonl').read_text().splitlines()]
    polls = [r for r in events if r['event'] in ('preflight', 'monitor')]
    pkg.require(polls and all(not r['conflicts'] for r in polls),
                'environment conflict in preserved monitor')
    return plan, dict(pressure_rows=len(rows), pressure_peak_bytes=peak,
        minimum_available_bytes=minimum_available, minimum_free_bytes=minimum_free,
        guard_observations=len(polls), full_observation_upper_bound=203)


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
                        hashlib.sha256(raw).hexdigest() == row['sha256'],
                        'archive content changed')
            h.check_identity(row)
            seen.add(member.name)
    pkg.require(seen == expected.keys(), 'missing archived record')
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
    pkg.require(mask.is_symlink() and os.readlink(mask) == '/dev/null',
                'persistent recovery-service mask missing')
    query = subprocess.check_output(['systemctl', '--user', 'show', unit,
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'UnitFileState'],
        text=True, timeout=10)
    pkg.require(all(v in query for v in ('MainPID=0', 'ActiveState=inactive',
                                        'UnitFileState=masked')), 'SOAR changed')
    lock = a.guard.common_lock()
    pkg.require(a.lock_available(lock) and not os.path.lexists(
        lock.with_name(lock.name+'.recovery-required')), 'lease/recovery pending')
    old = pkg.read(phase.BASE/'cpu/result.json')['legacy_package']
    pkg.verify(old['path'], old['manifest_sha256'])
    h.save(phase.BASE/'accepted-package.json', dict(
        status='PASS_MANAGED_MEMORY_STOP', package=plan['package'],
        operational_source_commit=plan['source_commit'],
        acceptance=h.identity(phase.OUT/'result.json'), statistics=stats,
        default_binary_sha256=plan['binary_sha256'],
        default_release_sha256=plan['default_release_sha256'],
        accepted_critical_memory_mib=phase.FLOOR_MIB,
        default_1024_mib_threshold_cpu_only=True,
        physical_ram_hard_limit=False, persistent_service_installed=False,
        rollback_package=old, new_model_or_http=0))
    h.save(phase.BASE/'postflight.json', dict(passed=True, soar_unit=query,
        lock_inode=lock.stat().st_ino, lock_available=True, recovery_marker=False,
        memory=phase.pressure.memory(), additional_systemd_queries=1,
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
    aliases = {name+'.json':h.identity(path) for name,path in dict(
        plan=phase.PLAN, result=phase.OUT/'result.json',
        terminal=phase.OUT/'managed-run/terminal.json',
        accepted_package=phase.BASE/'accepted-package.json',
        postflight=phase.BASE/'postflight.json',
        cpu=phase.BASE/'cpu-final/result.json').items()}
    for name, row in aliases.items():
        h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases=aliases, archiver=h.identity(Path(__file__)),
        scope='One real managed launch, four successful requests, project tool, '
        '30GiB critical floor, bounded real pressure and terminal cleanup. '
        'No physical RAM cap, default-1GiB GPU test or persistent deployment.'))
    h.seal(DEST)
    return verify()


if __name__ == '__main__':
    if sys.argv[1:] not in (['create'], ['verify']):
        raise SystemExit('create | verify')
    print(json.dumps(create() if sys.argv[1:] == ['create'] else verify()))
