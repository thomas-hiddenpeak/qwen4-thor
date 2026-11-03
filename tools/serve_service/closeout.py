"""Seal the bounded managed-service acceptance; never start a service."""
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
sys.path.insert(0, str(ROOT))
from tools.serve_service import supplement as s, acceptance as a, package as pkg
from tools.serve_handoff.stage import h

DEST = ROOT/'docs/evidence/serve-managed-service-20261103'


def check():
    plan = s.check()
    result = pkg.read(s.OUT/'result.json')
    pkg.require(result['status'] == 'PASS_MANAGED_SYSTEMD_MTP' and result['cleanup_confirmed']
        and result['counts'] == dict(generations=6, negatives=3, controls=17) and
        result['starts'] == 2 and result['successful_requests'] == 6 and
        result['actual_backend_replacements'] == result['pending_restarts_cancelled'] == 1 and
        result['gpu_window_elapsed'] <= a.GPU_WINDOW and result['elapsed_seconds'] <= s.LIMITS['seconds'],
        'incomplete actual acceptance')
    terminal = pkg.read(s.OUT/'managed-run/terminal.json')
    pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
        terminal['guard']['status'] == 'COMPLETED' and not terminal['automatic_reactivation'] and
        terminal['worker']['attempts'] == terminal['worker']['restarts'] == 2, 'terminal mismatch')
    first, second = [pkg.read(s.OUT/(label+'-ownership.json')) for label in ('first-ready', 'second-ready')]
    pkg.require(first['control'] == second['control'] and first['lock_identity'] == second['lock_identity']
        and first['owners'][-1] != second['owners'][-1], 'controller/lock/replacement identity')
    for label in ('first-idle', 'second-idle'):
        a.healthy_idle(pkg.read(s.OUT/(label+'-metrics.json'))['metrics'], 3)
    removed = pkg.read(s.OUT/'unit-removed.json')['stdout']
    pkg.require('LoadState=not-found' in removed and 'MainPID=0' in removed and
        len(list((s.OUT/'managed-run').glob('attempt-*.json'))) == 2, 'unit remained/restarted')
    pkg.require(pkg.read(s.OUT/'gpu-final.json')['status'] == 'OK' and
                not pkg.read(s.OUT/'gpu-final.json')['pids'], 'GPU cleanup missing')
    return plan, result


def verify():
    h.verify_seal(DEST)
    manifest = pkg.read(DEST/'manifest.json')
    expected = {row['member']:row for row in manifest['records']}
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            pkg.require(member.isfile() and member.name in expected and member.name not in seen,
                        'unexpected archive member')
            raw = archive.extractfile(member).read(); row = expected[member.name]
            pkg.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                        'archive bytes changed')
            h.check_identity(row); seen.add(member.name)
    pkg.require(seen == expected.keys(), 'missing original record')
    for row in manifest['local_artifacts']: h.check_identity(row)
    for name,row in manifest['aliases'].items():
        h.check_identity(row); pkg.require(pkg.sha(DEST/name) == row['sha256'], 'alias changed')
    check()
    return dict(passed=True, records=len(seen), local_artifacts=len(manifest['local_artifacts']),
                new_model_or_http=0)


def create():
    plan, result = check()
    unit = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/unit
    pkg.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'persistent SOAR mask missing')
    query = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'MainPID',
        '-p', 'ActiveState', '-p', 'UnitFileState'], capture_output=True, text=True, timeout=10, check=True)
    pkg.require(all(value in query.stdout for value in ('MainPID=0', 'ActiveState=inactive',
        'UnitFileState=masked')), 'SOAR service changed')
    pkg.require(a.lock_available(a.guard.common_lock()), 'shared GPU lock is busy')
    pkg.require(not list((ROOT/'build').glob('*recovery*.json')), 'recovery pending')
    h.save(s.BASE/'accepted-package.json', dict(status='PASS_MANAGED_SYSTEMD_MTP',
        package=plan['package'], runtime_source_commit='fded390d',
        operational_tool_commit=plan['source_commit'], acceptance=h.identity(s.OUT/'result.json'),
        terminal=h.identity(s.OUT/'managed-run/terminal.json'),
        installed_pair=dict(binary_sha256=plan['binary_sha256'], release_sha256=plan['default_release_sha256']),
        preparation_manifest_unchanged=True, new_persistent_service=False,
        physical_ram_bound=False, performance_acceptance=False))
    h.save(s.BASE/'postflight.json', dict(passed=True, gpu=h.identity(s.OUT/'gpu-final.json'),
        shared_lock_available=True, lock_inode=a.guard.common_lock().stat().st_ino,
        soar_mask=str(mask), soar_unit=query.stdout, additional_readonly_systemd_queries=1,
        new_model_or_http=0, no_pending_recovery=True))
    h.seal(s.OUT)
    DEST.mkdir()
    selected = [('supplement/'+str(p.relative_to(s.BASE)),p)
                for p in sorted(s.BASE.rglob('*')) if p.is_file()]
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for member,path in selected:
                    pkg.require(not path.is_symlink(), 'unexpected evidence link')
                    row = h.identity(path)
                    with path.open('rb') as source: magic = source.read(4)
                    if magic == b'\x7fELF': local.append(row); continue
                    raw = path.read_bytes()
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
                    archive.addfile(entry, io.BytesIO(raw))
                    records.append(dict(**row,member=member))
    aliases = dict(plan=s.PLAN, result=s.OUT/'result.json',
        terminal=s.OUT/'managed-run/terminal.json', guard=s.OUT/'managed-run/lease/exit.json',
        accepted_package=s.BASE/'accepted-package.json', postflight=s.BASE/'postflight.json')
    aliases = {name+'.json':h.identity(path) for name,path in aliases.items()}
    for name,row in aliases.items(): h.copy_checked(row,DEST/name)
    h.save(DEST/'manifest.json',dict(records=records, local_artifacts=local, aliases=aliases,
        archiver=h.identity(Path(__file__)), prior_failure_archive=h.identity(
            ROOT/'docs/evidence/serve-structured-handoff-20261103/manifest.json'),
        scope='Actual systemd, two backend starts, six model requests, two real project tools, '
        'one replacement and stop during backoff. No persistent deployment/long-soak/performance claim.'))
    h.seal(DEST)
    return verify()


if __name__ == '__main__':
    print(json.dumps(create() if sys.argv[1:] == ['create'] else verify()))
