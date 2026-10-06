#!/usr/bin/env python3
"""Repair only the observed storage-reserve defect, with no additional HTTP."""
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
import storage_compress as prior

base, R, Q = prior.base, prior.R, prior.Q
digest, read = prior.digest, prior.json_read
POLICY = R / 'storage-reserve-repair-policy.json'
PLAN = R / 'storage-reserve-repair-plan.json'
OUT = R / 'storage-reserve-repair'


def protect_prior(policy):
    for name, sha in policy['protected_metadata_sha256'].items():
        assert digest(Path(name)) == sha, name
    for item in policy['old_compressed_files']:
        assert base.snapshot(Path(item['path'])) == item['before']
    # Prior gzip bytes are not reread: retained map SHA and unchanged metadata
    # carry their existing proof. This tool never writes/removes those paths.


def guard(policy):
    assert policy['minimum_free_bytes'] == 1073741824
    assert policy['storage_reserve_bytes'] == 33554432
    assert policy['storage_target_free_bytes'] == 1107296256
    assert digest(R / 'execution-plan.json') == policy['plan_sha256']
    plan = read(R / 'execution-plan.json')
    assert plan['min_free_disk_bytes'] == policy['minimum_free_bytes']
    assert policy['groups'] == plan['group_ids'][:16]
    assert policy['groups'][-1] == 'm061-261887-on'
    for name, sha in policy['source_sha256'].items():
        assert digest(Path(name)) == sha, name
    protect_prior(policy)
    failure = read(R / 'm062-preflight-first-failure.json')
    assert failure['phase'] == 'preflight_before_controller'
    assert failure['service_started'] is False
    assert failure['HTTP_requests_sent'] == 0
    assert failure['controller_start_exists'] is False
    assert not (Q / 'm062-261887-off').exists()
    receipts = base.interval_guard(plan, policy)
    for group in policy['groups']:
        decision, audit = R / (group + '-decision.json'), R / (group + '-audit-exit.json')
        result = read(decision)
        assert result['passed'] is True and result['group_id'] == group
        assert result['plan_sha256'] == policy['plan_sha256']
        assert result['ended_t'] <= result['recorded_t'] <= time.time()
        for key in ('runtime_source_commit', 'runtime_binary_sha256'):
            assert result[key] == plan[key]
        done = read(audit)
        assert done['returncode'] == 0 and done['cleanup_complete'] is True
        assert done['ended_t'] <= time.time()
        receipts[str(decision)], receipts[str(audit)] = digest(decision), digest(audit)
    old = read(R / 'storage-compression/summary.json')
    assert old['passed'] is True and old['original_ledgers_unchanged'] is True
    assert old['all_removed_originals_roundtrip_verified'] is True
    assert old['candidate_count'] == 246 and old['unprocessed_count'] == 245
    assert old['compressed_count'] == old['originals_removed'] == 1
    assert old['plan_sha256'] == policy['plan_sha256']
    assert shutil.disk_usage(R).free < policy['storage_target_free_bytes']
    return plan, receipts


def remaining(policy, bound):
    original = read(R / 'storage-compression-plan.json')
    mapping = read(R / 'storage-compression/compression.json')
    processed = {item['original_path'] for item in mapping['files']}
    assert len(processed) == 1
    assert sorted(processed) == policy['excluded_processed_originals']
    old_entries = {item['path']: item for item in original['candidates']}
    expected = set(old_entries) - processed
    entries = policy['remaining_candidates']
    assert len(old_entries) == 246 and len(entries) == len(expected) == 245
    assert {item['path'] for item in entries} == expected
    for item in entries:
        path = Path(item['path'])
        prior.permitted(path, policy)
        assert str(path) not in bound and base.snapshot(path) == item['before']
        assert item['before'] == old_entries[str(path)]['before']
        assert item['before']['nlink'] == 1
        assert 0 < item['before']['bytes'] <= policy['single_candidate_byte_cap']
    assert sum(e['before']['bytes'] for e in entries) == policy['remaining_original_bytes']
    return entries


def prepare(policy, policy_sha):
    plan, receipts = guard(policy)
    bound, ledgers = prior.ledgers()
    entries = remaining(policy, bound)
    manifest = dict(schema=1, prepared_t=time.time(), preparation_only=True,
        policy_sha256=policy_sha, plan_sha256=policy['plan_sha256'],
        script_sha256=digest(Path(__file__)), candidates=entries,
        guard_receipts_sha256=receipts, ledger_sha256=ledgers,
        runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        minimum_free_bytes=policy['minimum_free_bytes'],
        storage_target_free_bytes=policy['storage_target_free_bytes'],
        candidate_payload_reads=0, extra_HTTP_requests=0)
    base.save(PLAN, manifest)
    base.sync_dir(R)
    print(json.dumps(dict(prepared=str(PLAN), manifest_sha256=digest(PLAN),
                         remaining_count=len(entries))))


def execute(policy, policy_sha, manifest_sha):
    assert manifest_sha and digest(PLAN) == manifest_sha
    manifest = read(PLAN)
    assert manifest['policy_sha256'] == policy_sha
    assert manifest['script_sha256'] == digest(Path(__file__))
    plan, receipts = guard(policy)
    assert manifest['guard_receipts_sha256'] == receipts
    for name, sha in manifest['ledger_sha256'].items():
        assert digest(Path(name)) == sha, name
    bound, _ = prior.ledgers()
    assert remaining(policy, bound) == manifest['candidates']
    OUT.mkdir(exist_ok=False)
    (OUT / 'payload').mkdir()
    base.sync_dir(OUT)
    base.sync_dir(R)
    before_free = shutil.disk_usage(R).free
    entries = []
    for item in manifest['candidates']:
        path = Path(item['path'])
        original_sha = digest(path)
        assert original_sha == item['recorded_original_sha256']
        assert base.snapshot(path) == item['before']
        entries.append(dict(item, original_sha256=original_sha))
    base.save(OUT / 'preflight.json', dict(started_t=time.time(),
        manifest_sha256=manifest_sha, policy_sha256=policy_sha, entries=entries,
        free_before=before_free, ledger_sha256=manifest['ledger_sha256'],
        protected_prior_metadata_sha256=policy['protected_metadata_sha256']))
    mapping = dict(schema=1, plan_sha256=policy['plan_sha256'], manifest_sha256=manifest_sha,
        files=[], prior_map=str(R / 'storage-compression/compression.json'),
        restoration='Stream gzip to an independent temporary file at original_path; '
        'verify original_sha256 and original_bytes, fsync and atomically replace. '
        'Restoration needs free space; gzip is not the original file format.',
        metadata_limit='No guarantee of original inode/mtime/mode preservation.')
    prior.persist_map(OUT, mapping)
    entries.sort(key=lambda item: (-item['before']['allocated_bytes'], item['path']))
    with (OUT / 'journal.jsonl').open('x') as journal:
        def record(value):
            journal.write(json.dumps(dict(t=time.time(), **value)) + '\n')
            journal.flush()
            os.fsync(journal.fileno())

        for index, item in enumerate(entries):
            if shutil.disk_usage(R).free >= policy['storage_target_free_bytes']:
                break
            base.interval_guard(plan, policy)
            original = Path(item['path'])
            assert str(original) not in bound and base.snapshot(original) == item['before']
            zipped = OUT / 'payload' / f'{index:04d}.gz'
            record(dict(action='compress', original_path=str(original),
                original_sha256=item['original_sha256'], original_bytes=item['before']['bytes'],
                compressed_path=str(zipped)))
            with original.open('rb') as source, zipped.open('xb') as destination:
                with gzip.GzipFile(filename='', mode='wb', fileobj=destination,
                                   compresslevel=1, mtime=0) as compressor:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        prior.read_bytes += len(block)
                        assert prior.read_bytes <= policy['hash_decompress_byte_cap']
                        compressor.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            assert base.snapshot(original) == item['before']
            h, size = hashlib.sha256(), 0
            with gzip.open(zipped, 'rb') as restored:
                for block in iter(lambda: restored.read(1024 * 1024), b''):
                    size += len(block)
                    prior.read_bytes += len(block)
                    assert size <= item['before']['bytes']
                    assert prior.read_bytes <= policy['hash_decompress_byte_cap']
                    h.update(block)
            assert size == item['before']['bytes'] and h.hexdigest() == item['original_sha256']
            os.chmod(zipped, 0o444)
            base.sync_dir(zipped.parent)
            saved = dict(original_path=str(original), original_sha256=item['original_sha256'],
                original_bytes=size, original_metadata=item['before'],
                compressed_path=str(zipped), compressed_sha256=digest(zipped),
                compressed_bytes=zipped.stat().st_size,
                compressed_allocated_bytes=zipped.stat().st_blocks * 512,
                gzip_level=1, roundtrip_verified=True, state='verified_original_present')
            mapping['files'].append(saved)
            prior.persist_map(OUT, mapping)
            if saved['compressed_allocated_bytes'] >= item['before']['allocated_bytes']:
                saved['state'] = 'original_retained_no_allocated_saving'
                prior.persist_map(OUT, mapping)
                record(dict(action='retained_no_saving', original_path=str(original)))
                continue
            assert base.snapshot(original) == item['before']
            record(dict(action='unlink_original_after_verified_durable_map',
                        original_path=str(original), compressed_sha256=saved['compressed_sha256']))
            saved['free_before_unlink'] = shutil.disk_usage(R).free
            original.unlink()
            base.sync_dir(original.parent)
            saved['state'] = 'original_removed'
            saved['free_after_unlink'] = shutil.disk_usage(R).free
            prior.persist_map(OUT, mapping)
            record(dict(action='original_removed', original_path=str(original),
                        free_after=saved['free_after_unlink']))
    protect_prior(policy)
    for name, sha in {**manifest['ledger_sha256'], **receipts}.items():
        assert digest(Path(name)) == sha, name
    after_free = shutil.disk_usage(R).free
    summary = dict(passed=True, storage_only=True, concrete_failure_repair=True,
        ended_t=time.time(), policy_sha256=policy_sha, manifest_sha256=manifest_sha,
        plan_sha256=policy['plan_sha256'], runtime_source_commit=plan['runtime_source_commit'],
        runtime_binary_sha256=plan['runtime_binary_sha256'], candidate_count=len(entries),
        compressed_count=len(mapping['files']), unprocessed_count=len(entries)-len(mapping['files']),
        originals_removed=sum(e['state'] == 'original_removed' for e in mapping['files']),
        net_payload_allocated_bytes_freed=sum(
            (e['original_metadata']['allocated_bytes'] if e['state'] == 'original_removed' else 0)
            - e['compressed_allocated_bytes'] for e in mapping['files']),
        free_before=before_free, free_after=after_free,
        minimum_free_bytes=policy['minimum_free_bytes'],
        storage_reserve_bytes=policy['storage_reserve_bytes'],
        storage_target_free_bytes=policy['storage_target_free_bytes'],
        startup_space_gate_met=after_free >= policy['minimum_free_bytes'],
        storage_target_met=after_free >= policy['storage_target_free_bytes'],
        original_ledgers_unchanged=True, prior_storage_evidence_unchanged=True,
        all_removed_originals_roundtrip_verified=True,
        compression_map_sha256=digest(OUT / 'compression.json'),
        content_read_bytes=prior.read_bytes, original_paths_require_restoration=True,
        tests_or_model_run=False, extra_HTTP_requests=0, frozen_runner_changed=False,
        automatic_retry=False, limitation='Repairs storage reserve only; no acceptance result changes.')
    base.save(OUT / 'summary.json', summary)
    print(json.dumps(summary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'execute'])
    parser.add_argument('--policy-sha256', required=True)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        assert digest(POLICY) == args.policy_sha256
        policy = read(POLICY)
        if args.mode == 'prepare':
            assert args.manifest_sha256 is None
            prepare(policy, args.policy_sha256)
        else:
            execute(policy, args.policy_sha256, args.manifest_sha256)
    except Exception as exc:
        failure = R / 'storage-reserve-repair-first-failure.json'
        if not failure.exists():
            base.save(failure, dict(passed=False, mode=args.mode, ended_t=time.time(),
                error=repr(exc), automatic_retry=False,
                note='Retain prior evidence and new map/journal/payload. '
                'No automatic retry, scope expansion, or runner gate changes.'))
        raise
