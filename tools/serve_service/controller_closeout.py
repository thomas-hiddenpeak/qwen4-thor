"""Read-only acceptance and custody archive of the controller-death run."""
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
from tools.serve_service import controller_death as phase, acceptance as a
from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_handoff.stage import h

DEST = ROOT/'docs/evidence/serve-controller-death-20261103'


def check():
    plan = phase.check()
    result = pkg.read(phase.OUT/'result.json')
    pkg.require(result['status'] == 'PASS_CONTROLLER_DEATH_ACTIVE_QUEUE' and
        result['cleanup_confirmed'] and result['helper_cleanup_signals'] == 0 and
        result['starts'] == 1 and result['successes'] == 4 and
        result['shutdown_errors'] == 2 and result['real_tool_executions'] == 1 and
        result['counts'] == dict(generations=6, negatives=1, controls=5) and
        result['elapsed_seconds'] < 600 and result['gpu_window_elapsed'] < 220,
        'actual controller-death acceptance incomplete')
    death = pkg.read(phase.OUT/'controller-death-verified.json')
    pkg.require(death['passed'] and death['controller_returncode'] == -9 and
        death['grammar_reaped'] == 4 and death['worker']['cleanup_confirmed'] and
        death['worker']['attempts'] == 1 and death['worker']['restarts'] == 0,
        'worker did not naturally drain')
    for label in ('active', 'queued'):
        pkg.require(pkg.read(phase.OUT/(label+'-http.json'))['eof'], 'wire EOF missing')
        phase.stream_terminal((phase.OUT/(label+'-wire.bin')).read_bytes(),
            graceful=True, validator=phase.Draft202012Validator(pkg.read(
                ROOT/'docs/serve-transport-v1.schema.json')))
    a.healthy_idle(pkg.read(phase.OUT/'idle-before-crash-metrics.json')['metrics'], 4)
    pkg.require(not (phase.OUT/'managed-run/terminal.json').exists() and
        not (phase.OUT/'managed-run/lease/exit.json').exists(), 'original terminal fabricated')
    reports = [pkg.read(phase.OUT/(name+'-ownership.json')) for name in
               ('first-ready', 'before-crash')]
    pkg.require(reports[0] == reports[1], 'process/lease descriptors changed')
    copy = pkg.read(phase.OUT/'custody/lease-copy.json')
    pkg.require(copy['inode'] == reports[0]['lock_inode'] == 3294067,
                'original inode not preserved')
    custody = pkg.read(phase.OUT/'custody/exit.json')
    pkg.require(custody['status'] == 'COMPLETED' and custody['cleanup_confirmed'],
                'independent custody did not complete')
    original = [json.loads(line) for line in
        (phase.OUT/'managed-run/lease/events.jsonl').read_text().splitlines()]
    original = [r for r in original if r['event'] in ('preflight', 'monitor')]
    independent = [json.loads(line) for line in
                   (phase.OUT/'custody/events.jsonl').read_text().splitlines()]
    pkg.require(original and independent and all(not r['conflicts'] for r in original)
        and all('observation' in r and not r['observation']['conflicts'] for r in independent),
        'environment abort in monitor records')
    gpu = pkg.read(phase.OUT/'gpu-final.json')
    pkg.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU cleanup missing')
    return plan, dict(original_guard_observations=len(original),
        independent_guard_observations=len(independent), explicit_controls=5,
        supervisor_http_upper_bound=188, full_observation_upper_bound=193)


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
            h.check_identity(row); seen.add(member.name)
    pkg.require(seen == expected.keys(), 'missing archived record')
    for row in manifest['local_artifacts']: h.check_identity(row)
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
    query = subprocess.check_output(['systemctl', '--user', 'show', unit,
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'UnitFileState'], text=True, timeout=10)
    pkg.require(all(v in query for v in ('MainPID=0','ActiveState=inactive',
                                        'UnitFileState=masked')), 'SOAR changed')
    lock = a.guard.common_lock()
    pkg.require(a.lock_available(lock) and not os.path.lexists(
        lock.with_name(lock.name+'.recovery-required')), 'GPU lock/recovery pending')
    h.save(phase.BASE/'accepted-package.json', dict(
        status='PASS_CONTROLLER_DEATH_ACTIVE_QUEUE', package=plan['package'],
        validation_source_commit=plan['source_commit'], statistics=stats,
        production_service_unchanged=True, default_pair_unchanged=True,
        acceptance=h.identity(phase.OUT/'result.json'),
        original_guard_intentionally_has_no_terminal=True,
        custody=h.identity(phase.OUT/'custody/exit.json'),
        persistent_service_installed=False, physical_ram_limit_proved=False))
    h.save(phase.BASE/'postflight.json', dict(passed=True, soar_unit=query,
        lock_inode=lock.stat().st_ino, lock_available=True,
        last_lock_record=pkg.read(lock), stale_held_metadata_is_not_a_live_lock=True,
        recovery_marker=False, gpu=h.identity(phase.OUT/'gpu-final.json'),
        additional_systemd_queries=1, new_model_or_http=0))
    h.seal(phase.OUT)
    DEST.mkdir()
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in sorted(phase.BASE.rglob('*')):
                    if not path.is_file(): continue
                    pkg.require(not path.is_symlink(), 'unexpected evidence link')
                    row = h.identity(path)
                    with path.open('rb') as source: magic = source.read(4)
                    if magic == b'\x7fELF':
                        local.append(row); continue
                    raw = path.read_bytes(); member = str(path.relative_to(phase.BASE))
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
                    archive.addfile(entry, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    aliases = {name+'.json':h.identity(path) for name,path in dict(
        plan=phase.PLAN, result=phase.OUT/'result.json',
        custody=phase.OUT/'custody/exit.json',
        accepted_package=phase.BASE/'accepted-package.json',
        postflight=phase.BASE/'postflight.json',
        cpu=phase.BASE/'entry-corrected/result.json').items()}
    for name, row in aliases.items(): h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases=aliases, archiver=h.identity(Path(__file__)),
        scope='Standalone production controller SIGKILL with active output and prepared '
        'grammar queue; no helper termination signals. Original guard interrupted; '
        'same-description custody and independent monitoring cover full cleanup.'))
    h.seal(DEST)
    return verify()


if __name__ == '__main__':
    if sys.argv[1:] not in (['create'], ['verify']): raise SystemExit('create | verify')
    print(json.dumps(create() if sys.argv[1:] == ['create'] else verify()))
