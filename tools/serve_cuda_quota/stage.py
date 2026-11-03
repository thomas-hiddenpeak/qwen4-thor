"""Frozen CPU contracts and four tiny CUDA probes; never starts serve/HTTP."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
BASE = ROOT/'.q4t-work/serve-cuda-quota-core-20261104'
CPU = BASE/'cpu-v2'
OUT = BASE/'execution'
BUILD = BASE/'build-v2'
DEFAULT = 'd66f2bb9df3a34eda1b885da148e6971c25137564e500b9357bd5f9a5f8dfb1b'
SIDECAR = '42d7672ab473aceb315889be8eeac54310121fd47d4ac861d4d40fcc2bfea1f5'
DAY = ROOT/'.q4t-work/serve-soak-20261103/day'
MODES = ('mixed', 'async', 'startup', 'concurrent')
SOURCES = (
    'include/q4t/runtime/allocation_quota.h',
    'include/q4t/runtime/reservation_owner.h',
    'tests/runtime_allocation_quota_test.cpp',
    'tools/serve_cuda_quota/probe.cpp',
    'tools/serve_cuda_quota/CMakeLists.txt',
    'tools/serve_cuda_quota/stage.py',
    'tools/serve_cuda_quota/test_stage.py',
    'docs/SERVE_CUDA_QUOTA_CORE_2026-11-04.md',
    'tools/evalscope/gpu_exclusive.py',
    'tools/evalscope/nvml_process_query.py',
    'tools/evalscope/bounded_log.py',
)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def read(path):
    return json.loads(Path(path).read_text())


def identity(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), bytes=path.stat().st_size, sha256=sha(path))


def sources():
    return {p:sha(ROOT/p) for p in SOURCES}


def unchanged():
    require(sha(ROOT/'build/q4t') == DEFAULT and
            sha(ROOT/'build/q4t.release.json') == SIDECAR, 'default pair drift')
    release = read(ROOT/'build/q4t.release.json')
    runtime = read(release['tested_source_manifest'])
    require(all(sha(ROOT/p) == digest for p,digest in runtime.items()),
            'current default runtime sources changed')
    plan = read(DAY/'plan.json')
    require(not (DAY/'execution').exists(), 'operator day test has started')
    require(all(sha(ROOT/p) == digest for p,digest in plan['source_files'].items()),
            'operator day sources drifted')
    return dict(default=DEFAULT, sidecar=SIDECAR, runtime_source_count=len(runtime),
                day_plan=identity(DAY/'plan.json'), day_execution_absent=True)


def command(folder, name, argv, timeout=60):
    started = time.monotonic()
    with (folder/(name+'.stdout')).open('xb') as stdout, \
         (folder/(name+'.stderr')).open('xb') as stderr:
        try:
            value = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr,
                                   timeout=timeout, check=False)
            result = dict(argv=argv, returncode=value.returncode,
                          elapsed_seconds=time.monotonic()-started)
        except subprocess.TimeoutExpired:
            result = dict(argv=argv, returncode=None, timed_out=True,
                          elapsed_seconds=time.monotonic()-started)
    save(folder/(name+'.json'), result)
    require(result['returncode'] == 0, name+' failed; original outputs retained')
    return result


def build():
    CPU.mkdir(parents=True, exist_ok=False)
    save(CPU/'baseline.json', unchanged())
    original = sources()
    save(CPU/'sources.json', original)
    for name in SOURCES:
        destination = CPU/'source'/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT/name).read_bytes())
    command(CPU, 'configure', ['cmake', '-S', 'tools/serve_cuda_quota',
        '-B', str(BUILD), '-DCMAKE_BUILD_TYPE=Release',
        '-DCMAKE_CXX_COMPILER=g++-14', '-DCUDAToolkit_ROOT=/usr/local/cuda-13.3'])
    command(CPU, 'build', ['cmake', '--build', str(BUILD), '--parallel', '2'])
    text = '\n'.join(p.read_text() for p in CPU.glob('*.std*'))
    require(not re.search(r'\bwarning:', text, re.I), 'build warning')
    require(sources() == original, 'source drift during build')
    command(CPU, 'host-needed', ['readelf', '-d', str(BUILD/'quota_host_contracts')])
    require('cuda' not in (CPU/'host-needed.stdout').read_text().lower(),
            'host contracts linked CUDA')
    command(CPU, 'probe-link', ['ldd', str(BUILD/'quota_cuda_probe')])
    command(CPU, 'default-cuda-symbols', ['nm', '-D', '--undefined-only',
                                        str(ROOT/'build/q4t')])
    command(CPU, 'allocation-call-sites', ['rg', '-n',
        r'\b(cudaMalloc\w*|cudaHostAlloc|cudaHostRegister|cuMem\w*)\s*\(',
        'src', '--glob', '*.{cpp,cu,h}'])
    save(CPU/'allocation-audit.json', dict(
        kind='lexical call-site inventory, not AST or DSO interception proof',
        wrappers=identity(ROOT/'cmake/CudaCleanupBoundary.cmake'),
        production_adapter=identity(ROOT/'src/runtime/startup_reservation.cpp'),
        new_core_is_linked_into_default=False,
        external_dso_internal_calls_are_not_wrapped=True,
        default=identity(ROOT/'build/q4t')))
    libraries = {}
    for line in (CPU/'probe-link.stdout').read_text().splitlines():
        match = re.search(r'=> (/\S+)', line)
        if match:
            libraries[match[1]] = identity(match[1])
    save(CPU/'build-result.json', dict(passed=True, zero_warnings=True,
        host=identity(BUILD/'quota_host_contracts'), probe=identity(BUILD/'quota_cuda_probe'),
        libraries=libraries, sources=original, baseline=unchanged()))
    print('BUILD_PASS', flush=True)


def build_gate():
    result = read(CPU/'build-result.json')
    require(result['passed'] and result['zero_warnings'] and
            result['sources'] == sources(), 'build/source gate')
    for item in (result['host'], result['probe'], *result['libraries'].values()):
        require(identity(item['path']) == item, 'build/dependency identity')
    require(unchanged() == result['baseline'], 'default/day drift')
    return result


def cpu():
    build_gate()
    folder = CPU/'tests'
    folder.mkdir()
    command(folder, 'contracts', [str(BUILD/'quota_host_contracts')], timeout=30)
    lines = (folder/'contracts.stdout').read_text().splitlines()
    case_names = [line[5:] for line in lines if line.startswith('PASS ')]
    results = [json.loads(line) for line in lines if line.startswith('{')]
    require(len(case_names) == len(set(case_names)) == 26 and
            results[0] == dict(passed=True, tests=26, cuda_linked=False),
            'CPU contract count/result')
    command(folder, 'driver', ['/usr/bin/python3', '-B', '-m', 'unittest',
        'tools.serve_cuda_quota.test_stage', '-v'], timeout=30)
    save(folder/'result.json', dict(passed=True, cases=case_names,
        host_result=results, model_gpu_http_started=False, sources=sources()))
    print('CPU_PASS', len(case_names), flush=True)


def parse_probe(mode, raw):
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    require(mode in MODES and all(row.get('event') in
        ('device', 'quota', 'verified', 'address_reuse_observation', 'result')
        for row in rows), 'mode/record type')
    require(sum(row['event'] == 'device' for row in rows) == 1 and
            sum(row['event'] == 'result' for row in rows) == 1,
            'duplicate identity/result')
    require(rows and rows[0]['event'] == 'device' and
        rows[0]['sm_major'] == 11 and rows[0]['sm_minor'] == 0 and
        rows[0]['integrated'] == 1, 'device identity')
    require(rows[-1] == dict(event='result', mode=mode, passed=True),
            'probe result/mode')
    quotas = [r for r in rows if r['event'] == 'quota']
    expected = {
        'mixed': [('mixed_full', 32, 0, 4, 4, 0),
                  ('mixed_denied', 32, 0, 4, 4, 0),
                  ('mixed_released', 0, 0, 0, 4, 4),
                  ('mixed_recovered', 0, 0, 0, 5, 5)],
        'async': [('async_pending', 16, 8, 2, 2, 1),
                  ('other_stream_no_credit', 16, 8, 2, 2, 1),
                  ('one_stream_complete', 8, 8, 1, 2, 2),
                  ('both_streams_pending', 16, 16, 2, 3, 3),
                  ('device_complete', 0, 0, 0, 3, 3)],
        'startup': [('plan_rejected_rolled_back', 0, 0, 0, 2, 2),
                    ('plan_handed_once', 16, 0, 3, 5, 2),
                    ('plan_closed', 0, 0, 0, 5, 5)],
        'concurrent': [('concurrent_held', 8, 0, 4, 4, 0),
                       ('concurrent_closed', 0, 0, 0, 4, 4)],
    }[mode]
    require(len(quotas) == len(expected), 'quota checkpoint count')
    limit = dict(mixed=32, async_=16, startup=16, concurrent=8)[
        'async_' if mode == 'async' else mode]*1024*1024
    for row, (label, charged, pending, entries, allocations, frees) in zip(quotas, expected):
        require(row['label'] == label and row['charged'] == charged*1024*1024 and
            row['pending'] == pending*1024*1024 and row['entries'] == entries and
            row['allocations'] == allocations and row['frees'] == frees and
            0 <= row['charged'] <= row['peak'] == row['limit'] == limit and
            row['unknown'] == row['reserved'] == row['releasing'] == 0 and
            row['poisoned'] is False and row['live'] + row['pending'] == row['charged'],
            'quota checkpoint mismatch: '+label)
    verified = [row for row in rows if row['event'] == 'verified']
    expected_sizes = dict(mixed=[8,8,8,8,32], async_=[8,8,8],
                          startup=[4,8,4], concurrent=[2,2,2,2])
    sizes = expected_sizes['async_' if mode == 'async' else mode]
    require([r['bytes'] for r in verified] == [n*1024*1024 for n in sizes],
            'whole-buffer checks incomplete')
    values = [r['value'] for r in verified]
    fixed_values = dict(mixed=[31,32,33,34,97], async_=[61,62,73], startup=[41,42,43])
    if mode == 'concurrent':
        require(len(set(values)) == 4 and all(81 <= v <= 88 for v in values),
                'concurrent independent buffer values')
    else:
        require(values == fixed_values['async_' if mode == 'async' else mode],
                'buffer values')
    reuse = [row for row in rows if row['event'] == 'address_reuse_observation']
    require(len(reuse) == int(mode == 'async') and
            all(type(row['same']) is bool for row in reuse), 'reuse observation')
    return dict(mode=mode, checkpoints=quotas, verified_bytes=sum(r['bytes'] for r in verified),
                verified_buffers=len(verified), device=rows[0], passed=True)


def prepare():
    result = build_gate()
    require(read(CPU/'tests/result.json')['passed'], 'CPU gate')
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit stage before GPU preparation')
    lock = ROOT/'.q4t-work/gpu-exclusive.lock'
    require(lock.stat().st_ino == 3294067 and lock.stat().st_dev == 66305,
            'shared lock identity')
    save(BASE/'plan.json', dict(modes=MODES, per_probe_seconds=15,
        driver_seconds=90, source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        sources=sources(), build_result_sha256=sha(CPU/'build-result.json'),
        probe=result['probe'], libraries=result['libraries'], baseline=unchanged(),
        lock=dict(path=str(lock), device=lock.stat().st_dev, inode=lock.stat().st_ino),
        model_starts=0, http_requests=0, performance_acceptance=False,
        physical_ram_hard_limit=False, serve_integrated=False))
    print('GPU_PLAN_PREPARED', flush=True)


def check():
    plan = read(BASE/'plan.json')
    require(plan['sources'] == sources() and tuple(plan['modes']) == MODES and
            plan['per_probe_seconds'] == 15 and plan['driver_seconds'] == 90,
            'frozen scope/source')
    result = build_gate()
    require(sha(CPU/'build-result.json') == plan['build_result_sha256'] and
            result['probe'] == plan['probe'] and unchanged() == plan['baseline'],
            'frozen dependency/default identity')
    return plan


def execute():
    plan = check()
    OUT.mkdir()
    started = time.monotonic()
    # This is a child of the production GPU guard, not a standalone entry.
    parent = os.getppid()
    parent_args = Path(f'/proc/{parent}/cmdline').read_bytes()
    require(b'tools/evalscope/gpu_exclusive.py' in parent_args, 'GPU guard parent')
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(1, signal.SIGKILL, 0, 0, 0) == 0 and os.getppid() == parent,
            'driver parent-death binding')
    lock = Path(plan['lock']['path'])
    with lock.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        require(stat.st_dev == plan['lock']['device'] and stat.st_ino == plan['lock']['inode'],
                'original lock inode')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError('guard does not hold exclusive lease')
    require(read(lock)['pid'] == parent, 'lease holder metadata/parent')
    save(OUT/'begin.json', dict(monotonic=started, pid=os.getpid(), parent=parent,
                               plan_sha256=sha(BASE/'plan.json')))
    completed = []
    try:
        for mode in MODES:
            require(time.monotonic()-started < 90, 'driver deadline')
            folder = OUT/mode
            folder.mkdir()
            argv = [plan['probe']['path'], mode, str(os.getpid())]
            probe_start = time.monotonic()
            with (folder/'stdout.jsonl').open('xb') as stdout, \
                 (folder/'stderr.log').open('xb') as stderr:
                process = subprocess.Popen(argv, cwd=ROOT, stdout=stdout, stderr=stderr)
                pidfd = os.pidfd_open(process.pid)
                try:
                    stat = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()
                    save(folder/'started.json', dict(pid=process.pid, start_ticks=int(stat[19]),
                        ppid=int(stat[1]), argv=argv, monotonic=probe_start))
                    try:
                        code = process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                        code = process.wait(timeout=5)
                        save(folder/'timed-out.json', dict(timed_out=True, returncode=code))
                        raise RuntimeError('probe timeout: '+mode)
                finally:
                    os.close(pidfd)
            elapsed = time.monotonic()-probe_start
            save(folder/'exit.json', dict(returncode=code, elapsed_seconds=elapsed))
            require(code == 0 and elapsed <= 15, 'probe failed: '+mode)
            summary = parse_probe(mode, (folder/'stdout.jsonl').read_text())
            save(folder/'result.json', summary)
            completed.append(summary)
        require(time.monotonic()-started < 90, 'driver deadline')
        check()
        save(OUT/'result.json', dict(passed=True, probes=completed,
            elapsed_seconds=time.monotonic()-started, model_starts=0, http_requests=0,
            serve_integrated=False, physical_ram_hard_limit=False))
    except BaseException as error:
        save(OUT/'failed.json', dict(error=repr(error), completed=completed,
                                    elapsed_seconds=time.monotonic()-started))
        raise
    print('CUDA_PROBES_PASS', len(completed), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'cpu', 'prepare', 'check', 'execute'])
    args = parser.parse_args()
    globals()[args.action]()
