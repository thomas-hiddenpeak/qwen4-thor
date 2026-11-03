"""Immutable local evidence helpers for the CUDA quota integration stage."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/serve-cuda-quota-integration-20261104'
OLD = 'd66f2bb9df3a34eda1b885da148e6971c25137564e500b9357bd5f9a5f8dfb1b'
SIDECAR = '42d7672ab473aceb315889be8eeac54310121fd47d4ac861d4d40fcc2bfea1f5'
MODES = ('mixed', 'async', 'stream_reuse', 'startup', 'startup_reject', 'disabled')
CASES = ('unconfigured', 'disabled', 'mixed', 'zero', 'backend_failure',
    'ambiguous_failure', 'serve_reason', 'startup', 'startup_reject',
    'stream_flags', 'stream_generations', 'stream_sync_race', 'stream_limit',
    'entry_limit', 'concurrent', 'per_thread', 'release_failure',
    'null_success', 'zero_owner', 'duplicate_owner', 'stream_backend_failure',
    'stream_duplicate', 'unknown_stream',
    'fatal_async_free', 'fatal_sync', 'fatal_destroy', 'fatal_type',
    'fatal_planned_async')
NEW_RUNTIME = ('include/q4t/runtime/allocation_quota.h',
    'include/q4t/runtime/cuda_allocation_quota.h',
    'src/runtime/cuda_allocation_quota.cpp', 'src/runtime/cuda_quota_backend.h',
    'include/q4t/server/cuda_quota_policy.h',
    'include/q4t/server/cuda_quota_metrics.h')


def require(value, message):
    if not value:
        raise RuntimeError(message)


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    with Path(path).open('x') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def identity(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), bytes=path.stat().st_size, sha256=sha(path))


def sources():
    previous = read(ROOT / 'build/q4t.release.json')
    paths = set(read(previous['tested_source_manifest'])) | set(NEW_RUNTIME)
    return {name: sha(ROOT / name) for name in sorted(paths)}


def command(folder, name, argv, timeout=180, expected=0):
    save(folder / (name + '-command.json'), dict(argv=argv, started=time.time()))
    with (folder / (name + '.stdout')).open('xb') as out, \
         (folder / (name + '.stderr')).open('xb') as err:
        result = subprocess.run(argv, cwd=ROOT, stdout=out, stderr=err,
                                timeout=timeout)
    save(folder / (name + '-exit.json'),
         dict(returncode=result.returncode, ended=time.time(), expected=expected))
    print(name, result.returncode, flush=True)
    return result.returncode == expected
