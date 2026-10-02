"""Observe q4t memory without treating overlapping counters as physical totals.

Start BEFORE launching the service and wait for --ready-file. --pid-file binds
samples to the runner's server PID; --phase-file labels loading/warmup/requests.
All *_kb fields are Linux KiB, *_bytes fields bytes. System Cached is global,
not model-owned. GPU driver allocations, pinned memory and process residency
may overlap on Thor; this monitor deliberately does not sum them for acceptance.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from file_cache import model_files, observe_files
from resource_metrics import ResourceSampler, normalize_cgroup, resource_summary

MEM_KEYS = ('MemTotal', 'MemFree', 'MemAvailable', 'Cached', 'Buffers',
            'Shmem', 'SwapTotal', 'SwapFree', 'SwapCached', 'AnonPages',
            'Mapped', 'Mlocked', 'Unevictable', 'KReclaimable', 'Slab',
            'SReclaimable', 'SUnreclaim', 'PageTables', 'KernelStack',
            'Dirty', 'Writeback')
PROCESS_KEYS = ('rss_kb', 'anon_kb', 'file_kb', 'shmem_kb', 'hwm_kb',
                'pss_kb', 'pss_anon_kb', 'pss_file_kb', 'pss_shmem_kb',
                'private_kb', 'locked_kb', 'vmpin_kb', 'process_swap_kb')
SYSTEM_COLS = {k: 'system_' + k.lower() + '_kb' for k in MEM_KEYS}
STOP = False


def read_fields(path):
    fields = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, tail = line.partition(':')
            words = tail.split()
            if sep and words:
                try:
                    fields[key] = int(words[0])
                except ValueError:
                    pass
    except (OSError, UnicodeError):
        return None
    return fields


def meminfo():
    fields = read_fields('/proc/meminfo') or {}
    return {key: fields[key] for key in MEM_KEYS if key in fields}


def process_stat(pid):
    try:
        parts = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(parts[1]), int(parts[19])  # ppid, starttime ticks
    except (OSError, ValueError, IndexError):
        return None


def find_pids(name):
    result = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                if (entry / 'comm').read_text().strip() == name:
                    result.append(int(entry.name))
            except OSError:
                pass
    return result


def tree(root):
    parents = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            st = process_stat(int(entry.name))
            if st:
                parents[int(entry.name)] = st[0]
    result = {root}
    while True:
        added = {pid for pid, parent in parents.items() if parent in result}
        if added <= result:
            return result
        result |= added


def process_memory(pids):
    """Missing fields remain unknown; RSS sums can duplicate shared pages."""
    values = {key: 0 for key in PROCESS_KEYS}
    for pid in pids:
        status = read_fields(f'/proc/{pid}/status') or {}
        rollup = read_fields(f'/proc/{pid}/smaps_rollup') or {}
        fields = {
            'rss_kb': status.get('VmRSS'), 'hwm_kb': status.get('VmHWM'),
            'anon_kb': status.get('RssAnon'), 'file_kb': status.get('RssFile'),
            'shmem_kb': status.get('RssShmem'), 'vmpin_kb': status.get('VmPin'),
            'pss_kb': rollup.get('Pss'), 'pss_anon_kb': rollup.get('Pss_Anon'),
            'pss_file_kb': rollup.get('Pss_File'),
            'pss_shmem_kb': rollup.get('Pss_Shmem'),
            'locked_kb': rollup.get('Locked'),
            'process_swap_kb': rollup.get('Swap'),
        }
        if 'Private_Clean' in rollup and 'Private_Dirty' in rollup:
            fields['private_kb'] = rollup['Private_Clean'] + rollup['Private_Dirty']
        else:
            fields['private_kb'] = None
        for key, value in fields.items():
            if value is None or values[key] is None:
                values[key] = None
            else:
                values[key] += value
    return values


def parse_gpu_csv(output, pids):
    total = 0
    matched = False
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid not in pids:
            continue
        matched = True
        try:
            mib = int(parts[1])
            if mib < 0:
                raise ValueError
        except ValueError:
            return None, 'unknown_matching_process'
        total += mib * 1024 * 1024
    return total, 'ok' if matched else 'no_matching_compute_process'


def gpu_observation(pids):
    if not pids:
        return None, 'no_target'
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-compute-apps=pid,used_memory',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2)
        if result.returncode:
            return None, 'query_failed'
        return parse_gpu_csv(result.stdout, pids)
    except (OSError, subprocess.SubprocessError):
        return None, 'query_unavailable_or_timeout'


def gpu_used_bytes(pids):
    # Compatibility for callers; unknown is never replaced by an old sample.
    return gpu_observation(pids)[0]


def summarize(rows, baseline, root, root_start, interval, stopped, existing):
    numeric = [key for key in rows[0] if key.endswith(('_kb', '_bytes'))] if rows else []
    peaks = {}
    for key in numeric:
        values = [row[key] for row in rows if row.get(key) is not None]
        peaks[key] = max(values) if values else None
    peaks_bytes = {key: (value * 1024 if key.endswith('_kb') else value)
                   for key, value in peaks.items() if value is not None}
    target_rows = [row for row in rows if row.get('root')]
    before = any(row.get('phase') == 'before_start' for row in rows)
    unresolved = ['CUDA/pinned/process-resident overlap is not identified',
                  'sampling does not capture instantaneous allocation peaks']
    if not target_rows or any(row.get('model_file_cache_bytes') is None
                              for row in target_rows):
        unresolved.insert(0, 'model-owned file cache is not fully observed')
    return {
        'schema_version': 3, 'root_pid': root, 'root_start_ticks': root_start,
        'baseline': baseline, 'sample_interval_seconds': interval,
        'sampling_complete': stopped == 'target_exited',
        'stop_reason': stopped, 'prelaunch_sample_present': before,
        'existing_named_pids_at_start': sorted(existing),
        'sample_count': len(rows), 'target_sample_count': len(target_rows),
        'gpu_unknown_samples': sum(row.get('gpu_bytes') is None for row in target_rows),
        'peaks_kb': {k: v for k, v in peaks.items() if k.endswith('_kb')},
        'peaks_bytes': peaks_bytes,
        'gpu_peak_bytes': peaks.get('gpu_bytes'),
        'service_physical_peak_bytes': None,
        'service_total_physical_peak_bytes': None,
        'accounting_status': 'INDETERMINATE',
        'unresolved': unresolved,
        'notes': ['Linux kB fields are KiB; NVIDIA query values are MiB.',
                  'System Cached and its delta are global context, not model cache.',
                  'VmPin/Locked are observations, not complete CUDA pinned accounting.',
                  'PSS is proportional residency; RSS sums may count shared pages twice.',
                  'NVIDIA per-process bytes are driver accounting, not a disjoint physical category.',
                  'No stale GPU values are carried forward; gaps remain unknown.',
                  'Independent counter peaks must not be added as a simultaneous total.'],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--name', default='q4t')
    ids = ap.add_mutually_exclusive_group()
    ids.add_argument('--pid', type=int)
    ids.add_argument('--pid-file', type=Path)
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--model-dir', type=Path)
    ap.add_argument('--cgroup-path', type=Path,
                    help='Expected v2 cgroup; compare with actual PID membership')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--wait-timeout', type=float, default=600)
    ap.add_argument('--baseline-json', type=Path)
    ap.add_argument('--phase-file', type=Path)
    ap.add_argument('--ready-file', type=Path)
    ap.add_argument('--stop-file', type=Path)
    args = ap.parse_args()
    if args.interval <= 0 or args.wait_timeout <= 0:
        ap.error('interval and wait-timeout must be positive')
    if args.cgroup_path:
        try:
            args.cgroup_path = normalize_cgroup(args.cgroup_path)
        except ValueError as exc:
            ap.error(str(exc))
    args.out.mkdir(parents=True, exist_ok=True)
    owned = ('memory.csv', 'memory-peak.json', 'model-cache-manifest.json',
             'model-cache.jsonl', 'resource-samples.jsonl')
    conflicts = [str(args.out / name) for name in owned
                 if (args.out / name).exists()]
    conflicts += [str(path) for path in (args.ready_file, args.stop_file)
                  if path is not None and path.exists()]
    if conflicts:
        ap.error('refusing to overwrite existing evidence: ' + ', '.join(conflicts))
    cache_paths = model_files(args.model_dir) if args.model_dir else []
    resources = ResourceSampler(args.cgroup_path,
                                cache_paths or ([args.model_dir] if args.model_dir else []))
    if cache_paths:
        manifest = [{'path': str(path), 'dev': path.stat().st_dev,
                     'inode': path.stat().st_ino, 'size_bytes': path.stat().st_size}
                    for path in cache_paths]
        with (args.out / 'model-cache-manifest.json').open('x') as manifest_file:
            json.dump({'model_dir': str(args.model_dir), 'files': manifest,
                       'scope': 'all non-hidden root files plus ple/mtp assets'},
                      manifest_file, indent=2)
            manifest_file.write('\n')
        (args.out / 'model-cache.jsonl').open('x').close()
    started = time.time()
    existing = set(find_pids(args.name))
    baseline = {'t': started, 'meminfo': meminfo(), 'source': 'monitor_start'}
    if args.baseline_json:
        baseline = json.loads(args.baseline_json.read_text())
    root = args.pid
    root_start = None
    rows = []
    reason = 'interrupted'
    cols = ['t', 'sample_end_t', 'sample_duration_seconds', 'phase', 'root',
            'root_start_ticks', 'nproc', *PROCESS_KEYS, 'cached_kb',
            'mem_used_kb', 'swap_used_kb', 'gpu_bytes', 'gpu_status',
            'model_file_cache_bytes', 'model_file_cache_known_bytes',
            'model_file_cache_files', 'model_file_cache_errors',
            'model_file_cache_start_t', 'model_file_cache_end_t',
            *SYSTEM_COLS.values()]

    def stop_handler(signum, frame):
        del signum, frame
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    with (args.out / 'memory.csv').open('x', newline='') as file, \
            (args.out / 'resource-samples.jsonl').open('x') as resource_file:
        writer = csv.DictWriter(file, fieldnames=cols)
        writer.writeheader()
        while not STOP:
            now = time.time()
            if root is None:
                if args.pid_file and args.pid_file.exists():
                    try:
                        root = int(args.pid_file.read_text().strip())
                    except (OSError, ValueError):
                        pass
                elif not args.pid_file:
                    new = set(find_pids(args.name)) - existing
                    if len(new) == 1:
                        root = new.pop()
                if root is None and now - started > args.wait_timeout:
                    reason = 'target_never_appeared'
                    break
            exited = False
            if root is not None:
                st = process_stat(root)
                if st is None or (root_start is not None and st[1] != root_start):
                    reason = 'target_exited'
                    exited = True
                else:
                    root_start = st[1]
            pids = tree(root) if root and not exited else set()
            phase = 'loading_or_requests' if root else 'before_start'
            if exited:
                phase = 'after_exit'
            row = {'t': now, 'root': root if pids else None,
                   'root_start_ticks': root_start,
                   'nproc': len(pids), 'phase': phase}
            if args.phase_file and pids:
                try:
                    row['phase'] = args.phase_file.read_text().strip() or row['phase']
                except OSError:
                    pass
            resource_row = resources.sample(root if pids else None, pids=pids)
            resource_row.update(sequence=len(rows), phase=row['phase'],
                                memory_sample_start_t=now)
            resource_file.write(json.dumps(resource_row, separators=(',', ':')) + '\n')
            resource_file.flush()
            row.update(process_memory(pids) if pids else {key: None for key in PROCESS_KEYS})
            mi = meminfo()
            row.update({column: mi.get(key) for key, column in SYSTEM_COLS.items()})
            row['cached_kb'] = mi.get('Cached')
            row['mem_used_kb'] = (mi['MemTotal'] - mi['MemFree']
                                  if 'MemTotal' in mi and 'MemFree' in mi else None)
            row['swap_used_kb'] = (mi['SwapTotal'] - mi['SwapFree']
                                   if 'SwapTotal' in mi and 'SwapFree' in mi else None)
            row['gpu_bytes'], row['gpu_status'] = gpu_observation(pids)
            if cache_paths:
                cache = observe_files(cache_paths)
                row['model_file_cache_bytes'] = cache['resident_bytes']
                row['model_file_cache_known_bytes'] = cache['known_resident_lower_bytes']
                row['model_file_cache_files'] = cache['observed_files']
                row['model_file_cache_errors'] = len(cache['errors'])
                row['model_file_cache_start_t'] = cache['start_t']
                row['model_file_cache_end_t'] = cache['end_t']
                cache_row = {'start_t': cache['start_t'], 'end_t': cache['end_t'],
                             'resident_pages': {str(i): value['cached_pages']
                                for i, value in enumerate(cache['files'])},
                             'file_paths': [value['path'] for value in cache['files']]
                                if cache['errors'] else None,
                             'errors': cache['errors']}
                with (args.out / 'model-cache.jsonl').open('a') as cache_file:
                    cache_file.write(json.dumps(cache_row, separators=(',', ':')) + '\n')
            row['sample_end_t'] = time.time()
            row['sample_duration_seconds'] = row['sample_end_t'] - now
            rows.append(row)
            writer.writerow(row)
            file.flush()
            if args.ready_file and not args.ready_file.exists():
                args.ready_file.parent.mkdir(parents=True, exist_ok=True)
                args.ready_file.write_text(json.dumps({'t': now, 'root': root}))
            if exited:
                break
            if args.stop_file and args.stop_file.exists():
                reason = 'controller_stopped'
                break
            time.sleep(max(0.0, args.interval - (time.time() - now)))
    summary = summarize(rows, baseline, root, root_start, args.interval, reason, existing)
    with (args.out / 'resource-samples.jsonl').open() as resource_file:
        summary['resource_observations'] = resource_summary(
            json.loads(line) for line in resource_file)
    with (args.out / 'memory-peak.json').open('x') as summary_file:
        json.dump(summary, summary_file, indent=2)
        summary_file.write('\n')
    print(json.dumps({'samples': len(rows), 'stop_reason': reason,
                      'accounting_status': 'INDETERMINATE'}))
    return 0 if rows else 2


if __name__ == '__main__':
    sys.exit(main())
