"""Read archived snapshot saves; never launch inference or a storage workload."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/session-preallocate-20261029'
ARCHIVE = BASE.with_name(BASE.name + '-archive')
ARCHIVE_SHA = '7f2c81f7ed0c714e1c3e5f8e573f944635fde64414530219cfcc299bffc95e46'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fields(line):
    return dict(re.findall(r'(\w+)=(\S+)', line))


def stat(raw):
    values = raw.rpartition(')')[2].split()
    return dict(state=values[0], start=int(values[19]))


def counters(raw):
    return {k: int(v) for k, v in
            (line.split(':') for line in raw.splitlines())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(output.is_relative_to(p) for p in (BASE, ARCHIVE)):
        raise ValueError('Never overwrite frozen evidence')
    if output.exists():
        raise ValueError('Use a new review output')
    if sha(ARCHIVE / 'manifest.json') != ARCHIVE_SHA:
        raise ValueError('Archive manifest changed')
    manifest = json.loads((ARCHIVE / 'manifest.json').read_text())
    bindings = {}

    def bind(path):
        relative = str(path.relative_to(BASE))
        digest = sha(path)
        if digest != manifest['files']['raw/' + relative]['sha256']:
            raise ValueError('Archived input changed: ' + relative)
        bindings[relative] = dict(sha256=digest, bytes=path.stat().st_size)
        return path

    def read(path):
        return json.loads(bind(path).read_text())

    plan = read(BASE / 'plan.json')
    decision = read(BASE / 'decision.json')
    if not (decision['evidence_valid'] and decision['measurement_complete']):
        raise ValueError('Prior evidence not complete/valid')
    if decision['engineering_decision'] != 'NO_GO':
        raise ValueError('Do not rewrite prior NO_GO')
    source = {}
    for name in ['src/server/session_cache.cpp',
                 'src/server/chat_generation.cpp',
                 'include/q4t/server/session_save_timing.h',
                 'tools/prefix_seed/precondition.py']:
        digest = sha(ROOT / name)
        if digest != plan['files'][str(ROOT / name)]:
            raise ValueError('Changed dependency: ' + name)
        source[name] = digest
    cases = []
    for step in plan['steps']:
        if step['kind'] != 'block':
            continue
        folder = BASE / 'run' / step['name']
        events = [json.loads(l) for l in
                  bind(folder / 'events.jsonl').read_text().splitlines()]
        pid = str(next(e['pid'] for e in events
                       if e['event'] == 'server_started'))
        timings = {(r['request'], r['pool']): r for r in
                   read(folder / 'timing-audit.json')['records']}
        for case in read(folder / 'results.json'):
            row = case['rows'][0]
            t = timings[row['response_id'], 'private']
            if int(t['pwrite_calls']) != 598 or t['valid'] != '1':
                raise ValueError('Changed timing contract')
            wall, cpu = float(t['pwrite_ms']), float(t['pwrite_cpu_ms'])
            slow_bound = int(t['slow_writes']) * float(t['max_write_ms'])
            cases.append(dict(service=step['name'], head=step['head'],
                preallocate=step['preallocate'], shared=case['shared'],
                request=row['response_id'], pid=pid, http_end=row['end'],
                timing=t, samples=[],
                child_start_gap=min(r['start'] for r in case['rows'][1:])
                                - row['end'],
                pwrite_mean_ms=wall / int(t['pwrite_calls']),
                pwrite_non_cpu_seconds=(wall - cpu) / 1000,
                slow_write_time_upper_bound_ms=slow_bound,
                at_most_50ms_time_lower_bound_ms=max(0, wall - slow_bound),
                at_most_50ms_time_lower_bound_fraction=max(0, 1-slow_bound/wall)))
    telemetry = bind(BASE / 'run/unified-memory.jsonl')
    source_keys = set()
    thread_fields = set()
    for line in telemetry.open():
        sample = json.loads(line)
        source_keys.update(sample['source_values'])
        for case in cases:
            end, wall = case['http_end'], float(case['timing']['wall_ms']) / 1000
            if not end-wall-3 < sample['monotonic'] < end+2:
                continue
            proc = sample['processes'].get(case['pid'])
            if not proc or proc['observation'] != 'OK':
                raise ValueError('Missing live server observation')
            before, after = stat(proc['stat']), stat(proc['stat_after'])
            if before['start'] != after['start']:
                raise ValueError('PID reused')
            threads = {}
            for tid, item in proc['threads'].items():
                thread_fields.update(item)
                if item['observation'] != 'OK':
                    continue
                identity = stat(item['stat'])
                if identity['start'] != stat(item['stat_after'])['start']:
                    raise ValueError('TID reused')
                threads[tid] = dict(identity, sched=list(map(int,
                                               item['schedstat'].split())))
            vm = {k: int(v) for k, v in
                  (l.split() for l in sample['source_values']['vmstat']['raw']
                   .splitlines())}
            case['samples'].append(dict(t=sample['monotonic'],
                finished=sample['sample_finished_monotonic'],
                process_start=before['start'], io=counters(proc['io']),
                threads=threads, vm={k: vm[k] for k in
                    ['nr_dirty', 'nr_writeback', 'pswpin', 'pswpout',
                     'pgscan_direct', 'allocstall_normal']}))
    for case in cases:
        samples = case.pop('samples')
        first = next(i for i in range(1, len(samples)) if
                     samples[i]['io']['wchar'] - samples[i-1]['io']['wchar']
                     > 1 << 20)
        last = next(i for i in range(first, len(samples)) if
                    samples[i]['t'] > case['http_end'])
        rows = samples[first-1:last+1]
        left, right = rows[0], rows[-1]
        if len({r['process_start'] for r in rows}) != 1:
            raise ValueError('PID reused in envelope')
        delta = {k: right['io'][k]-left['io'][k] for k in left['io']}
        if abs(delta['wchar']-int(case['timing']['disk_bytes'])) > 65536:
            raise ValueError('Envelope does not cover exactly one snapshot')
        candidates = {}
        for tid, item in left['threads'].items():
            live = [r for r in rows if tid in r['threads']]
            if len({r['threads'][tid]['start'] for r in live}) != 1:
                raise ValueError('Inferred writer identity changed')
            cpu = (live[-1]['threads'][tid]['sched'][0]-item['sched'][0])/1e9
            candidates[tid] = (cpu, live)
        tid = max(candidates, key=lambda key: candidates[key][0])
        cpu, live = candidates[tid]
        a, b = live[0]['threads'][tid], live[-1]['threads'][tid]
        case['envelope'] = dict(start=left['t'], end=right['t'],
            io_deltas=delta, seconds=right['t']-left['t'],
            max_collection_seconds=max(r['finished']-r['t'] for r in rows),
            vm_deltas={k: right['vm'][k]-left['vm'][k] for k in
                ['pswpin', 'pswpout', 'pgscan_direct', 'allocstall_normal']},
            dirty_pages_range=[min(r['vm']['nr_dirty'] for r in rows),
                               max(r['vm']['nr_dirty'] for r in rows)],
            writeback_pages_range=[min(r['vm']['nr_writeback'] for r in rows),
                                   max(r['vm']['nr_writeback'] for r in rows)])
        case['inferred_writer'] = dict(tid=tid, start_ticks=a['start'],
            cpu_seconds=cpu, observed_until=live[-1]['t'],
            runqueue_seconds=(b['sched'][1]-a['sched'][1])/1e9,
            sampled_states=dict(Counter(r['threads'][tid]['state']
                                        for r in live[1:])))
    prep = [json.loads(l) for l in
            bind(BASE/'io-preparation.jsonl').read_text().splitlines()]
    report = dict(source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        prior_decision='NO_GO', candidate_sha256=plan['candidate_sha256'],
        archive_manifest_sha256=ARCHIVE_SHA, archived_input_bindings=bindings,
        source_files=source, main_saves=cases, preparation=prep,
        observed_telemetry_fields=sorted(source_keys),
        observed_thread_fields=sorted(thread_fields),
        limits=[
            'No new model, HTTP, benchmark, device tracing or SSD workload.',
            'Save has no TID/timestamps; envelope includes tail and logs.',
            'Writer is inferred by CPU; last live sample can precede exit.',
            'Runqueue delay is CPU scheduling, not block-device queue wait.',
            'D-state samples do not identify a syscall or measure its duration.',
            'Non-CPU syscall time includes blocking and runnable delay.',
            '50ms bounds use rounded aggregate timings, not a per-call trace.',
            'Process IO accounting is not physical NAND traffic.',
            'No historical diskstats, request lifetimes, NVMe health or stacks.',
            'Current host inventory cannot fill historical missing observations.'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(dict(output=str(output), sha256=sha(output),
                         bound_files=len(bindings), main_saves=len(cases))))


if __name__ == '__main__':
    main()
