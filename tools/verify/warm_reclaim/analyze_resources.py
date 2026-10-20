"""Offline resource analysis of one sealed warm-reclaim stage; no sampling.

Reports observations and bounded associations. Kernel misc-reclaimable bytes
are not assumed to belong entirely to NVIDIA pools. All consumed inputs must
match the stage's sealed manifest. Output must be outside that sealed stage.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import statistics

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--stage', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
BASE = args.stage.resolve(strict=True)
destination = args.output.resolve()
if destination.is_relative_to(BASE):
    parser.error('output must be outside sealed stage')
if destination.exists():
    parser.error('refusing to overwrite existing output')
manifest_path = BASE / 'sealed-files.json'
if not manifest_path.is_file():
    parser.error('stage is not sealed yet')
MANIFEST = json.loads(manifest_path.read_text())
SOURCES = {}


def read(path):
    data = (BASE / path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if MANIFEST.get(path) != digest:
        raise ValueError('input identity does not match sealed manifest: ' + path)
    SOURCES[path] = digest
    return data.decode()


def document(path):
    return json.loads(read(path))


def records(path):
    return [json.loads(line) for line in read(path).splitlines()]


cache_launch = document('run/cache-launch.json')
PAGE_SIZE = cache_launch['page_size_bytes']
if (type(PAGE_SIZE) is not int or PAGE_SIZE <= 0
        or PAGE_SIZE & (PAGE_SIZE - 1)):
    raise ValueError('sealed cache-launch page_size_bytes must be a positive power of two')


def metrics(text):
    return {line.split()[0].rstrip(':'): int(line.split()[1])
            for line in text.splitlines()}


def buddy(text):
    result = {}
    for line in text.splitlines():
        head, tail = line.split('zone', 1)
        parts = tail.split()
        counts = list(map(int, parts[1:]))
        result[head.strip() + ' zone ' + parts[0]] = dict(
            counts_by_order=counts,
            free_bytes=sum(count * PAGE_SIZE * (1 << order)
                           for order, count in enumerate(counts)),
            blocks_order_ge9=sum(counts[9:]),
            bytes_order_ge9=sum(count * PAGE_SIZE * (1 << order)
                               for order, count in enumerate(counts) if order >= 9))
    return result


raw_system = records('resources/resources.jsonl')
if not raw_system or any(row['failures'] for row in raw_system):
    raise ValueError('missing or failed required resource observations')
milestones = {row['event']: row for row in records('run/milestones.jsonl')}
origin_name = 'runner_begin' if 'runner_begin' in milestones else 'prepare_begin'
origin = milestones[origin_name]['monotonic']
system = []
for row in raw_system:
    memory = metrics(row['system']['meminfo']['value'])
    vm = metrics(row['system']['vmstat']['value'])
    system.append(dict(monotonic=row['monotonic'], relative=row['monotonic'] - origin,
                       meminfo_kib=memory, vmstat=vm,
                       buddy=buddy(row['system']['buddyinfo']['value']),
                       model_present=any(t['kind'] == 'model' for t in row['targets'])))


def previous(when):
    return next((row for row in reversed(system) if row['monotonic'] <= when),
                system[0])


def following(when):
    return next((row for row in system if row['monotonic'] >= when), system[-1])


def summarize(row):
    result = {key: row[key] for key in ('monotonic', 'relative', 'buddy')}
    result['meminfo_gib'] = {key: row['meminfo_kib'][key] / 1048576 for key in
                             ('MemFree', 'MemAvailable', 'Cached', 'KReclaimable',
                              'SReclaimable', 'AnonPages', 'Shmem')}
    result['kernel_misc_reclaimable_gib'] = (
        row['vmstat']['nr_kernel_misc_reclaimable'] * PAGE_SIZE / (1 << 30))
    return result


def delta(first, last):
    counters = ('allocstall_normal', 'allocstall_movable', 'pgscan_kswapd',
                'pgscan_direct', 'pgsteal_kswapd', 'pgsteal_direct',
                'pgscan_file', 'pgsteal_file', 'pgscan_anon', 'pgsteal_anon',
                'compact_stall', 'compact_success', 'compact_fail',
                'compact_migrate_scanned', 'compact_free_scanned',
                'pgmigrate_success', 'pgmigrate_fail', 'oom_kill')
    return dict(start=first['monotonic'], end=last['monotonic'],
                seconds=last['monotonic'] - first['monotonic'],
                counters={key: last['vmstat'][key] - first['vmstat'][key]
                          for key in counters})


queries = records('lease/nvml-queries.jsonl')
failed_queries = [q for q in queries if q['status'] != 'OK']
failed_query = failed_queries[-1] if failed_queries else None
api_events = ([json.loads(line) for line in failed_query.get('stdout', '').splitlines()]
              if failed_query else [])
pending_apis = {}
for event in api_events:
    if event['event'] == 'begin':
        pending_apis[event['api']] = event
    elif event['event'] == 'end':
        pending_apis.pop(event['api'], None)
pending = max(pending_apis.values(), key=lambda x: x['monotonic'], default=None)
cleanup = (document('run/normal-process.json')
           if (BASE / 'run/normal-process.json').is_file() else {})
wall_offset = statistics.median(row['wall'] - row['monotonic']
                                for row in raw_system)
cleanup_signals = [dict(row, monotonic=row['t'] - wall_offset)
                   for row in cleanup.get('signals', [])]
term = cleanup_signals[0]['monotonic'] if cleanup_signals else None
model_rows = [row for row in system if row['model_present']]
first_no_model = (next((row for row in system
                       if row['monotonic'] > model_rows[-1]['monotonic']), None)
                  if model_rows else None)
low_misc = next((row for row in model_rows
                if row['vmstat']['nr_kernel_misc_reclaimable'] * PAGE_SIZE
                < (1 << 30)), None)
misc_return = (next((row for row in system
                    if row['monotonic'] > first_no_model['monotonic']
                    and (row['vmstat']['nr_kernel_misc_reclaimable'] -
                         model_rows[-1]['vmstat']['nr_kernel_misc_reclaimable'])
                    * PAGE_SIZE > (1 << 30)), None)
               if first_no_model else None)
landmarks = dict(initial=summarize(system[0]),
                 pre_runner=summarize(previous(origin)),
                 final=summarize(system[-1]))
for name, row in (
        ('first_model', model_rows[0] if model_rows else None),
        ('last_model', model_rows[-1] if model_rows else None),
        ('misc_first_below_1gib', low_misc),
        ('before_pending_nvml_api', previous(pending['monotonic']) if pending else None),
        ('before_cleanup_sigterm', previous(term) if term else None),
        ('first_model_absent', first_no_model),
        ('misc_return_after_exit', misc_return)):
    landmarks[name] = summarize(row) if row else None
for name in ('node_compaction_begin', 'node_compaction_end'):
    if name in milestones:
        choose = following if name.endswith('_end') else previous
        landmarks[name] = summarize(choose(milestones[name]['monotonic']))

warm_expected = document('run/cache-expected.json')
warm_paths = {path for path, count in warm_expected.items() if count}
launch_vector = {row['path']: row['resident_bytes'] for row in cache_launch['files']}
launch_vector_matches = (cache_launch['complete_file_set_observed'] and
                         all(launch_vector.get(path) == count
                             for path, count in warm_expected.items()))
cache = []
for path in sorted((BASE / 'resources/cache').glob('*.json')):
    row = document(str(path.relative_to(BASE)))
    if row['errors'] or not row['complete_file_set_observed']:
        raise ValueError('incomplete cache observation: ' + str(path))
    warm_resident = sum(item['resident_bytes'] for item in row['files']
                        if item['path'] in warm_paths)
    cache.append(dict(sequence=row['sequence'],
                      monotonic_start=row['observer_start']['monotonic'],
                      monotonic_end=row['observer_finished']['monotonic'],
                      relative_start=row['observer_start']['monotonic'] - origin,
                      total_bytes=row['resident_bytes'],
                      warm_set_bytes=warm_resident,
                      other_model_files_bytes=row['resident_bytes'] - warm_resident,
                      errors=row['errors']))

cgroups = []
for row in records('resources/cgroup.jsonl'):
    for group in row['groups']:
        item = dict(monotonic=row['monotonic'], relative=row['monotonic'] - origin,
                    path=group['path'], status=group['status'])
        if group.get('files'):
            files = group['files']
            required = ('memory.current', 'memory.max', 'memory.swap.current',
                        'memory.swap.max', 'memory.stat', 'memory.events')
            if all(files[name]['value'] is not None for name in required):
                item.update(current_bytes=int(files['memory.current']['value']),
                            max_bytes=int(files['memory.max']['value']),
                            swap_current_bytes=int(files['memory.swap.current']['value']),
                            swap_max_bytes=int(files['memory.swap.max']['value']),
                            stat=metrics(files['memory.stat']['value']),
                            events=metrics(files['memory.events']['value']))
            else:
                item['partial_files'] = files
        cgroups.append(item)
active_groups = [row for row in cgroups if 'current_bytes' in row]

trace = [json.loads(line.split('[memory-trace] ', 1)[1]) for line in
         (read('run/normal/http/server.log').splitlines()
          if (BASE / 'run/normal/http/server.log').is_file() else [])
         if line.startswith('[memory-trace] ')]
begins = {row['call']: row for row in trace if row['phase'] == 'begin'}
ends = {row['call']: row for row in trace if row['phase'] == 'end'}
slow = [dict(call=call, api=begin['api'], bytes=begin['bytes'],
             start=begin['mono'], end=ends[call]['mono'],
             seconds=ends[call]['mono'] - begin['mono'], rc=ends[call]['rc'],
             overlaps_cleanup=bool(term and ends[call]['mono'] >= term))
        for call, begin in begins.items() if call in ends
        and ends[call]['mono'] - begin['mono'] > .1]
pending_calls = [row for call, row in begins.items() if call not in ends]
stack_hits = collections.defaultdict(list)
for row in raw_system:
    for target in row['targets']:
        for task in target['tasks']:
            stack = task.get('stack', {}).get('value') or ''
            for marker in ('compact_zone', 'rmapiLockAcquire',
                           'nv_alloc_system_pages', 'try_to_free_pages'):
                if marker in stack:
                    stack_hits[target['kind'] + ':' + marker].append(
                        dict(monotonic=row['monotonic'], pid=target['pid'],
                             tid=task['tid']))

blocked_rows = ([row for row in system
                 if pending['monotonic'] <= row['monotonic'] <=
                 (term if term is not None else system[-1]['monotonic'])]
                if pending else [])
normal_key = next(key for key in system[0]['buddy'] if key.endswith(' Normal'))
blocked_orders = dict(
    samples=len(blocked_rows),
    normal_order_ge9_zero=sum(row['buddy'][normal_key]['blocks_order_ge9'] == 0
                             for row in blocked_rows),
    normal_order9_min=min((row['buddy'][normal_key]['counts_by_order'][9]
                          for row in blocked_rows), default=None),
    normal_order9_max=max((row['buddy'][normal_key]['counts_by_order'][9]
                          for row in blocked_rows), default=None),
    normal_order_ge10_max=max((sum(row['buddy'][normal_key]['counts_by_order'][10:])
                              for row in blocked_rows), default=None),
    memfree_gib_min=min((row['meminfo_kib']['MemFree'] / 1048576
                        for row in blocked_rows), default=None),
    memfree_gib_max=max((row['meminfo_kib']['MemFree'] / 1048576
                        for row in blocked_rows), default=None))

failed_query_rows = ([row for row in system
                     if api_events[0]['monotonic'] <= row['monotonic'] <=
                     (term if term is not None else system[-1]['monotonic'])]
                    if api_events else [])
failed_query_orders = dict(
    samples=len(failed_query_rows),
    start_monotonic=api_events[0]['monotonic'] if api_events else None,
    end_monotonic=term,
    normal_order_ge9_zero=sum(
        row['buddy'][normal_key]['blocks_order_ge9'] == 0
        for row in failed_query_rows),
    normal_order9_max=max((row['buddy'][normal_key]['counts_by_order'][9]
                          for row in failed_query_rows), default=None),
    normal_order_ge10_max=max((sum(row['buddy'][normal_key]['counts_by_order'][10:])
                              for row in failed_query_rows), default=None),
    memfree_gib_min=min((row['meminfo_kib']['MemFree'] / 1048576
                        for row in failed_query_rows), default=None),
    memfree_gib_max=max((row['meminfo_kib']['MemFree'] / 1048576
                        for row in failed_query_rows), default=None))

deltas = dict(preparation=delta(system[0], previous(origin)))
if model_rows:
    deltas['loading'] = delta(previous(origin), model_rows[-1])
if pending and term:
    deltas['nvml_pending_before_cleanup'] = delta(
        previous(pending['monotonic']), previous(term))
if api_events and term:
    deltas['failed_nvml_query_before_cleanup'] = delta(
        previous(api_events[0]['monotonic']), previous(term))
if term:
    deltas['cleanup_and_after'] = delta(previous(term), system[-1])
if 'node_compaction_begin' in milestones and 'node_compaction_end' in milestones:
    deltas['sampled_covering_node_compaction'] = delta(
        previous(milestones['node_compaction_begin']['monotonic']),
        following(milestones['node_compaction_end']['monotonic']))

compaction_files = {}
if (BASE / 'run/compaction').is_dir():
    for path in sorted((BASE / 'run/compaction').glob('*')):
        if path.is_file():
            relative = str(path.relative_to(BASE))
            text = read(relative)
            try:
                compaction_files[path.name] = json.loads(text)
            except json.JSONDecodeError:
                compaction_files[path.name] = text

compaction_summary = None
if 'before.json' in compaction_files and 'after.json' in compaction_files:
    before, after = [compaction_files[name]
                     for name in ('before.json', 'after.json')]
    snapshots = []
    for raw in (before, after):
        snapshots.append(dict(
            monotonic=raw['monotonic'], relative=raw['monotonic'] - origin,
            meminfo_kib=metrics(raw['files']['/proc/meminfo']),
            vmstat=metrics(raw['files']['/proc/vmstat']),
            buddy=buddy(raw['files']['/proc/buddyinfo'])))
    first, last = snapshots
    compaction_summary = dict(
        duration_seconds=after['monotonic'] - before['monotonic'],
        before=summarize(first), after=summarize(last),
        meminfo_change_kib={key: last['meminfo_kib'][key] - value
                            for key, value in first['meminfo_kib'].items()},
        vmstat_deltas={key: last['vmstat'][key] - value
                       for key, value in first['vmstat'].items()},
        buddy_change={zone: dict(
            counts_by_order=[right - left for left, right in zip(
                values['counts_by_order'],
                last['buddy'][zone]['counts_by_order'])],
            bytes_order_ge9=last['buddy'][zone]['bytes_order_ge9'] -
                           values['bytes_order_ge9'],
            free_bytes=last['buddy'][zone]['free_bytes'] - values['free_bytes'])
            for zone, values in first['buddy'].items()},
        limitation='Non-atomic global before/after reads; task identity and event timing require kernel trace. This is not a byte-ownership measurement.')

result = dict(
    schema=2, base=str(BASE), time_origin=origin_name, origin_monotonic=origin,
    page_size_bytes=PAGE_SIZE, page_size_source='run/cache-launch.json',
    task_type='reliability, offline descriptive analysis; no statistical/performance acceptance',
    collector_exit=document('resources/exit.json'),
    cache_gate=document('run/cache-gate.json'),
    cache_launch_vector_matches=launch_vector_matches,
    stage_state=document('state.json'),
    landmarks=landmarks,
    vmstat_deltas=deltas, node_compaction_evidence=compaction_files,
    node_compaction_summary=compaction_summary,
    failed_nvml=(dict(status=failed_query['status'], seconds=failed_query['seconds'],
                     worker_pid=failed_query['worker_pid'], api_events=api_events,
                     pending_api=pending) if failed_query else None),
    cleanup_signals=cleanup_signals,
    cuda_trace=dict(records=len(trace), began_calls=len(begins),
                    completed_calls=len(ends), slow_over_100ms=slow,
                    pending_calls=pending_calls),
    cgroup_summary=dict(samples=len(active_groups),
                        maximum_current_bytes=max((row['current_bytes'] for row in active_groups), default=None),
                        memory_max_bytes=sorted(set(row['max_bytes'] for row in active_groups)),
                        memory_events_maxima={key: max(row['events'][key] for row in active_groups)
                                              for key in (active_groups[0]['events'] if active_groups else [])},
                        maximum_swap_current_bytes=max((row['swap_current_bytes'] for row in active_groups), default=None),
                        swap_max_bytes=sorted(set(row['swap_max_bytes'] for row in active_groups))),
    normal_buddy_during_nvml_pending=blocked_orders,
    normal_buddy_during_failed_query=failed_query_orders,
    stack_hits=stack_hits, cache_timeline=cache, cgroup_timeline=cgroups,
    system_timeline=[summarize(row) for row in system],
    interpretation_rules=[
        'A large fall and later return in kernel misc-reclaimable stock can support a driver-pool lifecycle association when aligned with application lifecycle and independent kernel evidence. This counter alone neither identifies all ownership nor distinguishes reuse from shrinker reclamation.',
        'Absence of Normal-zone high-order blocks with sufficient free bytes supports a physical fragmentation hypothesis when the allocation order and compaction path are independently observed.',
        'A scope current value far below its maximum together with zero max/oom events does not support that ceiling as the trigger. Global reclaim can still increase scope pgscan/pgsteal.',
        'A full warm initial file vector does not imply that Linux retains those files throughout startup.',
        'Failed NVML must be attributed to the recorded pending API; do not assume an initialization timeout.',
        'Slow API returns after cleanup begins are observed progress under interruption, not successful startup acceptance.',
    ],
    limitations=[
        'One stage per report. Acceptance must consider guard, application and collector outcomes; this report does not accept a runtime repair or establish statistical benefit.',
        'PSI unavailable because running kernel CONFIG_PSI is not set; missing values are not zero pressure.',
        'System samples span non-atomic reads. File cachestat scans are not atomic whole-cache snapshots; each start/end timestamp is retained.',
        'Skipped nominal sampling slots are recorded by collector_exit; sampled counters and stacks cannot rule out shorter events.',
        'Global counters include unrelated host activity. Application trace does not include driver-internal allocations.',
        'Memcg current does not represent total physical device/host memory or preexisting file-cache ownership.',
        'Stacks identify paths and waits, not the exact RW-lock holder or causality between specific threads.',
        'CLEANUP_FAILED/ENV_ABORT is monitor failure, not proof of CUDA OOM. Completed allocation return codes and cleanup overlap are reported separately.',
        'Driver-pool ownership/order attribution needs the separately captured kernel callback-address identity and trace.',
    ],
    source_sha256=SOURCES,
    seal_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    analysis_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())

destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open('x') as output:
    output.write(json.dumps(result, indent=2) + '\n')
print(json.dumps(dict(output=str(destination),
                      source_files_verified=len(SOURCES),
                      system_samples=len(system), cache_samples=len(cache),
                      model_observed=bool(model_rows),
                      full_hot_launch_vector_matches=launch_vector_matches)))
