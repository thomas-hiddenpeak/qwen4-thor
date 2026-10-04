"""Read-only Thor RAM observations; never infer a complete service union.

This independent collector records sequential read windows. Global Linux
nonfree memory, memcg charges, process RSS and CUDA allocation counters are
different views: neither their sums nor differences identify service RAM.
No model files, pagemaps, GPU allocations, cache advice or system writes are
performed. Debugfs observations cover only the objects exposed by that driver.
An empty nvmap/dma_buf interface does not prove zero CUDA allocation.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 65536
MEM_KEYS = (
    'MemTotal', 'MemFree', 'MemAvailable', 'Buffers', 'Cached',
    'SwapCached', 'SwapTotal', 'SwapFree', 'AnonPages', 'Mapped', 'Shmem',
    'Mlocked', 'Unevictable', 'KReclaimable', 'Slab', 'SReclaimable',
    'SUnreclaim', 'PageTables', 'KernelStack', 'Dirty', 'Writeback',
    'CmaTotal', 'CmaFree',
)
DRIVER_FILES = {
    'nvmap_iovmm_clients': '/sys/kernel/debug/nvmap/iovmm/clients',
    'nvmap_total_memory': '/sys/kernel/debug/nvmap/stats/total_memory',
    'dma_buf_bufinfo': '/sys/kernel/debug/dma_buf/bufinfo',
}
GAPS = [
    'No GPU allocation-to-physical-frame identities are available here.',
    'Unmapped model file cache and driver allocations are not reconciled.',
    'Global Linux counters include other services and driver pools.',
    'Debugfs objects need not include CUDA allocations on this driver.',
    'Sequential snapshots cannot establish an atomic physical union.',
    'Sampling cannot establish an unsampled instantaneous lifetime peak.',
]


def parse_meminfo(raw):
    values, errors = {}, {}
    for line in raw.splitlines():
        key, separator, tail = line.partition(':')
        if key not in MEM_KEYS or not separator:
            continue
        if key in values or key in errors:
            values.pop(key, None)
            errors[key] = 'duplicate_field'
            continue
        match = re.fullmatch(r'\s*(\d+)\s+kB\s*', tail)
        if not match:
            errors[key] = 'expected_nonnegative_Linux_KiB'
        else:
            values[key] = int(match.group(1)) * 1024
    for key in MEM_KEYS:
        if key not in values and key not in errors:
            errors[key] = 'not_reported'
    if not values:
        raise ValueError('no recognized meminfo fields')
    nonfree = None
    if 'MemTotal' in values and 'MemFree' in values:
        if values['MemFree'] <= values['MemTotal']:
            nonfree = values['MemTotal'] - values['MemFree']
        else:
            errors['global_linux_nonfree'] = 'MemFree_exceeds_MemTotal'
    nonslab = None
    if 'KReclaimable' in values and 'SReclaimable' in values:
        if values['KReclaimable'] >= values['SReclaimable']:
            nonslab = values['KReclaimable'] - values['SReclaimable']
        else:
            errors['non_slab_reclaimable'] = 'inconsistent_counters'
    return {
        'fields_bytes': values, 'field_errors': errors,
        'global_linux_nonfree_bytes': nonfree,
        'global_non_slab_reclaimable_bytes': nonslab,
        'derived_scope': 'global diagnostic only; not service RAM or GPU RAM',
    }


def parse_nvmap_clients(raw):
    if not re.search(r'CLIENT\s+PROCESS\s+PID\s+SIZE', raw):
        raise ValueError('nvmap client header unavailable')
    totals = re.findall(r'^total\s+(\d+)K\s*$', raw, re.MULTILINE)
    if len(totals) != 1:
        raise ValueError('expected one nvmap total with explicit K suffix')
    return {
        'reported_total_K': int(totals[0]),
        'unit_note': 'K suffix retained; no physical-byte conversion assumed',
        'scope': 'nvmap iovmm driver view, CUDA coverage unproven',
        'cuda_physical_bytes': None,
    }


def parse_nvmap_stat(raw):
    if not re.fullmatch(r'\s*\d+\s*', raw):
        raise ValueError('expected one nonnegative nvmap statistic')
    return {
        'raw_counter': int(raw.strip()), 'unit': 'unverified',
        'scope': 'driver statistic; enabled state and CUDA coverage unproven',
        'cuda_physical_bytes': None,
    }


def parse_dma_buf(raw):
    if 'Dma-buf Objects:' not in raw:
        raise ValueError('dma_buf header unavailable')
    totals = re.findall(r'^Total (\d+) objects, (\d+) bytes\s*$',
                        raw, re.MULTILINE)
    if len(totals) != 1:
        raise ValueError('expected one dma_buf total')
    count, size = (int(value) for value in totals[0])
    if count == 0 and size != 0:
        raise ValueError('empty dma_buf object set has nonzero bytes')
    return {
        'reported_object_count': count, 'reported_object_size_bytes': size,
        'scope': 'exported DMA-BUF object sizes, not a physical RAM union',
        'cuda_physical_bytes': None,
    }


def read_observation(path, parser, privileged=False, timeout=2.0):
    started_t, started_ns = time.time(), time.monotonic_ns()
    result = {'path': str(path), 'start_t': started_t,
              'start_monotonic_ns': started_ns, 'value': None,
              'status': 'UNKNOWN', 'error': None, 'raw_text': None}
    try:
        if privileged:
            command = ['sudo', '-n', 'head', '-c',
                       str(MAX_SOURCE_BYTES + 1), '--', str(path)]
            process = subprocess.run(command, capture_output=True,
                                     timeout=timeout, check=False)
            result['command_returncode'] = process.returncode
            if process.returncode:
                raise OSError(process.stderr.decode(
                    'utf-8', errors='replace').strip()[:2048])
            content = process.stdout
        else:
            with Path(path).open('rb') as source:
                content = source.read(MAX_SOURCE_BYTES + 1)
        result['truncated'] = len(content) > MAX_SOURCE_BYTES
        result['raw_text'] = content[:MAX_SOURCE_BYTES].decode('utf-8')
        if result['truncated']:
            raise ValueError('source exceeds fixed read bound')
        result['value'] = parser(result['raw_text'])
        result['status'] = 'OBSERVED'
    except (OSError, ValueError, UnicodeError,
            subprocess.SubprocessError) as error:
        result['error'] = {'kind': type(error).__name__, 'message': str(error)}
    result['end_monotonic_ns'] = time.monotonic_ns()
    result['end_t'] = time.time()
    result['duration_seconds'] = (
        result['end_monotonic_ns'] - started_ns) / 1e9
    return result


def process_identity(pid, proc_root=Path('/proc')):
    raw = (Path(proc_root) / str(pid) / 'stat').read_text()
    fields = raw.rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': int(fields[19]), 'state': fields[0]}


class Binding:
    """Bind once to PID and start ticks; never silently follow a replacement."""
    def __init__(self, pid_file, proc_root=Path('/proc')):
        self.pid_file = pid_file
        self.proc_root = proc_root
        self.expected = None

    def observe(self):
        if self.pid_file is None:
            return {'status': 'UNBOUND', 'identity': None}
        try:
            pid = int(Path(self.pid_file).read_text().strip())
            if pid <= 0:
                raise ValueError('PID must be positive')
            identity = process_identity(pid, self.proc_root)
            stable = {key: identity[key] for key in ('pid', 'start_ticks')}
            if self.expected is None:
                self.expected = stable
            if self.expected != stable:
                return {'status': 'IDENTITY_CHANGED', 'identity': identity,
                        'expected': self.expected}
            return {'status': 'LIVE' if identity['state'] != 'Z' else 'ZOMBIE',
                    'identity': identity, 'expected': self.expected}
        except (OSError, ValueError, IndexError) as error:
            return {'status': 'UNKNOWN' if self.expected else 'NOT_YET_BOUND',
                    'identity': None, 'expected': self.expected,
                    'error': str(error)}


def same_identity(before, after):
    if before['status'] != 'LIVE' or after['status'] != 'LIVE':
        return False
    keys = ('pid', 'start_ticks')
    return all(before['identity'][key] == after['identity'][key]
               for key in keys)


def collect_sample(sequence, binding, driver_due, driver_mode, phase_file,
                   endpoint=False, observer=read_observation):
    started_t, started_ns = time.time(), time.monotonic_ns()
    before = binding.observe()
    phase, phase_error = None, None
    if phase_file:
        try:
            phase = Path(phase_file).read_text().strip()
        except OSError as error:
            phase_error = str(error)
    sources = {'meminfo': observer('/proc/meminfo', parse_meminfo)}
    parsers = {'nvmap_iovmm_clients': parse_nvmap_clients,
               'nvmap_total_memory': parse_nvmap_stat,
               'dma_buf_bufinfo': parse_dma_buf}
    for name, path in DRIVER_FILES.items():
        if driver_due and driver_mode != 'off':
            sources[name] = observer(path, parsers[name],
                                     privileged=driver_mode == 'sudo')
        else:
            sources[name] = {
                'path': path, 'status': 'DISABLED' if driver_mode == 'off'
                else 'SCHEDULED_SKIP', 'value': None,
                'start_t': None, 'end_t': None, 'duration_seconds': None,
            }
    after = binding.observe()
    ended_ns = time.monotonic_ns()
    return {
        'schema_version': SCHEMA_VERSION, 'sequence': sequence,
        'start_t': started_t, 'end_t': time.time(),
        'start_monotonic_ns': started_ns, 'end_monotonic_ns': ended_ns,
        'duration_seconds': (ended_ns - started_ns) / 1e9,
        'phase': phase, 'phase_read_error': phase_error, 'endpoint': endpoint,
        'binding_before': before, 'binding_after': after,
        'same_live_target_across_window': same_identity(before, after),
        'sources': sources, 'atomic_snapshot': False,
        'service_physical_union_bytes': None,
    }


class Summary:
    def __init__(self):
        self.count = 0
        self.first_t = None
        self.last_t = None
        self.max_window = 0
        self.total_sampling_seconds = 0
        self.source_status_counts = {}
        self.source_max_duration_seconds = {}
        self.global_peaks = {}
        self.binding_status_counts = {}
        self.live_windows = 0

    def add(self, sample):
        self.count += 1
        if self.first_t is None:
            self.first_t = sample['start_t']
        self.last_t = sample['end_t']
        self.max_window = max(self.max_window, sample['duration_seconds'])
        self.total_sampling_seconds += sample['duration_seconds']
        self.live_windows += int(sample['same_live_target_across_window'])
        status = sample['binding_after']['status']
        self.binding_status_counts[status] = (
            self.binding_status_counts.get(status, 0) + 1)
        for name, source in sample['sources'].items():
            counts = self.source_status_counts.setdefault(name, {})
            counts[source['status']] = counts.get(source['status'], 0) + 1
            duration = source.get('duration_seconds')
            if duration is not None:
                self.source_max_duration_seconds[name] = max(
                    self.source_max_duration_seconds.get(name, 0), duration)
        mem = sample['sources']['meminfo'].get('value')
        if mem:
            observed = dict(mem['fields_bytes'])
            for name in ('global_linux_nonfree_bytes',
                         'global_non_slab_reclaimable_bytes'):
                observed[name] = mem[name]
            for key, value in observed.items():
                if value is not None:
                    self.global_peaks[key] = max(
                        self.global_peaks.get(key, 0), value)

    def result(self, reason, policy):
        return {
            'schema_version': SCHEMA_VERSION, 'stop_reason': reason,
            'sample_count': self.count, 'first_t': self.first_t,
            'last_t': self.last_t, 'maximum_window_seconds': self.max_window,
            'total_sampling_seconds': self.total_sampling_seconds,
            'source_status_counts': self.source_status_counts,
            'source_max_duration_seconds': self.source_max_duration_seconds,
            'binding_status_counts': self.binding_status_counts,
            'same_live_target_windows': self.live_windows,
            'global_observed_counter_peaks_bytes': self.global_peaks,
            'policy': policy, 'gate': 'INDETERMINATE',
            'measurement_gap': GAPS,
            'complete_service_physical_peak_bytes': None,
            'strict_service_physical_lower_bound_bytes': None,
            'strict_service_physical_upper_bound_bytes': None,
            'claims_complete_lifecycle': False,
            'claims_atomic_snapshot': False,
            'claims_cuda_coverage_from_empty_debugfs': False,
        }


def positive_float(raw):
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return value


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--pid-file', type=Path)
    parser.add_argument('--phase-file', type=Path)
    parser.add_argument('--stop-file', type=Path)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--interval', type=positive_float, default=1.0)
    parser.add_argument('--driver-interval', type=positive_float, default=10.0)
    parser.add_argument('--driver-mode', choices=('sudo', 'direct', 'off'),
                        default='sudo')
    parser.add_argument('--max-seconds', type=positive_float, default=10800.0)
    args = parser.parse_args()
    if args.interval < 1 or args.driver_interval < 10:
        parser.error('global interval >= 1s and driver interval >= 10s '
                     'required')
    outputs = [args.output_dir / name for name in
               ('samples.jsonl', 'summary.json', 'identity.json')]
    conflicts = [path for path in outputs + [args.ready_file, args.stop_file]
                 if path is not None and path.exists()]
    if conflicts:
        parser.error('refusing to overwrite existing evidence: ' +
                     ', '.join(str(path) for path in conflicts))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    policy = {
        'global_interval_seconds': args.interval,
        'driver_interval_seconds': args.driver_interval,
        'driver_mode': args.driver_mode,
        'driver_endpoints': ['first_sample', 'collector_stop'],
        'driver_timeout_seconds_per_source': 2.0,
        'max_source_bytes': MAX_SOURCE_BYTES,
        'max_seconds': args.max_seconds,
        'carry_forward_values': False, 'model_file_reads': False,
        'gpu_api_queries': False, 'privileged_operations': 'read fixed paths',
        'sampling': 'best effort sequential windows; no catch-up bursts',
    }
    identity = {'command': sys.argv, 'pid': os.getpid(),
                'kernel': os.uname().release, 'policy': policy,
                'source_sha256': hashlib.sha256(Path(__file__).read_bytes())
                .hexdigest(), 'started_t': time.time()}
    with outputs[2].open('x') as stream:
        json.dump(identity, stream, indent=2)
        stream.write('\n')
    stopped = {'reason': None}

    def stop(signum, unused_frame):
        stopped['reason'] = signal.Signals(signum).name

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    binding, summary = Binding(args.pid_file), Summary()
    deadline = time.monotonic() + args.max_seconds
    next_sample, last_driver = time.monotonic(), None
    reason = None
    with outputs[0].open('x') as stream:
        while reason is None:
            now = time.monotonic()
            reason = stopped['reason']
            if reason is None and args.stop_file and args.stop_file.exists():
                reason = 'STOP_FILE'
            if reason is None and now >= deadline:
                reason = 'DEADLINE_REACHED'
            if reason is None and now < next_sample:
                time.sleep(min(0.1, next_sample - now))
                continue
            driver_due = (reason is not None or last_driver is None or
                          now - last_driver >= args.driver_interval)
            sample = collect_sample(summary.count, binding, driver_due,
                                    args.driver_mode, args.phase_file,
                                    endpoint=reason is not None)
            stream.write(json.dumps(sample, separators=(',', ':')) + '\n')
            stream.flush()
            summary.add(sample)
            if driver_due:
                last_driver = time.monotonic()
            if args.ready_file and summary.count == 1:
                args.ready_file.parent.mkdir(parents=True, exist_ok=True)
                with args.ready_file.open('x') as ready:
                    json.dump({'collector_pid': os.getpid(),
                               'first_sample_end_t': sample['end_t']}, ready)
            next_sample = max(now + args.interval, time.monotonic())
    result = summary.result(reason, policy)
    result['samples_sha256'] = file_sha256(outputs[0])
    with outputs[1].open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'gate': result['gate'], 'sample_count': summary.count,
                      'stop_reason': reason, 'summary': str(outputs[1])}))
    return 2 if reason == 'DEADLINE_REACHED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
