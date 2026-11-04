"""Verify original notification, recovery and lease evidence without HTTP."""
import argparse
from dataclasses import asdict
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
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_notify import stage as s, notify as n, receiver
from tools.serve_handoff.stage import h
from tools.serve_soak.evidence import guard_archive

BASE, OUT = s.BASE, s.OUT
DEST = ROOT/'docs/evidence/serve-notify-20261104'
FIXTURES = (ROOT/'build/serve-notify-tests', ROOT/'build/serve-notify-process-tests')


def check():
    s.configure(); plan = s.check()
    value = n.pkg.read(OUT/'result.json')
    n.require(value['status'] == 'PASS_LOCAL_NOTIFICATION_AND_RECOVERY' and
        value['cleanup_confirmed'] and value['model_starts'] == 2 and
        value['monitor_starts'] == 3 and value['counts']['generations'] == 4 and
        value['counts']['controls'] <= 256 and value['elapsed_seconds'] < 360,
        'original execution incomplete')
    guard = guard_archive(OUT/'lease/events.jsonl', n.pkg.read(OUT/'lease/exit.json'))
    n.require(guard['seconds'] < 360, 'whole lease exceeded budget')
    ids = set()
    labels = ('ordinary', 'project-read', 'project-answer', 'recovery')
    for label in labels:
        body = n.pkg.read(OUT/(label+'-request.json'))
        http = n.pkg.read(OUT/(label+'-http.json'))
        raw = (OUT/(label+'-wire.bin')).read_bytes()
        parsed = (s.a.read_sse if body['stream'] else s.a.read_json)(io.BytesIO(raw))
        n.require(asdict(parsed) == n.pkg.read(OUT/(label+'-response.json')) and
            parsed.id == body['request_id'] and parsed.id not in ids and
            http['status'] == 200 and http['eof'] and
            http['headers']['X-Request-Id'] == parsed.id, 'model wire identity')
        ids.add(parsed.id)
        if label in ('ordinary', 'recovery'):
            n.require(parsed.finish_reason == 'stop' and
                parsed.message['content'].strip() == '710003', 'ordinary quality')
        source_log = OUT/('model-2.log' if label == 'recovery' else 'model-1.log')
        ends = [line for line in source_log.read_text().splitlines()
                if 'event=end ' in line and f'id={parsed.id} ' in line]
        n.require(len(ends) == 1 and 'released=1' in ends[0] and
                  'reason=completed' in ends[0], 'generation trace differs/replayed')
    tool = n.pkg.read(OUT/'project-tool-result.json')
    source = json.loads(tool['content'])['source']
    expected = '\n'.join(f'{i+1}: {line}' for i,line in enumerate(
        (ROOT/'docs/MODEL.md').read_text().splitlines()[:24]))
    n.require(source['sha256'] == n.pkg.sha(ROOT/'docs/MODEL.md') and
        source['text'] == expected and tool['tool_call_id'] ==
        n.pkg.read(OUT/'project-tool-started.json')['call_id'], 'actual tool source mismatch')
    answer = n.pkg.read(OUT/'project-answer-response.json')['message']['content']
    s.a.validate_answer(answer, {source['citation']}, 1)
    n.require('Qwen4ExpForConditionalGeneration' in answer, 'project answer quality')
    for label, total in (('before-down', 3), ('recovered', 1)):
        s.a.healthy_idle(n.pkg.read(OUT/(label+'-metrics.json'))['metrics'], total)
    rows = []
    for file in sorted((OUT/'receiver').glob('*-receipt.json')):
        row = n.pkg.read(file)
        raw = (file.parent/f"{row['index']:03d}-body.json").read_bytes()
        n.require(receiver.payload(raw) == row['payload'] and
            hashlib.sha256(raw).hexdigest() == row['sha256'] and
            row['index'] == len(rows) and row['authorization_valid'] and
            row['path'] == '/alerts', 'receiver identity or redirect failure')
        rows.append(row)
    n.require(len(rows) <= 64 and len(rows) == n.pkg.read(OUT/'receiver-result.json')['attempts'],
              'receiver count mismatch')
    def selected(name, status, code):
        return [(row, alert) for row in rows for alert in row['payload']['alerts']
                if row['response_status'] == code and alert['status'] == status and
                alert['labels']['alertname'] == name]
    for name in ('Q4TNotifyHeartbeat', 'Q4TTargetDown'):
        failed, good = selected(name, 'firing', 503), selected(name, 'firing', 204)
        n.require(len(failed) == 1 and good and failed[0][0]['monotonic'] < good[0][0]['monotonic'] and
                  failed[0][1]['fingerprint'] == good[0][1]['fingerprint'], 'retry not confirmed')
    probe_failed = selected('Q4TDeliveryProbe', 'firing', 307)
    n.require(len(probe_failed) == 1, 'redirect refusal evidence missing')
    for name in ('Q4TTargetDown', 'Q4TDeliveryProbe'):
        firing, resolved = selected(name, 'firing', 204), selected(name, 'resolved', 204)
        n.require(len(firing) == len(resolved) == 1 and
            firing[0][0]['monotonic'] < resolved[0][0]['monotonic'] and
            firing[0][1]['fingerprint'] == resolved[0][1]['fingerprint'] and
            firing[0][1]['startsAt'] == resolved[0][1]['startsAt'], 'recovery correlation differs')
    down = selected('Q4TTargetDown', 'firing', 204)[0][1]
    n.require(down['labels']['job'] == 'q4t' and
              down['labels']['instance'] == '127.0.0.1:18123', 'synthetic target counted as real')
    n.require(selected('Q4TNotificationFailures', 'firing', 204), 'delivery-failure alert absent')
    dedup = n.pkg.read(OUT/'dedup-window.json')
    n.require(dedup['seconds'] >= 20 and dedup['state_reused'] and
              dedup['firing_acknowledgments'] == 1, 'restart dedup window absent')
    for label in ('alertmanager-1', 'alertmanager-2', 'prometheus-1'):
        terminal = n.pkg.read(OUT/label/'terminal.json')
        n.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
            terminal['starts'] == 1 and terminal['restarts'] == 0 and
            terminal['child_returncode'] == 0 and
            n.pkg.read(OUT/(label+'-claim.json'))['descriptors'], 'monitor lifetime incomplete')
    for label in ('model-1', 'model-2'):
        n.require(n.pkg.read(OUT/(label+'-exit.json'))['returncode'] == 0, 'model did not stop')
    gpu = n.pkg.read(OUT/'gpu-final.json')
    n.require(gpu['status'] == 'OK' and not gpu['pids'], 'GPU cleanup missing')
    grammar = n.pkg.read(OUT/'before-down-metrics.json')['metrics']['q4t_structured_workers_reaped_total']
    return dict(passed=True, source_commit=plan['source_commit'],
        model_starts=2, successful_requests=4, real_tool_executions=1,
        grammar_workers_reaped=grammar, monitor_starts=3, callback_attempts=len(rows),
        callback_confirmed=sum(200 <= r['response_status'] < 300 for r in rows),
        response_codes={str(code):sum(r['response_status'] == code for r in rows) for code in (204, 307, 503)},
        target_down_and_recovery_confirmed=True, synthetic_probe_reported_separately=True,
        restart_dedup_seconds=dedup['seconds'], guard=guard,
        elapsed_seconds=value['elapsed_seconds'], explicit_controls=value['counts']['controls'],
        model_or_http_started=0, external_receiver_accepted=False,
        long_validation_owner='user', performance_acceptance=False)


def private_material():
    paths, keys = set(), []
    for folder in (BASE, *FIXTURES):
        for path in folder.rglob('token-private'):
            paths.add(path.resolve()); keys.append(path.read_bytes().strip())
        for path in folder.rglob('policy-private.json'):
            value = n.pkg.read(path)
            if value.get('receiver_token_file'):
                target = Path(value['receiver_token_file']).resolve()
                paths.add(target)
                if target.exists(): keys.append(target.read_bytes().strip())
    return paths, [v for v in keys if v]


def postflight():
    for file in OUT.glob('*-owner.json'):
        n.require(not n.m.alive(n.pkg.read(file)), 'owned process still active')
    for path in OUT.glob('*/*-claim.json'):  # Kept for compatibility with nested receipts.
        n.require(not n.m.alive(n.pkg.read(path)['child']), 'owned monitor still active')
    for path in OUT.glob('*-claim.json'):
        n.require(not n.m.alive(n.pkg.read(path)['child']), 'owned monitor still active')
    lock = s.a.guard.common_lock()
    n.require(lock.stat().st_ino == 3294067 and s.a.lock_available(lock) and
              not os.path.lexists(lock.with_name(lock.name+'.recovery-required')), 'GPU lease unavailable')
    soar = 'soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask = Path.home()/'.config/systemd/user'/soar
    n.require(mask.is_symlink() and os.readlink(mask) == '/dev/null', 'SOAR mask changed')
    state = subprocess.check_output(['systemctl', '--user', 'show', soar,
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'UnitFileState'], text=True, timeout=10)
    n.require(all(v in state for v in ('MainPID=0', 'ActiveState=inactive', 'UnitFileState=masked')), 'SOAR active')
    containers = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '--format', '{{.ID}} {{.Names}}'],
                                         text=True, timeout=10)
    n.require(not containers.strip(), 'container state changed')
    s.a.require_idle(BASE/'postflight-gpu.json')
    n.require(not (ROOT/'.q4t-work/serve-soak-20261103/day/execution').exists(), 'day started')
    result = subprocess.run([sys.executable, '-B', 'tools/serve_soak/stage.py', 'check', '--profile', 'day'],
                            capture_output=True, text=True, timeout=20)
    n.pkg.save(BASE/'day-check.json', dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
    n.require(result.returncode == 0, 'day source changed')
    return dict(passed=True, all_owned_processes_gone=True, lock_inode=lock.stat().st_ino,
        lock_available=True, soar=state, containers=containers, day_started=False,
        binary_sha256=n.pkg.sha(ROOT/'build/q4t'), sidecar_sha256=n.pkg.sha(ROOT/'build/q4t.release.json'))


def verify_archive():
    h.verify_seal(DEST); manifest = n.pkg.read(DEST/'manifest.json')
    expected = {r['member']:r for r in manifest['records']}; seen = set()
    _, keys = private_material()
    with tarfile.open(DEST/'raw-records.tar.gz') as archive:
        for entry in archive:
            n.require(entry.isfile() and entry.name in expected and entry.name not in seen, 'unexpected member')
            raw = archive.extractfile(entry).read(); row = expected[entry.name]
            n.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'] and
                      not any(key in raw for key in keys), 'archive differs or contains credential')
            h.check_identity(row); seen.add(entry.name)
    n.require(seen == expected.keys(), 'archive record missing')
    for row in manifest['local_artifacts']: h.check_identity(row)
    h.check_identity(manifest['archiver'])
    for name, row in manifest['aliases'].items():
        h.check_identity(row); n.require(n.pkg.sha(DEST/name) == row['sha256'], 'alias differs')
    return dict(check(), archived_records=len(seen), local_artifacts=len(manifest['local_artifacts']))


def archive():
    result = check(); n.require(not DEST.exists(), 'preserve previous archive')
    state = postflight()
    n.pkg.save(BASE/'verified.json', result); n.pkg.save(BASE/'postflight.json', state)
    DEST.mkdir(); records, local = [], []
    private_paths, keys = private_material()
    paths = [(str(p.relative_to(BASE)), p) for p in sorted(BASE.rglob('*')) if p.is_file()]
    for folder in FIXTURES:
        paths.extend(('cpu-fixtures/'+folder.name+'/'+str(p.relative_to(folder)), p)
                     for p in sorted(folder.rglob('*')) if p.is_file())
    with (DEST/'raw-records.tar.gz').open('xb') as out, gzip.GzipFile(fileobj=out, mode='wb', mtime=0) as zipfile:
        with tarfile.open(fileobj=zipfile, mode='w|') as target:
            for name, path in paths:
                if path.is_symlink(): continue  # Deliberate CPU rejection fixtures only.
                private = (path.resolve() in private_paths or path.name in n.BINARIES or
                    path.name in ('key.pem', 'token-private', 'url-private', 'policy-private.json') or
                    path.name.endswith('-private.log') or path.suffix == '.html' or
                    path.name.endswith('.tar.gz') or path.is_relative_to(BASE/'state'))
                if private: local.append(h.identity(path)); continue
                raw = path.read_bytes()
                n.require(not any(key in raw for key in keys) and b'-----BEGIN PRIVATE KEY' not in raw,
                          'credential in proposed archive: '+name)
                entry = tarfile.TarInfo(name); entry.size = len(raw); entry.mode = 0o600; entry.mtime = 0
                target.addfile(entry, io.BytesIO(raw)); records.append(dict(h.identity(path), member=name))
    model = Path(n.pkg.read(s.PLAN)['package']['package'])
    local.extend(h.identity(model/name) for name in ('q4t', 'manifest.json'))
    aliases = {name+'.json':h.identity(path) for name,path in dict(
        plan=s.PLAN, result=OUT/'result.json', verified=BASE/'verified.json', postflight=BASE/'postflight.json').items()}
    for name, row in aliases.items(): h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local, aliases=aliases,
        archiver=h.identity(__file__), external_receiver_accepted=False, long_test_started=False,
        private_scope='Private token/URL/policy/keys/validator logs, binaries and mutable TSDB/notification state remain local by hash.',
        historical_state_limit='First clean-stop nflog has its original hash receipt; the state was then reused and updated.',
        cpu_symlink_fixture='Deliberate symlink is not followed or copied; its unchanged target is retained.'))
    h.seal(DEST); return verify_archive()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'archive', 'verify-archive'))
    args = parser.parse_args()
    print(json.dumps(check() if args.action == 'check' else archive() if args.action == 'archive' else verify_archive()))
