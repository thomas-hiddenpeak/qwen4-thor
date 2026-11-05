"""Reconstruct final delivery, preserving earlier scoped/failed observations."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_governance import completion as c
from tools.serve_cuda_quota_integration.verify import custody

cpu, h, BASE, PACKAGE, OUTPUT = c.cpu, c.h, c.BASE, c.PACKAGE, c.OUTPUT
ARCHIVE = ROOT/'docs/evidence/serve-client-governance-completion-20261105'


def owners():
    result = []
    for path in sorted((OUTPUT/'business').glob('*/identity.json')):
        previous = cpu.read(path)
        try:
            current = custody.proc(previous['pid'])
        except FileNotFoundError:
            current = None
        cpu.require(current is None or current['start_ticks'] != previous['start_ticks'], 'owned child retained')
        result.append(dict(role=path.parent.name, pid=previous['pid'],
                           start_ticks=previous['start_ticks'], gone=True))
    return result


def resources(label):
    folder = BASE/label
    folder.mkdir()
    c.a.require_idle_gpu(folder/'gpu.json')
    lock = c.s.a.guard.common_lock()
    st = lock.stat()
    cpu.require((st.st_dev, st.st_ino) == (66305, 3294067) and c.s.a.lock_available(lock), 'original GPU lock unavailable')
    for port in (c.PORT, c.prior.TLS_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
            probe.listen(1)
    raw = subprocess.check_output(['systemctl', '--user', 'show', c.old_archive.UNIT,
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'], text=True)
    (folder/'soar-unit.txt').write_text(raw)
    state = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    cpu.require(state == dict(MainPID='0', LoadState='masked', ActiveState='inactive', SubState='dead'), 'restore service changed')
    raw = subprocess.check_output(['sudo', '-n', 'docker', 'ps', '--format', '{{.ID}} {{.Names}}'], text=True)
    (folder/'containers.txt').write_text(raw)
    cpu.require(not raw.strip(), 'unexpected running container')
    owned = owners() if label == 'postflight' else []
    if label == 'postflight':
        cpu.require(len(owned) == 1 and cpu.read(OUTPUT/'finalized.json')['status'] == 'DELIVERED', 'final cleanup/delivery')
    cpu.save(folder/'result.json', dict(passed=True, owned=owned, gpu_empty=True,
        ports=[c.PORT, c.prior.TLS_PORT], lock=dict(dev=st.st_dev, inode=st.st_ino, available=True),
        restore_unit_masked=True, containers_empty=True, long_runs=False))


def audit():
    plan = c.check()
    h.verify_seal(OUTPUT)
    previous = c.verify_prior()
    cpu.require(previous == cpu.read(PACKAGE/'scoped-acceptance.json'), 'scoped evidence drift')
    business = c.verify_business()
    result = business['result']
    final = cpu.read(OUTPUT/'finalized.json')
    binding = cpu.read(OUTPUT/'release/binding.json')
    cpu.require(final['status'] == 'DELIVERED' and final['installed'] == binding['new'] and
        binding['old'] == plan['old_hashes'] and final['source_commit'] == plan['source_commit'], 'delivery binding')
    c.a.verify_hashes(ROOT/'build', binding['new'])
    c.a.verify_hashes(PACKAGE/'previous', binding['old'])
    release = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', plan['candidate_sha256'])
    cpu.require(release == cpu.read(OUTPUT/'release/q4t.release.json') and
        release['acceptance'] == 'ACCEPT_BOUNDED_CLIENT_GOVERNANCE' and
        release['acceptance_decision_sha256'] == cpu.sha(OUTPUT/'acceptance.json') and
        release['observed_configuration'] == cpu.read(OUTPUT/'business/legacy-model-config-wire.bin') and
        release['chat_protocol']['protocol_sha256'] == plan['protocol'], 'release metadata')
    cpu.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending recovery')
    health = cpu.read(OUTPUT/'health/result.json')
    cpu.require(health['passed'] and health['new_models'] == health['new_http'] == 0 and
        health['installed']['sha256'] == plan['candidate_sha256'] and
        health['reused_same_elf_http']['sha256'] == cpu.sha(OUTPUT/'business/result.json'), 'installed health identity')
    execution = cpu.read(OUTPUT/'execution.json')
    cpu.require(execution['status'] == 'HEALTH_CHECKED_PENDING_GUARD' and
        execution['finished']-execution['started'] < c.LIMITS['seconds'], 'driver result/time')
    events = [json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    observations = [row for row in events if row['event'] == 'monitor']
    lease = cpu.read(OUTPUT/'lease/exit.json')
    cpu.require(events[0]['event'] == 'lease_acquired' and events[-1]['event'] == 'lease_released' and
        lease['status'] == 'COMPLETED' and lease['child_returncode'] == 0 and observations and
        all(not row['conflicts'] and row['unreadable_processes'] == 0 for row in observations), 'guard')
    seconds = events[-1]['monotonic']-events[0]['monotonic']
    cumulative = seconds+previous['cumulative_lease_seconds']
    cpu.require(0 < seconds < c.LIMITS['seconds'] and cumulative < c.CUMULATIVE['seconds'] and
        result['seconds'] < c.LIMITS['seconds'], 'GPU budget')
    counts = dict(models=previous['models']+result['counts']['starts'],
        generation_attempts=previous['generation_attempts']+result['counts']['generation_attempts'],
        successful_responses=previous['successful_responses']+result['successes'],
        real_tools=previous['real_tools'], cancelled=previous['cancelled'], refusals=previous['refusals'])
    acceptance = cpu.read(OUTPUT/'acceptance.json')
    cpu.require(acceptance['status'] == 'PASS_BOUNDED_CLIENT_GOVERNANCE' and
        all(acceptance[key] == value for key, value in counts.items()) and
        acceptance['business'] == business and acceptance['reused_acceptance'] == plan['reused_acceptance'], 'combined counts')
    cpu.require(counts['models'] == 3 and counts['generation_attempts'] == 18 and
        counts['successful_responses'] == 14 and 28+result['counts']['controls'] <= c.CUMULATIVE['controls'], 'cumulative budget')
    metrics = cpu.read(OUTPUT/'business/final-metrics.json')
    for name in c.s.a.EMPTY:
        cpu.require(metrics[name] == 0, 'retained resource: '+name)
    cpu.require(metrics['q4t_requests_success_total'] == 2 and
        metrics['q4t_requests_aborted_total'] == metrics['q4t_client_policy_enabled'] == 0 and
        metrics['q4t_seq_slots_free'] == metrics['q4t_gpu_healthy'] == 1 and
        not any(key.startswith('q4t_client_rate_') and '{' in key for key in metrics), 'disabled metrics')
    cpu.require(metrics['q4t_prompt_tokens_total'] == sum(row['usage']['prompt_tokens'] for row in business['responses']) and
        metrics['q4t_generation_tokens_total'] == sum(row['usage']['completion_tokens'] for row in business['responses']), 'usage ledger')
    log = (OUTPUT/'business/legacy-model/stderr.log').read_text()
    cpu.require('shutdown complete (0 in-flight remaining)' in log, 'normal drain')
    post = cpu.read(BASE/'postflight/result.json')
    cpu.require(post['passed'] and post['owned'] == owners() and len(post['owned']) == 1, 'postflight')
    gate = cpu.read(c.CPU/'result.json')
    cpu.require(gate['passed'] and gate['models'] == gate['http'] == gate['gpu'] == 0, 'new driver CPU gate')
    return dict(status='ACCEPT_BOUNDED_CLIENT_GOVERNANCE', **counts,
        installed=final['installed'], protocol_sha256=plan['protocol'], source_commit=plan['source_commit'],
        disabled_responses=business['responses'], lease_seconds=seconds, cumulative_lease_seconds=cumulative,
        controls=28+result['counts']['controls'], lease_observations=len(observations), guard_finalized=True,
        scoped_evidence=plan['prior_archive'], prior_failures_preserved=True,
        handler_elapsed_ms=[float(v) for v in re.findall(r'event=end [^\n]* elapsed_ms=([0-9.]+)', log)],
        resources=post, rollback=str(PACKAGE/'previous'), previous_binary_sha256=plan['previous_binary_sha256'],
        cpu_checks=previous['cpu_checks'], new_driver_cases=6,
        numerical_reuse=cpu.read(PACKAGE/'numerical-reuse.json'), declarations_verified=True,
        performance_acceptance=False, long_runs=False, actual_lan_accepted=False,
        managed_lifecycle_accepted=False, physical_ram_hard_limit=False)


def private_values():
    values = set(c.old_archive.private_values())
    for path in cpu.BASE.glob('private*/gateway.json'):
        values.update(row['api_key'].encode() for row in cpu.read(path)['clients'])
    for path in cpu.BASE.glob('private*/clients.json'):
        values.add(cpu.read(path)['gateway_token'].encode())
    for path in cpu.BASE.glob('execution*/business/*-request.json'):
        body = cpu.read(path).get('body') or {}
        if body.get('cancel_token'):
            values.add(body['cancel_token'].encode())
    for path in (OUTPUT/'business').glob('*-request.json'):
        body = cpu.read(path).get('body') or {}
        if body.get('cancel_token'):
            values.add(body['cancel_token'].encode())
    return values


def archive():
    result = audit()
    secrets = private_values()
    records, payload = [], {}
    for path in sorted(BASE.rglob('*')):
        if not path.is_file() or path.name in ('archive.stdout', 'archive.stderr', 'verify.stdout', 'verify.stderr'):
            continue
        relative = path.relative_to(BASE)
        row = dict(path=str(path), sha256=cpu.sha(path), bytes=path.stat().st_size)
        eligible = (not path.name.endswith('-request.json') and
            path.suffix in ('.json', '.jsonl', '.stdout', '.stderr', '.log', '.txt', '.bin', '.yml', '.h') and row['bytes'] <= 2*1024**2)
        if eligible:
            raw = path.read_bytes()
            if not any(secret in raw for secret in secrets):
                row['artifact'] = 'records/'+str(relative)
                payload[row['artifact']] = raw
        records.append(row)
    rawtar = io.BytesIO()
    with tarfile.open(fileobj=rawtar, mode='w') as tar:
        for name, raw in sorted(payload.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o600
            tar.addfile(info, io.BytesIO(raw))
    zipped = gzip.compress(rawtar.getvalue(), mtime=0)
    ARCHIVE.mkdir(parents=True)
    (ARCHIVE/'records.tar.gz').write_bytes(zipped)
    result.update(archived_records=len(payload), local_references=len(records),
        privacy='All prior/current credentials screened. Secret requests and ELF remain local references; public archive is not a recovery backup.')
    cpu.save(ARCHIVE/'result.json', result)
    cpu.save(ARCHIVE/'manifest.json', dict(version=1, records=records,
        result_sha256=cpu.sha(ARCHIVE/'result.json'), archive_sha256=cpu.sha(ARCHIVE/'records.tar.gz'),
        verifier=h.identity(Path(__file__).resolve())))
    verify()


def verify():
    manifest = cpu.read(ARCHIVE/'manifest.json')
    cpu.require(cpu.sha(__file__) == manifest['verifier']['sha256'] and
        cpu.sha(ARCHIVE/'result.json') == manifest['result_sha256'] and
        cpu.sha(ARCHIVE/'records.tar.gz') == manifest['archive_sha256'], 'archive/verifier drift')
    with tarfile.open(ARCHIVE/'records.tar.gz', 'r:gz') as tar:
        members = tar.getmembers()
        cpu.require(all(row.isfile() for row in members) and len({row.name for row in members}) == len(members), 'archive entries')
        payload = {row.name: tar.extractfile(row).read() for row in members}
    cpu.require(set(payload) == {row['artifact'] for row in manifest['records'] if 'artifact' in row}, 'inventory')
    secrets = private_values()
    for row in manifest['records']:
        cpu.require(cpu.sha(row['path']) == row['sha256'], 'local reference changed: '+row['path'])
        if 'artifact' in row:
            raw = payload[row['artifact']]
            cpu.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'] and
                not any(secret in raw for secret in secrets), 'archived bytes/privacy')
    result = cpu.read(ARCHIVE/'result.json')
    cpu.require(all(result[key] == value for key, value in audit().items()), 'reconstructed result differs')
    print(json.dumps(dict(verified=True, records=result['archived_records'], references=result['local_references'],
        installed=result['installed'], cumulative_lease_seconds=result['cumulative_lease_seconds'],
        archive_sha256=manifest['archive_sha256'])))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'postflight', 'audit', 'archive', 'verify'))
    action = parser.parse_args().action
    if action in ('preflight', 'postflight'):
        resources(action)
    elif action == 'audit':
        print(json.dumps(audit(), indent=2))
    else:
        globals()[action]()
