"""Offline reconciliation of the bounded managed v6 lifecycle evidence."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_operations_managed import stage as s

ARCHIVE = ROOT / 'docs/evidence/serve-operations-managed-20261104'


def audit():
    plan = s.check()
    root = s.OUT
    result = s.pkg.read(root / 'result.json')
    s.pkg.require(result['status'] == 'PASS_MANAGED_OPERATIONS_SHORT' and
        result['cleanup_confirmed'] and result['counts'] ==
        dict(starts=1, generation_attempts=4, controls=13), 'driver result')
    first = s.pkg.read(root / 'first-ready.json')
    pressure = s.pkg.read(root / 'saturated-supervisor.json')
    s.pkg.require(s.expected_saturation(pressure, first['updated_unix']) and
                  pressure['backend'] == first['backend'], 'fresh same-backend pressure sample')
    diagnostic = s.pkg.read(root / 'saturated-diagnostic.json')
    s.pkg.require(diagnostic['state'] == 'HEALTHY' and
                  diagnostic['identity']['state'] == 'verified', 'pressure diagnostic')
    for label in ('models', 'capabilities'):
        response = s.pkg.read(root / 'business' / ('saturated-' + label + '-http.json'))
        s.pkg.require(response['status'] == 200, 'TLS discovery status')
    responses = []
    for client, ceiling in (('alpha', 512), ('beta', 64)):
        for part in ('read', 'answer'):
            name = f'project-{client}-{part}'
            stored = s.pkg.read(root / 'business' / (name + '-response.json'))
            seconds = stored.pop('elapsed')
            raw = (root / 'business' / (name + '-wire.bin')).read_bytes()
            parsed = s.client.a.read_sse(io.BytesIO(raw))
            request = s.pkg.read(root / 'business' / (name + '-request.json'))['body']
            s.pkg.require(asdict(parsed) == stored and parsed.id == request['request_id'] and
                parsed.model == request['model'] and request['max_tokens'] <= ceiling and
                parsed.usage['completion_tokens'] <= ceiling, 'wire/request/budget mismatch')
            if part == 'answer':
                tool = json.loads(s.pkg.read(root / 'business' / (f'project-{client}-tool-result.json'))['content'])
                source = tool['source']
                s.pkg.require(tool['ok'] and source['path'] == 'docs/MODEL.md' and
                    source['start_line'] == 1 and source['end_line'] == 24 and
                    source['sha256'] == s.pkg.sha(ROOT / 'docs/MODEL.md'), 'actual tool source')
                s.client.a.validate_answer(parsed.message['content'], {source['citation']}, 1)
                s.pkg.require('Qwen4ExpForConditionalGeneration' in parsed.message['content'], 'answer value')
            responses.append(dict(client=client, part=part, usage=parsed.usage, seconds=seconds))
    frontends = []
    for index in (1, 2):
        ready = s.pkg.read(root / f'front-{index}-ready.json')
        terminal = s.pkg.read(root / f'front-{index}/terminal.json')
        s.pkg.require(ready['control']['binding']['backend'] == first['backend'] and
            ready['control']['binding']['operations_port'] == s.OPS_PORT and
            terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
            not terminal['automatic_reactivation'], 'frontend lifecycle')
        frontends.append(dict(manifest_sha256=plan['gateways'][index-1]['manifest_sha256'],
                              terminal=terminal, binding=ready['control']['binding']))
    for index in (0, 1):
        terminal = s.pkg.read(root / f'rejected-{index}/terminal.json')
        s.pkg.require(terminal['state'] == 'FAILED' and terminal['cleanup_confirmed'] and
            'backend operations port differs' in terminal['error'] and
            not (root / f'rejected-{index}/status.json').exists(), 'rejection before child')
    terminal = s.pkg.read(root / 'managed-run/terminal.json')
    s.pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
        terminal['worker']['attempts'] == 1 and terminal['worker']['restarts'] == 0 and
        terminal['worker']['replayed_requests'] == 0 and
        terminal['guard']['status'] == 'COMPLETED', 'backend terminal')
    metrics = s.pkg.read(root / 'final-metrics.json')
    for name in ('q4t_active_chats', 'q4t_public_connections', 'q4t_business_connections',
                 'q4t_request_body_bytes', 'q4t_structured_workers_active',
                 'q4t_structured_active', 'q4t_structured_preparing'):
        s.pkg.require(metrics[name] == 0, 'resource retained: ' + name)
    s.pkg.require(metrics['q4t_requests_success_total'] == 4 and
        metrics['q4t_requests_error_total'] == metrics['q4t_requests_aborted_total'] == 0 and
        metrics['q4t_structured_workers_started_total'] ==
        metrics['q4t_structured_workers_reaped_total'] == 4 and
        metrics['q4t_seq_slots_free'] == 1, 'request/worker accounting')
    lease = [json.loads(line) for line in (root / 'managed-run/lease/events.jsonl').read_text().splitlines()]
    polls = [r for r in lease if r['event'] == 'monitor']
    s.pkg.require(lease[0]['event'] == 'lease_acquired' and lease[-1]['event'] == 'lease_released' and
        polls and all(not r['conflicts'] and r['unreadable_processes'] == 0 for r in polls), 'guard observations')
    custody = [json.loads(line) for line in (root / 'custody/events.jsonl').read_text().splitlines()]
    s.pkg.require(all(not r['observation']['conflicts'] and
        r['observation']['unreadable_processes'] == 0 for r in custody) and
        s.pkg.read(root / 'custody/exit.json')['status'] == 'COMPLETED', 'custody observations')
    stopped = s.pkg.read(root / 'stopped-inspection.json')
    s.pkg.require(stopped['cleanup_observed'] and stopped['complete'] and
        not stopped['listeners'] and not stopped['gpu']['pids'] and
        stopped['lock']['state'] == 'AVAILABLE' and
        s.pkg.read(s.BASE / 'postflight/result.json')['passed'], 'final resource inspection')
    owners = [dict(role='controller', **s.pkg.read(root/'controller-identity.json')),
              dict(role='worker', **first['owner']), dict(role='backend', **first['backend'])]
    for index in (1, 2):
        ready = s.pkg.read(root / f'front-{index}-ready.json')
        owners += [dict(role=f'front-{index}', **ready['control']['owner']),
                   dict(role=f'nginx-{index}', **ready['state']['observation']['nginx'])]
    for owner in owners:
        s.pkg.require(not s.gateway.alive({k: owner[k] for k in ('pid', 'start_ticks', 'boot_id')}),
                      'owned identity remains')
    return dict(status='ACCEPT_MANAGED_OPERATIONS_SHORT', package=plan['package'],
        source_commit=plan['source_commit'], default_sha256=s.pkg.sha(ROOT/'build/q4t'),
        protocol=plan['protocol'], cpu_checks=5, models=1, successful_responses=4,
        real_tools=2, gateway_starts=2, rejected_gateway_starts=2,
        responses=responses, frontend_lifecycles=frontends,
        public_connections_at_new_supervisor_sample=136, model_identity=first['backend'],
        parent_controls=result['counts']['controls'], supervisor_http_upper_bound=plan['supervisor_http_upper_bound'],
        gateway_binding_http_upper_bound=8,
        production_guard_seconds=lease[-1]['monotonic']-lease[0]['monotonic'],
        monitored_shared_lock_seconds=custody[-1]['monotonic']-lease[0]['monotonic'],
        guard_observations=len(polls), custody_observations=len(custody),
        driver_seconds=result['seconds'], owned_processes=owners,
        cleanup_observed=True, physical_ram_hard_limit_proved=False,
        host_envelope_enabled_in_package=False, persistent_installation=False,
        actual_lan_accepted=False, long_runs_executed=False, default_changed=False,
        protocol_migration_registered=False, performance_acceptance=False)


def original_files():
    return sorted(p for p in s.BASE.rglob('*') if p.is_file())


def secrets():
    plan = s.pkg.read(s.PLAN)
    front, policy = s.pkg.read(plan['frontend']), s.pkg.read(plan['policy'])
    values = {policy['gateway_token'], *(r['api_key'] for r in front['clients'])}
    for path in (s.OUT / 'business').glob('*-request.json'):
        body = s.pkg.read(path).get('body') or {}
        if 'cancel_token' in body:
            values.add(body['cancel_token'])
    return [v.encode() for v in values if v]


def exported(path):
    rel = path.relative_to(s.BASE)
    if rel.parts[0] == 'private' or path.name.endswith('-request.json'):
        return False
    if rel.parts[0].startswith('gateway-'):
        return len(rel.parts) == 2 and path.name == 'manifest.json'
    if 'source' in rel.parts or path.suffix == '.pem':
        return False
    return path.suffix in ('.json', '.jsonl', '.log', '.bin', '.stdout', '.stderr')


def archive():
    result = audit()
    ARCHIVE.mkdir(parents=True)
    values = secrets()
    references = []
    blobs = []
    for path in original_files():
        row = dict(path=str(path), sha256=s.pkg.sha(path), bytes=path.stat().st_size)
        if exported(path) and row['bytes'] <= 2 * 1024**2:
            raw = path.read_bytes()
            if not any(secret in raw for secret in values):
                row['artifact'] = str(path.relative_to(s.BASE))
                blobs.append((row['artifact'], raw))
        references.append(row)
    # Fixed archive metadata; originals retain their original bytes.
    with tarfile.open(ARCHIVE / 'records.tar.gz', 'w:gz') as archive:
        for name, raw in blobs:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o600, 0
            archive.addfile(info, io.BytesIO(raw))
    result.update(archived_records=len(blobs), archived_bytes=sum(len(raw) for _, raw in blobs),
                  local_references=len(references))
    s.pkg.save(ARCHIVE / 'result.json', result)
    s.pkg.save(ARCHIVE / 'manifest.json', dict(records=references,
        archive_sha256=s.pkg.sha(ARCHIVE / 'records.tar.gz'),
        result_sha256=s.pkg.sha(ARCHIVE / 'result.json'),
        verifier=dict(path=str(Path(__file__).resolve()), sha256=s.pkg.sha(__file__)),
        privacy='Private API keys, internal/cancellation tokens and TLS private keys stay local; hash-only references are not backups.'))
    verify()


def verify():
    manifest = s.pkg.read(ARCHIVE / 'manifest.json')
    s.pkg.require({p.name for p in ARCHIVE.iterdir()} == {'manifest.json', 'result.json', 'records.tar.gz'}, 'archive inventory')
    s.pkg.require(s.pkg.sha(manifest['verifier']['path']) == manifest['verifier']['sha256'] and
        s.pkg.sha(ARCHIVE / 'records.tar.gz') == manifest['archive_sha256'] and
        s.pkg.sha(ARCHIVE / 'result.json') == manifest['result_sha256'], 'archive identity')
    values = secrets()
    with tarfile.open(ARCHIVE / 'records.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        expected = {r['artifact'] for r in manifest['records'] if 'artifact' in r}
        s.pkg.require(len(members) == len(expected) and {m.name for m in members} == expected and
            all(m.isfile() and not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
                for m in members), 'archive member safety/inventory')
        for row in manifest['records']:
            s.pkg.require(s.pkg.sha(row['path']) == row['sha256'], 'original changed: ' + row['path'])
            if 'artifact' in row:
                raw = archive.extractfile(row['artifact']).read()
                s.pkg.require(hashlib.sha256(raw).hexdigest() == row['sha256'] and
                    len(raw) == row['bytes'] and not any(secret in raw for secret in values), 'archived original drift/privacy')
    result = s.pkg.read(ARCHIVE / 'result.json')
    rebuilt = audit()
    s.pkg.require(all(result[k] == v for k, v in rebuilt.items()), 'audit reconstruction differs')
    print(json.dumps(dict(verified=True, records=result['archived_records'],
        local_references=result['local_references'],
        monitored_shared_lock_seconds=result['monitored_shared_lock_seconds'])))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('archive', 'verify'))
    globals()[p.parse_args().action]()
