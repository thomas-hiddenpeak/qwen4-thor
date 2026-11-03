"""Verify and seal original receiver silence/restart evidence without HTTP."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_watchdog import core as c, watchdog as w, stage as s
from tools.serve_watchdog.storage import encoded
from tools.serve_soak.evidence import guard_archive

BASE, OUT = s.BASE, s.OUT
DEST = ROOT/'docs/evidence/serve-watchdog-20261104'


def read(path):
    return s.n.pkg.read(path)


def identity(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), bytes=path.stat().st_size, sha256=w.file_sha(path))


def verify_identity(row):
    c.require(identity(row['path']) == row, 'local_identity_changed')


def check():
    plan = s.check()
    result = read(OUT/'result.json')
    c.require(result['status'] == 'PASS_LOCAL_INDEPENDENT_HEARTBEAT' and
        result['cleanup_confirmed'] and not result['cleanup_errors'] and
        result['monitor_starts'] == 4 and result['watchdog_starts'] == 2 and
        result['model_starts'] == result['generations'] == 0 and
        result['explicit_http'] == 3 and result['elapsed_seconds'] < 420,
        'execution_incomplete')
    lease = guard_archive(OUT/'lease/events.jsonl', read(OUT/'lease/exit.json'))
    c.require(lease['seconds'] < 420, 'lease_budget')
    initial = read(BASE/'cpu/result.json')
    fixed = read(BASE/'cpu-correction/result.json')
    reviewed = read(BASE/'review-cpu/result.json')
    driver = read(BASE/'driver-cpu/result.json')
    c.require(initial['tests_run'] == 25 and not initial['passed'] and
        initial['failures'] == initial['errors'] == 0 and
        read(BASE/'cpu/check-2.json')['returncode'] == 3 and fixed['passed'] and
        fixed['targeted_tests'] == 1 and reviewed['passed'] and
        reviewed['tests_run'] == 6 and driver['passed'] and
        driver['tests_run'] == 4, 'CPU_chain')
    for number in (0, 1, 3):
        c.require(read(BASE/f'cpu/check-{number}.json')['returncode'] == 0,
                  'unchanged_CPU_failure')
    c.require(not read(BASE/'review/regression-before-first-receipt.json')[
        'passed'], 'reproducer_missing')
    first = read(OUT/'first-present.json')
    missing = read(OUT/'missing.json')
    recovery = read(OUT/'recovered.json')
    c.require(first['state'] == recovery['state'] == 'PRESENT' and
        first['healthy'] and recovery['healthy'] and
        first['age_seconds'] < 3 and recovery['age_seconds'] < 3 and
        recovery['accepted'] > first['accepted'] and
        missing['state'] == 'MISSING' and not missing['healthy'] and
        180 <= missing['age_seconds'] < 183, 'real_silence_or_recovery')
    stopped = read(OUT/'monitors-stopped.json')
    original = stopped['receipt']
    state = c.new_state(plan['watchdog']['policy_sha256'], original['received'])
    state.update(receipt=original, accepted=first['accepted'])
    samples = read(OUT/'silence-samples.json')
    c.require(2 < len(samples) < 1050 and samples[-1] == missing,
              'silence_samples')
    previous = None
    for index, sample in enumerate(samples):
        wanted = c.status(state, sample['observed'])
        c.require(sample['state'] == wanted['state'] and
            sample['age_seconds'] == wanted['age_seconds'] and
            sample['healthy'] == wanted['healthy'], 'silence_recalculation')
        c.require(sample['state'] == ('MISSING' if index == len(samples)-1
                                      else 'PRESENT'), 'premature_expiry')
        if previous:
            c.require(0 < sample['age_seconds']-previous['age_seconds'] < 3,
                      'silence_observation_gap')
        previous = sample
    projected = read(OUT/'missing-projection.json')
    c.require(projected['state'] == 'MISSING' and
        projected['last_received_unix_seconds'] == original['received']['unix_seconds'] and
        'state="MISSING"} 1' in (OUT/'missing.prom').read_text(), 'missing_projection')
    stale = read(OUT/'observer-stale.json')
    c.require(stale['age_seconds'] > 20 and
        stale['observed']-stale['stamp'] > 20 and
        stale['file_sha256'] == w.file_sha(OUT/'missing.prom'), 'stale_observer')
    restarted = read(OUT/'observer-restarted.json')
    c.require(restarted['state'] == 'MISSING' and restarted['accepted'] ==
        first['accepted'] and restarted['last_received_unix_seconds'] ==
        original['received']['unix_seconds'], 'restart_reset_deadline')
    for label, code in (('unauthorized', 401), ('wrong-deployment', 422),
                        ('ordinary-not-heartbeat', 202)):
        row = read(OUT/(label+'-http.json'))
        c.require(row['status'] == row['expected'] == code and
            row['before_sha256'] == row['after_sha256'] and row['bytes'] == 0,
            'invalid_delivery_changed_state')
    callbacks = []
    for path in sorted((OUT/'receiver').glob('*-receipt.json')):
        row = read(path)
        raw = (path.parent/f"{row['index']:03d}-body.json").read_bytes()
        c.require(row['index'] == len(callbacks) and row['path'] == '/alerts' and
            row['payload_sha256'] == hashlib.sha256(raw).hexdigest(), 'callback_identity')
        if row['status'] == 204:
            c.heartbeat(raw, 'thor-acceptance')
            c.require(row['authorization_valid'] and row['result']['durable'] and
                row['result']['disposition'] == 'accepted', 'callback_not_confirmed')
        elif row['status'] == 202:
            c.require(row['authorization_valid'] and
                row['result'] == dict(fixture_nonheartbeat=True), 'synthetic_sink_mismatch')
        elif row['status'] == 401:
            c.require(not row['authorization_valid'] and not raw, 'auth_refusal')
        elif row['status'] == 422:
            c.require(row['result']['rejected'] == 'heartbeat_labels', 'contract_refusal')
        else:
            raise c.ContractError('unexpected_callback_status')
        callbacks.append(row)
    accepted = [row for row in callbacks if row['status'] == 204]
    c.require(len(callbacks) <= 64 and len(accepted) >= 2 and
        len(accepted) == recovery['accepted'] and
        accepted[0]['monotonic'] < stopped['monotonic'] and
        accepted[-1]['monotonic'] > stopped['monotonic']+180,
        'real_delivery_sequence')
    owners = read(OUT/'custody.json')['owners']
    c.require(len(owners) == 6, 'direct_process_count')
    for label, owner in owners.items():
        before = read(OUT/(label+'-waitable.json'))
        after = read(OUT/(label+'-exit.json'))
        c.require(before['owner'] == owner and before['returncode'] ==
                  after['returncode'] == 0, 'owned_exit')
        if label.startswith(('prometheus-', 'alertmanager-')):
            terminal = read(OUT/label/'terminal.json')
            c.require(terminal['state'] == 'STOPPED' and terminal['starts'] == 1
                and terminal['restarts'] == 0 and terminal['cleanup_confirmed']
                and terminal['child_returncode'] == 0, 'monitor_terminal')
    final = c.validate(read(OUT/'final-ledger.json'), plan['watchdog']['policy_sha256'])
    states = [event['state'] for event in final['history']]
    c.require('MISSING' in states and states[-1] == 'PRESENT' and
        final['accepted'] == len(accepted), 'final_state')
    gpu = read(OUT/'gpu-final.json')
    c.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU_cleanup')
    return dict(passed=True, source_commit=plan['source_commit'],
        effective_cpu_tests=26, physical_cpu_tests=32, driver_cpu_tests=4,
        official_rule_scenarios=11, initial_failures_preserved=True,
        monitor_starts=4, watchdog_starts=2, models=0, generations=0,
        callback_attempts=len(callbacks), heartbeats_confirmed=len(accepted),
        callback_statuses={str(code):sum(v['status'] == code for v in callbacks)
                           for code in (202, 204, 401, 422)},
        explicit_http=3, silence_samples=len(samples),
        missing_age_seconds=missing['age_seconds'],
        stale_observer_seconds=stale['age_seconds'],
        transition_states=states, guard=lease,
        whole_work_seconds=result['elapsed_seconds'],
        external_receiver_accepted=False, long_test_started=False,
        source_freshness_or_authenticated_replay_guaranteed=False)


def postflight():
    plan = s.check()
    owners = read(OUT/'custody.json')['owners'].copy()
    for label in tuple(owners):
        path = OUT/(label+'-listening.json')
        if path.exists(): owners[label+'-worker'] = read(path)['child']
    c.require(len(owners) == 10 and all(not s.n.m.alive(v)
        for v in owners.values()), 'owned_process_remained')
    lock = s.guard.common_lock()
    with lock.open('r+') as stream:
        s.fcntl.flock(stream, s.fcntl.LOCK_EX | s.fcntl.LOCK_NB)
        info = os.fstat(stream.fileno())
        c.require((info.st_dev, info.st_ino) == (66305, 3294067), 'lock_replaced')
    c.require(not os.path.lexists(lock.with_name(lock.name+'.recovery-required')),
              'recovery_marker')
    name = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/name
    c.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'mask_changed')
    service = subprocess.check_output(['systemctl', '--user', 'show', name,
        '-p', 'ActiveState', '-p', 'MainPID', '-p', 'UnitFileState'], text=True, timeout=10)
    c.require(all(v in service for v in ('ActiveState=inactive', 'MainPID=0',
                                       'UnitFileState=masked')), 'recovery_service_active')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '--format',
        '{{.ID}} {{.Names}}'], text=True, timeout=10)
    c.require(not containers.strip(), 'containers_present')
    gpu = s.nvml_process_query.query(timeout=5)
    c.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU_not_empty')
    return dict(passed=True, owned_processes_gone=len(owners), service=service,
        containers=containers, gpu=gpu, lock_available=True, lock_device=info.st_dev,
        lock_inode=info.st_ino, default_binary=plan['default_binary'],
        default_sidecar=plan['default_sidecar'], day_sha256=plan['day_sha256'],
        old_day_sha256=plan['old_day_sha256'], long_test_started=False)


def secrets_and_private():
    secret_values = [(BASE/'private/token-private').read_bytes().strip()]
    return secret_values


def local_only(path):
    return (path.is_relative_to(BASE/'private') or
        path.is_relative_to(BASE/'monitor-state') or
        (path.is_relative_to(BASE/'archive-failed-1') and path.suffix == '.gz') or
        path.name in (*s.n.BINARIES, 'token-private', 'url-private', 'key.pem') or
        path.name.endswith('-private.log'))


def contains_private_key(raw):
    # Match actual PEM armor lines, not a quoted marker in auditor source.
    return bool(re.search(rb'(?m)^-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----\r?$', raw))


def archive():
    value = check()
    s.save(BASE/'verified.json', value)
    s.save(BASE/'postflight.json', postflight())
    c.require(not DEST.exists(), 'preserve_archive')
    DEST.mkdir()
    records, local = [], []
    secret_values = secrets_and_private()
    with (DEST/'records.tar.gz').open('xb') as stream, \
         gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as compressed, \
         tarfile.open(fileobj=compressed, mode='w|') as archive_file:
        for path in sorted(BASE.rglob('*')):
            if not path.is_file() or path.is_symlink(): continue
            row = identity(path)
            if local_only(path):
                local.append(row); continue
            raw = path.read_bytes()
            c.require(not any(key in raw for key in secret_values) and
                not contains_private_key(raw), 'private_material_in_archive:'+str(path))
            name = str(path.relative_to(BASE))
            entry = tarfile.TarInfo(name); entry.size = len(raw)
            entry.mode = 0o600; entry.mtime = 0
            archive_file.addfile(entry, io.BytesIO(raw))
            records.append(dict(row, member=name))
    evaluated = {}
    for module in tuple(sys.modules.values()):
        path = getattr(module, '__file__', None)
        if path and Path(path).resolve().is_relative_to(ROOT/'tools'):
            evaluated[str(Path(path).resolve())] = identity(path)
    manifest = dict(version=1, records=records, local_artifacts=local,
        evaluator=list(evaluated.values()),
        archive=identity(DEST/'records.tar.gz'),
        scope='Immutable full stage records; credentials, binaries and mutable monitor storage remain local by hash.',
        cpu_fixture_scope='CPU outputs and complete tested source retained; private per-test work directories are not archived.')
    (DEST/'manifest.json').write_bytes(encoded(manifest))
    (DEST/'result.json').write_bytes(encoded(value))
    return verify_archive()


def verify_archive():
    manifest = read(DEST/'manifest.json')
    verify_identity(manifest['archive'])
    expected = {row['member']:row for row in manifest['records']}
    seen = set(); secrets = secrets_and_private()
    with tarfile.open(DEST/'records.tar.gz', 'r:gz') as archive_file:
        for member in archive_file:
            c.require(member.isfile() and member.name in expected and
                      member.name not in seen, 'unexpected_archive_member')
            row = expected[member.name]; raw = archive_file.extractfile(member).read()
            c.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest()
                == row['sha256'] and not any(key in raw for key in secrets)
                and not contains_private_key(raw), 'archive_bytes')
            verify_identity({k:row[k] for k in ('path', 'bytes', 'sha256')})
            seen.add(member.name)
    c.require(seen == expected.keys(), 'missing_archive_member')
    for row in (*manifest['local_artifacts'], *manifest['evaluator']):
        verify_identity(row)
    value = check()
    c.require(value == read(DEST/'result.json'), 'archived_result_changed')
    return dict(value, archived_records=len(seen),
        local_artifacts=len(manifest['local_artifacts']),
        archive_files=3, archive_bytes=sum(p.stat().st_size for p in DEST.iterdir()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'postflight', 'archive', 'verify-archive'))
    args = parser.parse_args()
    print(json.dumps(globals()[args.action.replace('-', '_')]()))
