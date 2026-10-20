"""Audit sealed descriptive baseline; never infer physical RAM from sums."""
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/offload-foundation-20261020/baseline-v1'
LENGTHS = [1024, 4096, 8192, 45056, 204800]
BINARY = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def mode_report(folder):
    http, memory = folder / 'http', folder / 'memory'
    end = read(http / 'exit.json')
    require(end['server'] == 0 and end['completed'] == 5 and
            end['http_output_checks_passed'] and end['full_five_tier_completed'] and
            not end['failure'] and not end['cleanup_failure'], 'HTTP completion')
    require((http / 'binary.sha256').read_text().strip() == BINARY, 'HTTP binary')
    capacity = read(http / 'capacity.json')
    require(capacity['matches_requested'] and capacity['effective'] ==
            dict(max_len=208896, max_seq=1, max_prefill=8192), 'capacity')
    rows = read(http / 'results.json')
    require([r['length'] for r in rows] == LENGTHS, 'five tiers')
    for row in rows:
        folder_tier = http / ('context-' + str(row['length']))
        replies = read(folder_tier / 'responses.json')
        require(len(replies) == 3 and len(row['metrics']) == 3 and
                len(row['outputs']) == 3 and len(set(row['outputs'])) == 1 and
                row['deterministic'], 'request count/determinism')
        for i, reply in enumerate(replies):
            require(reply['success'] == 1 and reply['actual_input'] == row['length'] and
                    reply['actual_output'] == 256 and reply['finish'] == ['length'] and
                    reply['prompt_sha256'] == row['prompt_sha256'] and
                    sha(folder_tier / f'output-{i}.txt') == row['outputs'][i],
                    'raw response contract')
            require(all(math.isfinite(value) and value > 0
                        for value in row['metrics'][i].values()), 'positive finite metrics')
    peak = read(memory / 'memory-peak.json')
    require(peak['sampling_complete'] and peak['stop_reason'] == 'target_exited' and
            peak['prelaunch_sample_present'] and not peak['existing_named_pids_at_start'],
            'monitor lifecycle')
    with (memory / 'memory.csv').open() as stream:
        samples = list(csv.DictReader(stream))
    statuses = Counter(r['gpu_status'] for r in samples)
    require(set(statuses) <= {'ok', 'scheduled_skip', 'no_target',
                             'no_matching_compute_process'}, 'resource NVML query failure')
    # Startup may precede the first compute context; keep this unknown sample.
    unknown_context = [dict(phase=r['phase'], t=r['t']) for r in samples
                       if r['gpu_status'] == 'no_matching_compute_process']
    process_io = defaultdict(Counter)
    logical = []
    for line in (http / 'server.log').read_text().splitlines():
        if line.startswith('[q4t][residency] id='):
            fields = dict(re.findall(r'(\w+)=(\S+)', line))
            for key in list(fields):
                if key not in ('id', 'finish'):
                    fields[key] = float(fields[key]) if '.' in fields[key] else int(fields[key])
            require(fields['loads'] == fields['dmiss'] + fields['pmiss'] and
                    fields['loads'] == fields['l2h'] + fields['l2m'] + fields['mh'],
                    'software cache count closure')
            logical.append(fields)
    with (memory / 'resource-samples.jsonl').open() as stream:
        for line in stream:
            sample = json.loads(line)
            for proc in sample['processes']:
                delta = proc.get('io_delta', {}).get('value')
                if delta is not None:
                    process_io[sample['phase']].update(delta)
    return dict(metrics=rows, requests=15, server_exit=0,
        monitor_samples=len(samples), gpu_status_counts=dict(statuses),
        context_not_yet_visible_samples=unknown_context,
        nvml_observed_peak_bytes=peak['gpu_peak_bytes'],
        process_observed_rss_peak_bytes=peak['peaks_bytes']['rss_kb'],
        process_observed_swap_peak_bytes=peak['peaks_bytes']['process_swap_kb'],
        model_cache_endpoints=peak['model_file_cache_endpoint_observations'],
        sampled_process_io_deltas_by_phase=dict(process_io),
        sampled_io_scope='consecutive known samples only; not full request totals or expert-only SSD bytes',
        software_residency=logical, resource_coverage=peak['resource_observations'],
        physical_ram_total=None, shared_cgroup_is_not_experiment_owned=True)


def main():
    manifest = read(STAGE / 'sealed-files.json')
    for name, digest in manifest.items():
        require(sha(STAGE / name) == digest, 'sealed identity: ' + name)
    guard = read(STAGE / 'lease/exit.json')
    require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0, 'lease')
    out = STAGE / 'run'
    state = read(out / 'state.json')
    require(state['status'] == 'MEASURED_NOT_OPTIMIZATION_ACCEPTANCE' and
            state['completed'] == ['resident', 'offload'], 'driver completion')
    plan = read(out / 'plan.json')
    require(plan['binary_sha256'] == BINARY and plan['requests'] == 30 and
            plan['automatic_retries'] == 0 and plan['repeats'] == 3, 'frozen plan')
    modes = {name: mode_report(out / name) for name in ('resident', 'offload')}
    require(not modes['resident']['software_residency'] and
            len(modes['offload']['software_residency']) == 15, 'offload execution coverage')
    identity = []
    for left, right in zip(modes['resident']['metrics'], modes['offload']['metrics']):
        require(left['prompt_sha256'] == right['prompt_sha256'], 'cross-mode input identity')
        identity.append(dict(length=left['length'], outputs_equal=left['outputs'] == right['outputs']))
    print(json.dumps(dict(status='AUDITED_DESCRIPTIVE_BASELINE', sealed_files=len(manifest),
        binary_sha256=BINARY, guard_status=guard['status'], modes=modes,
        cross_mode_output_identity=identity, optimization_acceptance=False,
        statistical_noninferiority=False, strict_physical_ram_acceptance=False,
        cold_ssd_acceptance=False,
        limitations=['fixed resident then offload order with inherited page cache',
            'one service per mode; requests are not independent service replicates',
            'CPU-only scope probes and host checks occurred during descriptive baseline',
            'process storage accounting includes PLE and other model reads',
            'global device counters include unrelated I/O; shared cgroup peaks may predate stage',
            'NVML, RSS, pinned, cache and cgroup counters overlap; never add peaks']),indent=2))


if __name__ == '__main__':
    main()
