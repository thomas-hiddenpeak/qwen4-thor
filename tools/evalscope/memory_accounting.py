"""Evaluate the evidence completeness of a Thor memory budget measurement.

Counter values are preserved by source. This tool never calls global Cached
'model cache', never adds independent peaks, and never assumes RSS and NVIDIA
allocation accounting are disjoint. Historical v1 reports cannot establish a
complete service physical peak; an authorized overrun does not cure that gap.
"""
import argparse
import csv
import json
from pathlib import Path
import sys


SCHEMA_VERSION = 1
UNKNOWN = ('model_file_cache_attribution', 'cuda_pinned_rss_overlap')


def number(value):
    try:
        result = float(value)
        if result != result or result < 0 or result == float('inf'):
            return None
        return int(result)
    except (TypeError, ValueError, OverflowError):
        return None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def column_peak(rows, key, multiplier=1):
    values = [number(row.get(key)) for row in rows]
    return max((value * multiplier for value in values if value is not None),
               default=None)


def evaluate_memory_csv(memory_dir, budget_bytes=54_000_000_000,
                        allow_overrun=False, c3_dir=None):
    memory_dir = Path(memory_dir)
    summary = read_json(memory_dir / 'memory-peak.json') or {}
    try:
        with (memory_dir / 'memory.csv').open(newline='') as file:
            rows = list(csv.DictReader(file))
    except (OSError, csv.Error):
        rows = []
    target = [row for row in rows if number(row.get('root'))]
    schema = summary.get('schema_version', 1)
    gaps = list(UNKNOWN)
    if not rows:
        gaps.append('missing_memory_csv')
    if not target:
        gaps.append('no_target_process_samples')
    if not summary.get('prelaunch_sample_present'):
        gaps.append('no_same_timeline_prelaunch_sample')
    if not summary.get('sampling_complete'):
        gaps.append('lifecycle_sampling_incomplete_or_legacy')
    gpu_unknown = sum(number(row.get('gpu_bytes')) is None for row in target)
    if gpu_unknown:
        gaps.append('gpu_observation_gaps')
    if schema < 2:
        gaps += ['legacy_gpu_freshness_unknown', 'legacy_baseline_timing_unknown']
    if summary.get('existing_named_pids_at_start'):
        gaps.append('other_named_processes_present_at_start')
    gpu_peak = column_peak(target, 'gpu_bytes')
    rss_peak = column_peak(target, 'rss_kb', 1024)
    private_peak = column_peak(target, 'private_kb', 1024)
    cache_rows = [number(row.get('model_file_cache_bytes')) for row in target]
    cache_complete = bool(cache_rows) and all(value is not None for value in cache_rows)
    if cache_complete:
        gaps.remove('model_file_cache_attribution')
    physical_lower_samples = []
    for row in target:
        cache = number(row.get('model_file_cache_known_bytes'))
        if cache is None:
            cache = number(row.get('model_file_cache_bytes'))
        if cache is not None:
            anon = number(row.get('pss_anon_kb')) or 0
            shmem = number(row.get('pss_shmem_kb')) or 0
            physical_lower_samples.append(cache + (anon + shmem) * 1024)
    # File scans and smaps run sequentially. Their sum is a sampling-window
    # estimate, NOT a lower bound on one simultaneous physical peak. Only
    # the single-process private-residency observation is used as a bound.
    lower_peak = column_peak([row for row in target
                              if number(row.get('nproc')) == 1],
                             'private_kb', 1024)
    known_overrun = lower_peak is not None and lower_peak > budget_bytes
    gate = 'FAIL' if known_overrun and not allow_overrun else 'INDETERMINATE'
    # Source union bounds describe THESE observed counters, not physical RAM:
    # max(A,B) <= |A union B| <= A+B only if both sources count byte sets.
    # That premise is unproven for driver allocation vs Linux residency.
    paired = [(number(row.get('rss_kb')), number(row.get('gpu_bytes')))
              for row in target]
    paired = [(rss * 1024, gpu) for rss, gpu in paired
              if rss is not None and gpu is not None]
    diagnostic_sum = max((rss + gpu for rss, gpu in paired), default=None)
    source_max = max((max(rss, gpu) for rss, gpu in paired), default=None)
    cached_peak = column_peak(rows, 'cached_kb', 1024)
    memtotal = column_peak(rows, 'system_memtotal_kb', 1024)
    baseline = summary.get('baseline') or {}
    baseline_meminfo = baseline.get('meminfo', baseline)
    if memtotal is None:
        kb = number(baseline_meminfo.get('MemTotal'))
        memtotal = kb * 1024 if kb is not None else None
    c3 = read_json(Path(c3_dir) / 'evidence.json') if c3_dir else None
    historical = {
        'reported_service_total_physical_peak_bytes':
            summary.get('service_total_physical_peak_bytes'),
        'c3_global_cached_deltas_unattributed': c3,
        'usable_as_complete_physical_total': False,
    }
    phases = sorted({row.get('phase', 'unknown') for row in rows})
    return {
        'schema_version': SCHEMA_VERSION,
        'gate': gate,
        'budget_status': 'PROVEN_OVERRUN_FROM_LOWER_BOUND' if known_overrun else 'UNKNOWN',
        'budget_bytes': budget_bytes,
        'user_approved_overrun': bool(allow_overrun),
        'approval_applied': False,
        'candidate_total': None,
        'complete_service_physical_peak_bytes': None,
        'reason': 'Physical ownership and overlap are unresolved; authorization cannot convert unknown usage into a measured overrun.',
        'unresolved': sorted(set(gaps)),
        'sources': {'memory_csv': str(memory_dir / 'memory.csv'),
                    'memory_summary': str(memory_dir / 'memory-peak.json'),
                    'monitor_schema_version': schema,
                    'csv_sha256': file_sha256(memory_dir / 'memory.csv'),
                    'summary_sha256': file_sha256(memory_dir / 'memory-peak.json')},
        'coverage': {'sample_count': len(rows), 'target_samples': len(target),
                     'gpu_unknown_samples': gpu_unknown,
                     'phases': phases,
                     'first_sample_t': rows[0].get('t') if rows else None,
                     'last_sample_t': rows[-1].get('t') if rows else None,
                     'sampling_complete': summary.get('sampling_complete', False)},
        'legacy_component_caveat': ('v1 anon_kb is smaps Anonymous and file_kb is RSS minus Anonymous, not an independent file-ownership measurement.' if schema < 2 else None),
        'observed_component_peaks_bytes': {
            'process_tree_rss': rss_peak,
            'process_tree_pss': column_peak(target, 'pss_kb', 1024),
            'process_private_resident': private_peak,
            'process_rss_anon': column_peak(target, 'anon_kb', 1024),
            'process_rss_file': column_peak(target, 'file_kb', 1024),
            'process_rss_shmem': column_peak(target, 'shmem_kb', 1024),
            'process_vmpin': column_peak(target, 'vmpin_kb', 1024),
            'process_smaps_locked': column_peak(target, 'locked_kb', 1024),
            'nvidia_process_driver_accounted': gpu_peak,
            'model_file_cache': column_peak(target, 'model_file_cache_bytes'),
            'model_file_cache_known_subset': column_peak(target, 'model_file_cache_known_bytes'),
            'global_cached_unattributed': cached_peak,
            'global_swap_used': column_peak(rows, 'swap_used_kb', 1024),
        },
        'bounds': {
            'observed_service_physical_lower_bytes': lower_peak,
            'lower_basis': 'Maximum observed private residency when the process tree has exactly one process. Sequential model-cache/PSS sums are excluded from this bound.',
            'complete_service_physical_upper_bytes': None,
            'machine_memtotal_context_bytes': memtotal,
            'machine_memtotal_is_service_measurement': False,
            'instantaneous_model_file_cache_lower_bytes': 0,
            'instantaneous_model_file_cache_upper_bytes': None,
            'model_file_cache_scope': ('cachestat over explicit inode-deduplicated model files; includes pre-existing cache, not exclusive process ownership. The sequential file sum is a sampling-window observation, not an atomic total.' if cache_complete else 'Only global Cached is available; model ownership is unresolved.'),
            'budget_exceeded_by_known_private_residency':
                private_peak > budget_bytes if private_peak is not None else None,
        },
        'sampling_window_estimates': {
            'model_cache_plus_anon_shmem_pss_peak_bytes': max(physical_lower_samples, default=None),
            'is_strict_simultaneous_lower_bound': False,
            'used_for_budget_gate': False,
            'model_cache_complete_at_every_target_sample': cache_complete,
            'note': 'Components describe disjoint kinds of pages, but each row spans sequential smaps, GPU and per-file cache observations. Allocation and reclaim may occur between them; do not treat their sum as a simultaneous peak or bound.',
        },
        'nonphysical_diagnostics': {
            'max_sampled_rss_plus_driver_bytes': diagnostic_sum,
            'max_sampled_max_rss_driver_bytes': source_max,
            'usable_as_physical_total_or_bound': False,
            'reason': 'RSS includes mapped file/shared/pinned pages; driver counters may overlap and do not establish disjoint physical byte sets.',
        },
        'historical_claims': historical,
        'limitations': ['Snapshots are sequential observations over recorded sample intervals, not atomic or guaranteed instantaneous maxima.',
                        'Process PSS is proportional; anonymous/shared-memory PSS is a lower bound on their physical union.',
                        'Global machine DRAM context is not an exact service upper bound without reserved/driver ownership reconciliation.'],
        'accounting_contract': {
            'units': {'linux_kb': 'KiB (1024 bytes)', 'nvidia_query': 'MiB (1048576 bytes)', 'gpu_bytes': 'bytes, no further conversion'},
            'category_rules': [
                'Private/Anonymous/PSS/File/Locked/VmPin describe overlapping Linux views; do not add them.',
                'File-backed resident pages also occur in global Cached.',
                'CUDA/pinned memory may occur in driver and process/kernel views; resolve overlap before summation.',
                'Global Cached deltas are affected by unrelated processes, startup and reclaim; they are not model attribution.',
                'Peak of a complete contemporaneous ledger is required; summing independent peaks is invalid.',
            ],
            'required_next_evidence': [
                'Start this monitor before launch, bind the exact PID, and label loading/warmup/requests on one timeline.',
                'Obtain model-file resident bytes with a validated non-faulting file-residency method or equivalent ownership evidence; record inaccessible files as unknown.',
                'Reconcile CUDA allocations and pinned buffers against RSS/PSS/kernel ownership using allocation identities, not aggregate subtraction.',
                'Retain full lifecycle samples, sampling gaps, and process identity; report sampled peak separately from any unobserved instantaneous peak.',
            ],
        },
    }


def file_sha256(path):
    import hashlib
    try:
        digest = hashlib.sha256()
        with Path(path).open('rb') as file:
            for block in iter(lambda: file.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--memory-dir', required=True, type=Path)
    ap.add_argument('--c3-dir', type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--budget-bytes', type=int, default=54_000_000_000)
    ap.add_argument('--allow-overrun', action='store_true')
    args = ap.parse_args()
    if args.budget_bytes <= 0:
        ap.error('budget must be positive')
    if args.output.exists():
        ap.error('refusing to overwrite existing evidence: ' + str(args.output))
    result = evaluate_memory_csv(args.memory_dir, args.budget_bytes,
                                 args.allow_overrun, args.c3_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output_file:
        json.dump(result, output_file, indent=2)
        output_file.write('\n')
    print(json.dumps({'gate': result['gate'], 'candidate_total': None,
                      'unresolved': result['unresolved']}))
    if result['gate'] in ('PASS', 'USER_APPROVED_OVERRUN'):
        return 0
    return 1 if result['gate'] == 'FAIL' else 2


if __name__ == '__main__':
    sys.exit(main())
