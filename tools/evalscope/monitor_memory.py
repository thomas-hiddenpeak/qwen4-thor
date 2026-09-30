"""Sample physical memory for the q4t serve process tree plus system page cache.

Writes a 1 Hz CSV and a final peak summary. The service footprint is the
process tree (q4t serve + descendants): VmRSS (anonymous + file-backed
resident) split via smaps_rollup, plus the system page cache (Cached) as a
separate column. Peaks are reported per column; the acceptance number is
peak(rss) and peak(anon + model page cache), where model page cache is the
system Cached delta above the pre-start baseline (clean file pages are
reclaimable, so the summary reports both raw and delta views).

Usage: monitor_memory.py --name q4t --out DIR [--interval 1]
Start after the serve process exists; it stops when the root process exits.
"""
import argparse
import csv
import json
import os
import sys
import time


def find_pids(name):
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/comm') as f:
                if f.read().strip() == name:
                    pids.append(int(entry))
        except OSError:
            pass
    return pids


def children(root):
    out = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as f:
                parts = f.read().rsplit(')', 1)[1].split()
                ppid = int(parts[1])
            if ppid == root:
                out.append(int(entry))
        except (OSError, ValueError, IndexError):
            pass
    return out


def tree(root):
    seen = {root}
    stack = [root]
    while stack:
        p = stack.pop()
        for c in children(p):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def read_kb(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + ':'):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def smaps_anon_kb(pid):
    try:
        with open(f'/proc/{pid}/smaps_rollup') as f:
            for line in f:
                if line.startswith('Anonymous:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def meminfo():
    info = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                key = line.split(':')[0]
                if key in ('MemTotal', 'MemFree', 'Cached', 'Buffers',
                           'Shmem', 'SwapTotal', 'SwapFree'):
                    info[key] = int(line.split()[1])
    except OSError:
        pass
    return info



def gpu_used_bytes(pids):
    """Sum nvidia-smi per-process GPU memory for the given PIDs.

    On Jetson Thor CUDA allocations are not visible in /proc/meminfo; the
    driver's per-process accounting (nvidia-smi --query-compute-apps) is the
    only reliable source for the service's GPU-side physical memory.
    Returns None when nvidia-smi is unavailable or reports no matching PID.
    """
    if not pids:
        return 0
    try:
        import subprocess
        out = subprocess.run(
            ['nvidia-smi', '--query-compute-apps=pid,used_memory',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        total = 0
        seen = False
        for line in out.stdout.splitlines():
            parts = [x.strip() for x in line.split(',')]
            if len(parts) != 2:
                continue
            try:
                pid, mem = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if pid in pids:
                total += mem * 1024 * 1024
                seen = True
        return total if seen else 0
    except (OSError, subprocess.SubprocessError):
        return None

def svc_used_kb(mi):
    """free(1)-style used: total - free - buffers - cached - shared.

    On Jetson Thor the NVIDIA driver's CUDA allocations do not appear in
    meminfo's standard categories, so this delta (vs the pre-start baseline)
    is the service's physical footprint, including driver memory.
    """
    if 'MemTotal' not in mi:
        return 0
    return (mi['MemTotal'] - mi.get('MemFree', 0) - mi.get('Buffers', 0) -
            mi.get('Cached', 0) - mi.get('Shmem', 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', default='q4t')
    ap.add_argument('--out', required=True)
    ap.add_argument('--interval', type=float, default=1.0)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    root = None
    for _ in range(600):
        pids = find_pids(args.name)
        if pids:
            root = min(pids)
            break
        time.sleep(1)
    if root is None:
        print('root process never appeared', file=sys.stderr)
        return 2

    baseline = meminfo()
    csv_path = os.path.join(args.out, 'memory.csv')
    summary_path = os.path.join(args.out, 'memory-peak.json')
    cols = ['t', 'root', 'nproc', 'rss_kb', 'anon_kb', 'file_kb', 'hwm_kb',
            'cached_kb', 'mem_used_kb', 'svc_used_kb', 'swap_used_kb',
            'gpu_bytes']
    peaks = {c: 0 for c in cols[3:]}
    sim_peak = 0
    last_gpu = None
    with open(csv_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        while True:
            if not os.path.exists(f'/proc/{root}'):
                break
            pids = tree(root)
            rss = anon = hwm = 0
            for p in pids:
                r = read_kb(f'/proc/{p}/status', 'VmRSS')
                h = read_kb(f'/proc/{p}/status', 'VmHWM')
                a = smaps_anon_kb(p)
                if r:
                    rss += r
                if h:
                    hwm += h
                if a is not None:
                    anon += a
            mi = meminfo()
            cached = mi.get('Cached', 0)
            mem_used = mi.get('MemTotal', 0) - mi.get('MemFree', 0)
            svc_used = svc_used_kb(mi)
            swap_used = mi.get('SwapTotal', 0) - mi.get('SwapFree', 0)
            gpu = gpu_used_bytes(pids)
            if gpu is None:
                # nvidia-smi can transiently hang on Thor under load; carry
                # the last good value forward so the peak is not
                # under-reported (the process is still alive and holding its
                # GPU allocation).
                gpu = last_gpu if last_gpu is not None else -1
            else:
                last_gpu = gpu
            if gpu >= 0:
                simultaneous = rss * 1024 + gpu
            else:
                simultaneous = -1
            row = [time.time(), root, len(pids), rss, anon,
                   max(rss - anon, 0), hwm, cached, mem_used, svc_used,
                   swap_used, gpu]
            if simultaneous > 0:
                sim_peak = max(sim_peak, simultaneous)
            w.writerow(row)
            for c, v in zip(cols[3:], row[3:]):
                peaks[c] = max(peaks[c], v)
            f.flush()
            time.sleep(args.interval)
    base_svc = svc_used_kb(baseline)
    gpu_peak = peaks.get('gpu_bytes', 0)
    summary = {
        'root_pid': root,
        'baseline': baseline,
        'baseline_svc_used_kb': base_svc,
        'peaks_kb': peaks,
        'peaks_bytes': {k: v * 1024 for k, v in peaks.items()},
        'gpu_peak_bytes': gpu_peak,
        'service_physical_peak_bytes':
            (peaks['svc_used_kb'] - base_svc) * 1024,
        'service_total_physical_peak_bytes': sim_peak,
        'note': ('rss includes file-backed resident pages; anon is '
                 'smaps_rollup Anonymous; cached is system page cache; '
                 'mem_used = MemTotal - MemFree; svc_used = free(1)-style '
                 'used (total-free-buffers-cached-shared), whose delta vs '
                 'baseline is the service physical footprint on Thor '
                 '(driver CUDA memory is not in meminfo categories); '
                 'gpu_bytes is nvidia-smi per-process GPU memory; '
                 'service_total_physical_peak_bytes = max over time of '
                 '(process-tree VmRSS + nvidia-smi GPU bytes), the '
                 'acceptance number for the 54 GB budget'),
    }
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary['peaks_bytes'], indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
