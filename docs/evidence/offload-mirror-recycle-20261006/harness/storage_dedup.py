#!/usr/bin/env python3
"""One bounded, between-service evidence dedup; no model/test execution."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
Q = R / 'source/.q4t-work/evidence'
MAX_FILE = 8 * 1024 * 1024
READ_CAP = 512 * 1024 * 1024
read_bytes = 0


def sha(path):
    global read_bytes
    assert path.stat().st_size <= MAX_FILE, path
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            read_bytes += len(block)
            assert read_bytes <= READ_CAP, 'storage read cap'
            h.update(block)
    return h.hexdigest()


def read(path):
    assert path.stat().st_size <= MAX_FILE, path
    return json.loads(path.read_text())


def snapshot(path):
    s = path.lstat()
    assert stat.S_ISREG(s.st_mode) and path.resolve() == path, path
    return dict(device=s.st_dev, inode=s.st_ino, bytes=s.st_size,
                allocated_bytes=s.st_blocks * 512, mtime_ns=s.st_mtime_ns,
                mode=stat.S_IMODE(s.st_mode), nlink=s.st_nlink)


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def interval_guard(plan, scope):
    # Explicitly require the current service to finish; never wait or stop it.
    assert (R / (scope['execute_only_after'] + '-stage.json')).is_file()
    assert not (R / (scope['must_not_be_started'] +
                     '-controller-start.json')).exists()
    receipts = {}
    for group in plan['group_ids']:
        start = R / (group + '-controller-start.json')
        if not start.exists():
            continue
        for suffix in ('-stage.json', '-controller-exit.json'):
            path = R / (group + suffix)
            value = read(path)
            assert value['returncode'] == 0
            assert value['cleanup_complete'] is True
            assert value['ended_t'] <= time.time()
            assert value['group_after_cleanup']['absent'] is True
            receipts[str(path)] = sha(path)
        cleanup = read(Q / group / 'http/isolation/cleanup.json')
        assert cleanup['unit_removed'] is True
        identity = read(Q / group / 'http/isolation/identity.json')
        cg = identity['properties']['ControlGroup']
        unit = cg.rsplit('/', 1)[-1]
        assert unit.startswith('q4t-ram-') and unit.endswith('.service')
        result = subprocess.run(['systemctl', 'show', unit,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID'],
            capture_output=True, text=True, timeout=10)
        assert result.returncode in (0, 1), result.stderr
        props = dict(line.split('=', 1) for line in result.stdout.splitlines()
                     if '=' in line)
        assert props.get('ActiveState') in ('inactive', 'failed'), props
        assert props.get('MainPID') == '0', props
    return receipts


def bindings(scope):
    result = {}
    paths = {Path(p) for p in scope['ledger_files']}
    paths.update(R.glob('*-decision.json'))
    for path in sorted(paths):
        value = read(path)
        for key in ('source_sha256', 'frozen_files'):
            for name, digest in value.get(key, {}).items():
                assert name not in result or result[name] == digest, name
                result[name] = digest
    return result


def allowed(entry, scope, rebuildable=False):
    path = Path(entry['path'])
    assert path.is_absolute() and path.resolve() == path
    if rebuildable:
        assert path.is_relative_to(R / 'candidate-build')
        if entry['kind'] == 'configure_probe_binary':
            assert path.is_relative_to(R / 'candidate-build/CMakeFiles/4.4.3')
            assert path.suffix in ('.bin', '.cubin', '.fatbin') or path.name == 'a.out'
        else:
            assert path.name.endswith(('.o', '.o.d')) or path.suffix == '.a'
    else:
        relative = path.relative_to(Q)
        assert relative.parts[0] in scope['groups']
        sub = relative.parts[1:]
        assert ((len(sub) == 2 and sub[0] == 'tools' and path.suffix == '.py') or
                (sub[0] == 'http' and path.name == 'requests.jsonl') or
                (len(sub) == 3 and sub[:2] == ('http', 'inputs') and
                 path.suffix == '.jsonl')), path
    return path


def execute(args):
    global read_bytes
    scope_path = R / 'storage-dedup-plan.json'
    assert sha(scope_path) == args.scope_sha256
    scope = read(scope_path)
    assert scope['minimum_free_bytes'] == 1073741824
    assert sha(R / 'execution-plan.json') == scope['plan_sha256']
    plan = read(R / 'execution-plan.json')
    assert scope['groups'] == plan['group_ids'][:14]
    assert scope['groups'][-1] == 'm051-204800-off'
    assert 0 < len(scope['share_candidates']) <= 500
    assert 0 < len(scope['optional_rebuildable_candidates']) <= 250
    receipts = interval_guard(plan, scope)
    for path, record in scope['ledger_files'].items():
        assert sha(Path(path)) == record['sha256'], path
    bound = bindings(scope)
    before_free = shutil.disk_usage(R).free
    out = R / 'storage-dedup'
    out.mkdir(exist_ok=False)
    save(out / 'operation.json', dict(started_t=time.time(),
         script_sha256=sha(Path(__file__)), scope_sha256=args.scope_sha256,
         interval_receipts=receipts, free_before=before_free,
         prune_rebuildable_if_needed=args.prune_rebuildable_if_needed))
    entries, groups = [], defaultdict(list)
    # All selected evidence bytes and all fixed ledgers are checked before any
    # sharing. SHA equality is then also checked with direct byte comparison.
    for entry in scope['share_candidates']:
        path = allowed(entry, scope)
        before = snapshot(path)
        assert before == entry['before'] and before['nlink'] == 1, path
        digest = sha(path)
        assert snapshot(path) == before, path
        if str(path) in bound:
            assert digest == bound[str(path)], path
        item = dict(entry, original_sha256=digest)
        entries.append(item)
        groups[(before['device'], before['bytes'], digest,
                before['mode'] & ~0o222)].append(item)
    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = Path(group[0]['path'])
        for entry in group[1:]:
            with canonical.open('rb') as a, Path(entry['path']).open('rb') as b:
                while True:
                    x, y = a.read(1024 * 1024), b.read(1024 * 1024)
                    read_bytes += len(x) + len(y)
                    assert read_bytes <= READ_CAP
                    assert x == y, entry['path']
                    if not x:
                        break
    save(out / 'preflight.json', dict(entries=entries, free_before=before_free,
         ledger_sha256=scope['ledger_files'],
         distinct_content_groups=len(groups)))
    interval_guard(plan, scope)
    released, replacements, removed = 0, 0, []
    with (out / 'journal.jsonl').open('x') as journal:
        def record(value):
            journal.write(json.dumps(dict(t=time.time(), **value)) + '\n')
            journal.flush()
            os.fsync(journal.fileno())

        for group in groups.values():
            if len(group) < 2:
                continue
            canonical = Path(group[0]['path'])
            assert snapshot(canonical) == group[0]['before']
            record(dict(action='make_readonly', path=str(canonical),
                        before=group[0]['before']))
            os.chmod(canonical, group[0]['before']['mode'] & ~0o222)
            for entry in group[1:]:
                target = Path(entry['path'])
                assert snapshot(target) == entry['before']
                temp = target.with_name(target.name + '.recycle-share-pending')
                assert not temp.exists() and not temp.is_symlink()
                record(dict(action='replace_with_hardlink', path=str(target),
                     canonical=str(canonical), before=entry['before'],
                     original_sha256=entry['original_sha256']))
                os.link(canonical, temp)
                # The target remains readable throughout the atomic replacement.
                assert snapshot(target) == entry['before']
                os.replace(temp, target)
                sync_dir(target.parent)
                released += entry['before']['allocated_bytes']
                replacements += 1
                record(dict(action='replaced', path=str(target),
                            after=snapshot(target)))
            assert canonical.stat().st_nlink == len(group)
        # Verify every candidate original path, hash each readonly inode once.
        cache = {}
        for entry in entries:
            path = Path(entry['path'])
            now = snapshot(path)
            key = (now['device'], now['inode'], now['bytes'], now['mtime_ns'])
            if key not in cache:
                cache[key] = sha(path)
            assert cache[key] == entry['original_sha256'], path
            assert now['bytes'] == entry['before']['bytes'], path
        for path, value in scope['ledger_files'].items():
            assert sha(Path(path)) == value['sha256'], path
        # Retain all build files if the unchanged gate is already met. Otherwise
        # choose a smallest sufficient single file, or the largest remaining one.
        pending = list(scope['optional_rebuildable_candidates'])
        while (args.prune_rebuildable_if_needed and pending and
               shutil.disk_usage(R).free < scope['minimum_free_bytes']):
            interval_guard(plan, scope)
            deficit = scope['minimum_free_bytes'] - shutil.disk_usage(R).free
            enough = [e for e in pending
                      if e['before']['allocated_bytes'] >= deficit]
            entry = (min(enough, key=lambda e: e['before']['allocated_bytes'])
                     if enough else max(pending,
                         key=lambda e: e['before']['allocated_bytes']))
            path = allowed(entry, scope, rebuildable=True)
            assert str(path) not in bound and not entry['bound_sha256']
            assert snapshot(path) == entry['before']
            original = sha(path)
            assert snapshot(path) == entry['before']
            record(dict(action='remove_rebuildable', path=str(path),
                 before=entry['before'], original_sha256=original,
                 reason='unchanged 1 GiB startup gate still unmet'))
            free_before_remove = shutil.disk_usage(R).free
            path.unlink()
            sync_dir(path.parent)
            removed.append(dict(path=str(path), original_sha256=original,
                allocated_bytes=entry['before']['allocated_bytes'],
                free_before=free_before_remove,
                free_after=shutil.disk_usage(R).free))
            record(dict(action='removed_rebuildable', **removed[-1]))
            pending.remove(entry)
        for path, value in scope['ledger_files'].items():
            assert sha(Path(path)) == value['sha256'], path
    free_after = shutil.disk_usage(R).free
    summary = dict(passed=True, storage_only=True, ended_t=time.time(),
        plan_sha256=scope['plan_sha256'], scope_sha256=args.scope_sha256,
        candidate_count=len(entries), shared_replacements=replacements,
        released_duplicate_allocated_bytes=released,
        released_rebuildable_allocated_bytes=sum(
            item['allocated_bytes'] for item in removed),
        removed_rebuildable=removed, original_ledgers_unchanged=True,
        all_evidence_paths_and_byte_sha_preserved=True,
        free_before=before_free, free_after=free_after,
        minimum_free_bytes=scope['minimum_free_bytes'],
        startup_space_gate_met=free_after >= scope['minimum_free_bytes'],
        model_or_tests_run=False, content_hash_read_bytes=read_bytes,
        metadata_note=scope['metadata_note'])
    save(out / 'summary.json', summary)
    print(json.dumps(summary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope-sha256', required=True)
    parser.add_argument('--execute-after-m061', required=True, action='store_true')
    parser.add_argument('--prune-rebuildable-if-needed', action='store_true')
    args = parser.parse_args()
    try:
        execute(args)
    except Exception as exc:
        out = R / 'storage-dedup'
        if out.is_dir() and not (out / 'failure.json').exists():
            save(out / 'failure.json', dict(passed=False, ended_t=time.time(),
                 error=repr(exc), automatic_retry=False,
                 note='Retain preflight/journal and inspect; do not rerun.'))
        raise
