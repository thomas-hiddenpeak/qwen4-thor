"""Observe an owned CUDA pool probe in the native q4t service cgroup.

The caller owns the GPU lease and output directory. Each new probe process uses
the existing checkpoint protocol; the observer stays outside its service leaf.
"""
import json
import os
from pathlib import Path
import subprocess
import time

from tools.serve_recovery import resource_kernel as kernel

PAYLOAD_BYTES = 256 * 1024**2
PHASES = ('baseline', 'held', 'freed', 'trimmed')


def integers(values, names):
    if not all(type(values.get(key)) is int and values[key] >= 0 for key in names):
        raise ValueError('missing or invalid accounting counter')


def check_native(native, mode, phase):
    if mode not in ('async', 'async-retained') or phase not in PHASES:
        raise ValueError('unrecognized pool observation')
    touches = dict(baseline='context_initialized', held='gpu_all_words_verified_edges',
                   freed='release_returned_success', trimmed='pool_trim_returned_success')
    if (native['kind'], native['phase'], native['touch'], native['bytes']) != (
            mode, phase, touches[phase], PAYLOAD_BYTES):
        raise ValueError('unexpected payload, phase or completion')
    integers(native, ('pool_used_bytes', 'pool_reserved_bytes', 'cuda_free_bytes',
                      'cuda_total_bytes', 'pool_release_threshold_bytes'))
    if (native['pool_used_bytes'] > native['pool_reserved_bytes']
            or not 0 < native['cuda_total_bytes']
            or native['cuda_free_bytes'] > native['cuda_total_bytes']):
        raise ValueError('inconsistent CUDA counters')


def kernel_sample(scope, pid):
    scope.check()
    fd = scope.fds['service']
    if kernel.read(fd, 'cgroup.procs').splitlines() != [str(pid)]:
        raise ValueError('the service leaf must contain only the original probe')
    membership = (scope.root/'service').relative_to(kernel.CGROUP)
    if Path(f'/proc/{pid}/cgroup').read_text().splitlines() != [f'0::/{membership}']:
        raise ValueError('probe resource membership changed')
    status = Path(f'/proc/{pid}/status').read_text().splitlines()
    rss, = (line.split() for line in status if line.startswith('VmRSS:'))
    if len(rss) != 3 or rss[2] != 'kB':
        raise ValueError('invalid RSS units')
    return dict(monotonic_ns=time.monotonic_ns(), rss_bytes=int(rss[1])*1024,
                current_bytes=int(kernel.read(fd, 'memory.current')),
                events=kernel.counters(kernel.read(fd, 'memory.events')))


def summarize(mode, phases):
    if set(phases) != set(PHASES):
        raise ValueError('incomplete observations')
    base = phases['baseline']
    before = base['native']
    last = 0
    deltas = {}
    for name in PHASES:
        row = phases[name]
        native, samples = row['native'], row['samples']
        check_native(native, mode, name)
        if len(samples) != 2 or native['pool_release_threshold_bytes'] != before['pool_release_threshold_bytes']:
            raise ValueError('incomplete sampling or threshold changed mid-probe')
        for sample in samples:
            integers(sample, ('monotonic_ns','rss_bytes','current_bytes'))
            integers(sample['events'], ('high','max','oom','oom_kill'))
            if (sample['monotonic_ns'] <= last
                    or sample['events'] != base['samples'][0]['events']):
                raise ValueError('sample order or kernel pressure changed')
            last = sample['monotonic_ns']
        deltas[name] = {
            k: native[k]-before[k] for k in ('pool_used_bytes','pool_reserved_bytes','cuda_free_bytes')}
        for key in ('current_bytes','rss_bytes'):
            deltas[name][key] = [a[key]-b[key] for a,b in zip(samples, base['samples'])]
    if ((mode == 'async-retained' and before['pool_release_threshold_bytes'] != PAYLOAD_BYTES)
            or deltas['held']['pool_used_bytes'] != PAYLOAD_BYTES
            or any(deltas[name]['pool_used_bytes'] != 0 for name in ('freed','trimmed'))):
        raise ValueError('scope did not demonstrate one live allocation and synchronized release')
    return deltas


def observe(binary, folder, mode, scope):
    folder.mkdir(mode=0o700)
    with (folder/'probe.stderr').open('xb') as log:
        def enter():
            entry = scope.join_descriptor('service')
            try:
                scope.join_self('service', entry)
            finally:
                os.close(entry)
        proc = subprocess.Popen([binary, mode, folder], preexec_fn=enter,
                                stderr=log, stdin=subprocess.DEVNULL)
        phases = {}
        try:
            for name in PHASES:
                receipt = folder/(name+'.json')
                deadline = time.monotonic()+20
                while not receipt.exists():
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('probe exited or timed out before '+name)
                    time.sleep(.03)
                native = json.loads(receipt.read_bytes())
                check_native(native, mode, name)
                first = kernel_sample(scope, proc.pid)
                time.sleep(.25)
                phases[name] = dict(native=native, samples=[first,kernel_sample(scope, proc.pid)])
                (folder/(name+'.continue')).touch(exist_ok=False)
            if proc.wait(timeout=10):
                raise RuntimeError('probe failed after its last checkpoint')
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    if json.loads((folder/'result.json').read_bytes()) != dict(
            passed=True, bytes=PAYLOAD_BYTES, allocations=1, releases=1):
        raise ValueError('allocation and release receipt differs')
    if not scope.empty('service'):
        raise RuntimeError('owned service leaf still populated')
    return dict(mode=mode, phases=phases, deltas_from_baseline=summarize(mode, phases),
                device=json.loads((folder/'device.json').read_bytes()))
