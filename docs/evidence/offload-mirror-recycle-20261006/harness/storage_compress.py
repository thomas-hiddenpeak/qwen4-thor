#!/usr/bin/env python3
"""Final, bounded lossless gzip rescue; prepare and execute are separate."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
import storage_dedup as base

R, Q = base.R, base.Q
TOTAL_READ_CAP = 512 * 1024 * 1024
read_bytes = 0


def digest(path):
    global read_bytes
    assert path.stat().st_size <= 16 * 1024 * 1024, path
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            read_bytes += len(block)
            assert read_bytes <= TOTAL_READ_CAP
            h.update(block)
    return h.hexdigest()


def json_read(path):
    assert path.stat().st_size <= 8 * 1024 * 1024
    return json.loads(path.read_text())


def collect_bindings(value, result):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('source_sha256', 'frozen_files', 'metadata_source_sha256'):
                assert isinstance(child, dict)
                for name, sha in child.items():
                    # Historical failure/static receipts may retain an earlier
                    # SHA. Any such binding still excludes the original path.
                    result[name] = sha
            collect_bindings(child, result)
    elif isinstance(value, list):
        for child in value:
            collect_bindings(child, result)


def ledgers():
    # Parse only this round's compact metadata. Referenced old raw files are
    # never opened; their already-frozen SHA names still prohibit selection.
    paths = sorted(p for p in R.glob('*.json')
                   if not p.name.startswith('storage-compression'))
    assert len(paths) <= 250
    bound, records = {}, {}
    for path in paths:
        value = json_read(path)
        collect_bindings(value, bound)
        records[str(path)] = digest(path)
    return bound, records


def guard(policy):
    assert digest(R / 'execution-plan.json') == policy['plan_sha256']
    plan = json_read(R / 'execution-plan.json')
    assert policy['groups'] == plan['group_ids'][:16]
    assert policy['groups'][-1] == 'm061-261887-on'
    assert policy['minimum_free_bytes'] == 1073741824
    for name, sha in policy['source_sha256'].items():
        assert digest(Path(name)) == sha, name
    receipts = base.interval_guard(plan, policy)
    for group in policy['groups']:
        decision = R / (group + '-decision.json')
        value = json_read(decision)
        assert value['passed'] is True and value['group_id'] == group
        assert value['plan_sha256'] == policy['plan_sha256']
        for key in ('runtime_source_commit', 'runtime_binary_sha256'):
            assert value[key] == plan[key]
        assert value['ended_t'] <= value['recorded_t'] <= time.time()
        audit = R / (group + '-audit-exit.json')
        result = json_read(audit)
        assert result['returncode'] == 0 and result['cleanup_complete'] is True
        assert result['ended_t'] <= time.time()
        receipts[str(decision)], receipts[str(audit)] = digest(decision), digest(audit)
    previous = R / 'storage-dedup/summary.json'
    record = json_read(previous)
    assert record['passed'] is True
    assert record['original_ledgers_unchanged'] is True
    assert record['all_evidence_paths_and_byte_sha_preserved'] is True
    assert record['plan_sha256'] == policy['plan_sha256']
    assert shutil.disk_usage(R).free < policy['minimum_free_bytes']
    receipts[str(previous)] = digest(previous)
    return plan, receipts


def permitted(path, policy):
    assert path.is_absolute() and path.resolve() == path
    rel = path.relative_to(Q)
    group, *parts = rel.parts
    assert group in policy['groups'] and parts[0] == 'http'
    if path.name in ('benchmark_data.db', 'perf_report.html'):
        return True
    assert group in policy['generated_request_groups']
    assert (path.name == 'requests.jsonl' or
            (len(parts) == 3 and parts[:2] == ['http', 'inputs'] and
             path.suffix == '.jsonl'))
    return True


def inventory(policy, bound):
    found = set()
    for group in policy['groups']:
        http = Q / group / 'http'
        found.update(http.rglob('benchmark_data.db'))
        found.update(http.rglob('perf_report.html'))
        if group in policy['generated_request_groups']:
            found.update((http / 'inputs').glob('*.jsonl'))
            found.update(http.rglob('requests.jsonl'))
    assert 0 < len(found) <= policy['max_candidates']
    entries = []
    for path in sorted(found):
        permitted(path, policy)
        state = base.snapshot(path)
        assert state['nlink'] == 1 and str(path) not in bound, path
        assert 0 < state['bytes'] <= policy['single_candidate_byte_cap']
        entries.append(dict(path=str(path), before=state))
    assert sum(e['before']['bytes'] for e in entries) <= policy['total_original_byte_cap']
    return entries


def prepare(policy, policy_sha):
    plan, receipts = guard(policy)
    bound, records = ledgers()
    entries = inventory(policy, bound)
    result = dict(schema=1, prepared_t=time.time(), preparation_only=True,
        policy_sha256=policy_sha, plan_sha256=policy['plan_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        script_sha256=digest(Path(__file__)), candidates=entries,
        ledger_sha256=records, guard_receipts_sha256=receipts,
        candidate_payload_bytes_read=0,
        allocated_bytes=sum(e['before']['allocated_bytes'] for e in entries))
    path = R / 'storage-compression-plan.json'
    base.save(path, result)
    base.sync_dir(R)
    print(json.dumps(dict(prepared=str(path), manifest_sha256=digest(path),
                         candidate_count=len(entries), allocated_bytes=result['allocated_bytes'])))


def persist_map(out, mapping):
    pending = out / 'compression.json.pending'
    base.save(pending, mapping)
    os.replace(pending, out / 'compression.json')
    base.sync_dir(out)


def execute(policy, policy_sha, manifest_sha):
    global read_bytes
    manifest_path = R / 'storage-compression-plan.json'
    assert manifest_sha and digest(manifest_path) == manifest_sha
    manifest = json_read(manifest_path)
    assert manifest['policy_sha256'] == policy_sha
    assert manifest['script_sha256'] == digest(Path(__file__))
    plan, receipts = guard(policy)
    assert manifest['guard_receipts_sha256'] == receipts
    for name, sha in manifest['ledger_sha256'].items():
        assert digest(Path(name)) == sha, name
    bound, _ = ledgers()
    assert inventory(policy, bound) == manifest['candidates']
    out = R / 'storage-compression'
    out.mkdir(exist_ok=False)
    (out / 'payload').mkdir()
    # The new archive root itself must survive a crash before an original's
    # directory entry can be removed durably; syncing only its children is not
    # sufficient to persist its entry in R.
    base.sync_dir(out)
    base.sync_dir(R)
    before_free = shutil.disk_usage(R).free
    preflight = []
    for entry in manifest['candidates']:
        path = Path(entry['path'])
        original = digest(path)
        assert base.snapshot(path) == entry['before']
        preflight.append(dict(entry, original_sha256=original))
    base.save(out / 'preflight.json', dict(started_t=time.time(),
        manifest_sha256=manifest_sha, policy_sha256=policy_sha,
        entries=preflight, free_before=before_free,
        ledger_sha256=manifest['ledger_sha256']))
    mapping = dict(schema=1, plan_sha256=policy['plan_sha256'],
        manifest_sha256=manifest_sha, files=[],
        restoration='Stream gzip to a new independent temporary file at original_path; '
        'verify original_sha256 and original_bytes, fsync, then atomically replace. '
        'Never interpret a gzip stream as the original file. Restoring needs free space.',
        metadata_limit='Original inode/mtime/mode are not promised by compression.')
    persist_map(out, mapping)
    preflight.sort(key=lambda e: (-e['before']['allocated_bytes'], e['path']))
    with (out / 'journal.jsonl').open('x') as journal:
        def record(value):
            journal.write(json.dumps(dict(t=time.time(), **value)) + '\n')
            journal.flush()
            os.fsync(journal.fileno())

        for index, entry in enumerate(preflight):
            if shutil.disk_usage(R).free >= policy['minimum_free_bytes']:
                break
            # Recheck the service interval before each mutation. This never
            # stops a service or changes the frozen startup gate.
            base.interval_guard(plan, policy)
            original = Path(entry['path'])
            assert str(original) not in bound and base.snapshot(original) == entry['before']
            compressed = out / 'payload' / f'{index:04d}.gz'
            record(dict(action='compress', original_path=str(original),
                        original_sha256=entry['original_sha256'],
                        original_bytes=entry['before']['bytes'], compressed_path=str(compressed)))
            with original.open('rb') as source, compressed.open('xb') as target:
                with gzip.GzipFile(filename='', mode='wb', fileobj=target,
                                   compresslevel=1, mtime=0) as zipped:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        read_bytes += len(block)
                        assert read_bytes <= TOTAL_READ_CAP
                        zipped.write(block)
                target.flush()
                os.fsync(target.fileno())
            assert base.snapshot(original) == entry['before']
            h, length = hashlib.sha256(), 0
            with gzip.open(compressed, 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    length += len(block)
                    read_bytes += len(block)
                    assert length <= entry['before']['bytes'] and read_bytes <= TOTAL_READ_CAP
                    h.update(block)
            assert length == entry['before']['bytes'] and h.hexdigest() == entry['original_sha256']
            os.chmod(compressed, 0o444)
            base.sync_dir(compressed.parent)
            item = dict(original_path=str(original), original_sha256=entry['original_sha256'],
                original_bytes=length, original_metadata=entry['before'],
                compressed_path=str(compressed), compressed_sha256=digest(compressed),
                compressed_bytes=compressed.stat().st_size,
                compressed_allocated_bytes=compressed.stat().st_blocks * 512,
                gzip_level=1, roundtrip_verified=True, state='verified_original_present')
            mapping['files'].append(item)
            # A durable restoration mapping is committed BEFORE source unlink.
            persist_map(out, mapping)
            if item['compressed_allocated_bytes'] >= entry['before']['allocated_bytes']:
                item['state'] = 'original_retained_no_allocated_saving'
                persist_map(out, mapping)
                record(dict(action='retained_no_saving', original_path=str(original)))
                continue
            assert base.snapshot(original) == entry['before']
            record(dict(action='unlink_original_after_verified_durable_map',
                        original_path=str(original), compressed_sha256=item['compressed_sha256']))
            item['free_before_unlink'] = shutil.disk_usage(R).free
            original.unlink()
            base.sync_dir(original.parent)
            item['state'] = 'original_removed'
            item['free_after_unlink'] = shutil.disk_usage(R).free
            persist_map(out, mapping)
            record(dict(action='original_removed', original_path=str(original),
                        free_after=item['free_after_unlink']))
    for name, sha in manifest['ledger_sha256'].items():
        assert digest(Path(name)) == sha, name
    for name, sha in receipts.items():
        assert digest(Path(name)) == sha, name
    free_after = shutil.disk_usage(R).free
    result = dict(passed=True, storage_only=True, ended_t=time.time(),
        plan_sha256=policy['plan_sha256'], policy_sha256=policy_sha,
        manifest_sha256=manifest_sha, runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        compressed_count=len(mapping['files']),
        originals_removed=sum(e['state'] == 'original_removed' for e in mapping['files']),
        candidate_count=len(preflight), unprocessed_count=len(preflight)-len(mapping['files']),
        removed_original_allocated_bytes=sum(e['original_metadata']['allocated_bytes']
            for e in mapping['files'] if e['state'] == 'original_removed'),
        compressed_allocated_bytes_total=sum(e['compressed_allocated_bytes']
            for e in mapping['files']),
        net_payload_allocated_bytes_freed=sum(
            (e['original_metadata']['allocated_bytes'] if e['state'] == 'original_removed' else 0)
            - e['compressed_allocated_bytes'] for e in mapping['files']),
        free_before=before_free, free_after=free_after,
        minimum_free_bytes=policy['minimum_free_bytes'],
        startup_space_gate_met=free_after >= policy['minimum_free_bytes'],
        original_ledgers_unchanged=True, all_removed_originals_roundtrip_verified=True,
        compression_map_sha256=digest(out / 'compression.json'),
        content_read_bytes=read_bytes, original_paths_require_restoration=True,
        tests_or_model_run=False, automatic_retry=False,
        limitation='Storage rescue only; no runtime, resource, quality, or performance acceptance.')
    base.save(out / 'summary.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'execute'])
    parser.add_argument('--policy-sha256', required=True)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        path = R / 'storage-compression-policy.json'
        assert digest(path) == args.policy_sha256
        policy = json_read(path)
        if args.mode == 'prepare':
            assert args.manifest_sha256 is None
            prepare(policy, args.policy_sha256)
        else:
            execute(policy, args.policy_sha256, args.manifest_sha256)
    except Exception as exc:
        failure = R / 'storage-compression-first-failure.json'
        if not failure.exists():
            base.save(failure, dict(passed=False, mode=args.mode, ended_t=time.time(),
                error=repr(exc), automatic_retry=False,
                note='Preserve journal, map and all gzip files; inspect interrupted state. '
                'Do not retry, enlarge scope, discard evidence, or lower the gate.'))
        raise
