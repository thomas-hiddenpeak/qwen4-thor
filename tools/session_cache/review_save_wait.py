"""Offline review of the two completed C services; never launches a model.

The write envelope is deliberately NOT labelled an exact Save() interval.
It starts one sample before the first >1 MiB write delta near the save and
ends at the first sample after HTTP completion. Small logs and the decode
tail are included. Per-thread CPU ends at its last live sample, not at exit.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/mtp-prefill-release-20261029'
ARCHIVE = ROOT / '.q4t-work/mtp-prefill-release-20261029-archive'
SOURCE = 'c9d4475d1169fa653a092f22f970253fd4bcf043'
ARMS = ('00-full-off', '01-full-on')
EXPECTED_BYTES = 4159963136


def fields(line):
    return dict(re.findall(r'(\w+)=(\S+)', line))


def counters(raw):
    return {key: int(value) for key, value in
            (line.split(':') for line in raw.splitlines())}


def stat(raw):
    parts = raw.rpartition(')')[2].split()
    return dict(state=parts[0], user_ticks=int(parts[11]),
                system_ticks=int(parts[12]), start_ticks=int(parts[19]))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(BASE) or output.is_relative_to(ARCHIVE):
        raise ValueError('Do not mutate original evidence or its archive')
    archive_sha = sha(ARCHIVE / 'manifest.json')
    if archive_sha != '7b37aaa4ca7caba1c8aaa3cfe565323787bac2b87df7870153519ed000967ed5':
        raise ValueError('Archive manifest changed')
    manifest = json.loads((ARCHIVE / 'manifest.json').read_text())
    bindings = {}

    def bind(path):
        relative = str(path.relative_to(BASE))
        digest = sha(path)
        expected = manifest['files']['raw/' + relative]['sha256']
        if digest != expected:
            raise ValueError(f'Archived input changed: {relative}')
        bindings[relative] = dict(sha256=digest, bytes=path.stat().st_size)
        return path

    def read(path):
        return json.loads(bind(path).read_text())

    run = BASE / 'phase-c/run'
    plan = read(BASE / 'phase-c/plan.json')
    if plan['runtime_source_commit'] != SOURCE:
        raise ValueError('Runtime identity changed')
    state = read(run / 'state.json')
    if state['status'] != 'STOPPED' or state['completed'] != list(ARMS):
        raise ValueError('Unexpected completed-service boundary')
    cases, all_mains = {}, []
    for arm in ARMS:
        folder = run / arm
        if not read(folder / 'exit.json')['http_output_checks_passed']:
            raise ValueError('Incomplete HTTP service')
        if read(folder / 'server-audit.json')['finalization']['status'] != 'PASS':
            raise ValueError('Incomplete allocation finalization')
        for audit in ('session-audit.json', 'prefix-audit.json'):
            if not read(folder / audit)['passed']:
                raise ValueError('Cache audit failed')
        events = [json.loads(line) for line in
                  bind(folder / 'events.jsonl').read_text().splitlines()]
        pid = str(next(e['pid'] for e in events if e['event'] == 'server_started'))
        lines = bind(folder / 'server.log').read_text().splitlines()
        saves = {fields(line)['request']: fields(line) for line in lines
                 if line.startswith('[session-cache]') and 'event=save' in line}
        ends = {fields(line)['id']: fields(line) for line in lines
                if line.startswith('[q4t][request]') and 'event=end' in line}
        for case in read(folder / 'results.json'):
            row = case['rows'][0]
            rid = row['response_id']
            saved = saves[rid]
            record = dict(service=arm, scenario=case['id'], request=rid,
                          save_seconds=float(saved['ms']) / 1000,
                          prefix=int(saved['prefix']),
                          model_lock_ms=float(ends[rid]['model_lock_ms']),
                          main_latency=row['latency'], ttft=row['ttft'],
                          transaction_seconds=case['makespan'])
            all_mains.append(record)
            if case['id'] == 'repeat-code-8192':
                if int(saved['disk_bytes']) != EXPECTED_BYTES:
                    raise ValueError('First private snapshot changed')
                if min(r['start'] for r in case['rows'][1:]) <= row['end']:
                    raise ValueError('Children overlap main')
                cases[arm] = dict(record, pid=pid, end=row['end'],
                                  rows=case['rows'], samples=[])

    telemetry = bind(run / 'unified-memory.jsonl')
    hz = int(subprocess.check_output(['getconf', 'CLK_TCK'], text=True))
    for line in telemetry.open():
        sample = json.loads(line)
        for case in cases.values():
            t = sample['monotonic']
            if not case['end'] - case['save_seconds'] - 2 < t < case['end'] + 2:
                continue
            proc = sample['processes'].get(case['pid'])
            if not proc or proc['observation'] != 'OK':
                raise ValueError('Missing live process observation')
            before, after = stat(proc['stat']), stat(proc['stat_after'])
            if before['start_ticks'] != after['start_ticks']:
                raise ValueError('Process identity changed')
            threads = {}
            for tid, thread in proc['threads'].items():
                if thread['observation'] != 'OK':
                    continue
                ts = stat(thread['stat'])
                if ts['start_ticks'] != stat(thread['stat_after'])['start_ticks']:
                    raise ValueError('Thread identity changed')
                threads[tid] = dict(ts, schedstat=list(map(int,
                                                        thread['schedstat'].split())))
            case['samples'].append(dict(
                t=t, finished=sample['sample_finished_monotonic'],
                process=before, io=counters(proc['io']), threads=threads))

    output_cases = []
    for case in cases.values():
        samples = case.pop('samples')
        first = next(i for i in range(1, len(samples))
                     if samples[i]['io']['wchar'] -
                     samples[i - 1]['io']['wchar'] > 1 << 20)
        last = next(i for i in range(first, len(samples))
                    if samples[i]['t'] > case['end'])
        rows = samples[first - 1:last + 1]
        left, right = rows[0], rows[-1]
        if len({s['process']['start_ticks'] for s in rows}) != 1:
            raise ValueError('PID reused')
        deltas = {key: right['io'][key] - left['io'][key]
                  for key in left['io']}
        if abs(deltas['wchar'] - EXPECTED_BYTES) > 65536:
            raise ValueError('Envelope does not cover exactly one large save')
        # Identify the dominant CPU thread during the serial transfer. This is
        # an inference, not an instrumented request->TID association.
        candidate_threads = {}
        for tid, thread in left['threads'].items():
            available = [s for s in rows if tid in s['threads']]
            end_thread = available[-1]['threads'][tid]
            if end_thread['start_ticks'] != thread['start_ticks']:
                raise ValueError('TID reused')
            cpu = (end_thread['schedstat'][0] - thread['schedstat'][0]) / 1e9
            candidate_threads[tid] = (cpu, available)
        tid = max(candidate_threads, key=lambda t: candidate_threads[t][0])
        cpu, available = candidate_threads[tid]
        ts0 = available[0]['threads'][tid]['schedstat']
        ts1 = available[-1]['threads'][tid]['schedstat']
        series = []
        for a, b in zip(rows, rows[1:]):
            row = dict(monotonic=b['t'], seconds_to_http_end=b['t'] - case['end'],
                       interval_seconds=b['t'] - a['t'],
                       write_bytes_delta=b['io']['write_bytes'] - a['io']['write_bytes'],
                       wchar_delta=b['io']['wchar'] - a['io']['wchar'])
            if tid in b['threads'] and tid in a['threads']:
                old, new = a['threads'][tid], b['threads'][tid]
                row.update(writer_state=new['state'],
                           writer_cpu_seconds=(new['schedstat'][0] - old['schedstat'][0]) / 1e9,
                           runqueue_seconds=(new['schedstat'][1] - old['schedstat'][1]) / 1e9)
            series.append(row)
        used = {key: (right['process'][key] - left['process'][key]) / hz
                for key in ('user_ticks', 'system_ticks')}
        http_rows = case.pop('rows')
        case['child_start_gap'] = min(r['start'] for r in http_rows[1:]) - case['end']
        case['child_group_seconds'] = (max(r['end'] for r in http_rows[1:]) -
                                       min(r['start'] for r in http_rows[1:]))
        output_cases.append(dict(case, process_start_ticks=left['process']['start_ticks'],
            logical_snapshot_bytes=EXPECTED_BYTES,
            envelope=dict(start=left['t'], end=right['t'],
                wall_seconds=right['t'] - left['t'],
                max_sample_collection_seconds=max(s['finished'] - s['t'] for s in rows),
                user_seconds=used['user_ticks'], system_seconds=used['system_ticks'],
                process_cpu_seconds=sum(used.values()), io_deltas=deltas),
            inferred_writer=dict(tid=tid, start_ticks=left['threads'][tid]['start_ticks'],
                observed_start=available[0]['t'], observed_end=available[-1]['t'],
                observed_cpu_seconds=cpu, runqueue_seconds=(ts1[1] - ts0[1]) / 1e9,
                sampled_states=dict(Counter(s['threads'][tid]['state'] for s in available[1:]))),
            series=series))

    source_bindings = {}
    for name in ('src/server/session_cache.cpp', 'src/server/session_state.cpp',
                 'src/server/chat_generation.cpp', 'src/trace/sha256.cpp',
                 'include/q4t/server/request_diagnostics.h',
                 'include/q4t/server/session_cache.h'):
        original = subprocess.check_output(['git', 'show', f'{SOURCE}:{name}'], cwd=ROOT)
        if (ROOT / name).read_bytes() != original:
            raise ValueError(f'Read source differs from tested runtime: {name}')
        source_bindings[name] = hashlib.sha256(original).hexdigest()
    preparation = [json.loads(line) for line in
                   bind(BASE / 'phase-c/io-preparation.jsonl').read_text().splitlines()]
    report = dict(runtime_source=SOURCE, clk_tck=hz,
        candidate_sha256=plan['candidate_sha256'], archive_manifest_sha256=archive_sha,
        source_files=source_bindings, archived_input_bindings=bindings,
        completed_main_controls=all_mains, first_pair=output_cases,
        preparation_segments=preparation,
        limits=[
            'No new HTTP, GPU, benchmark, SSD write, or model execution.',
            'Original C remains STOPPED; incomplete subset is excluded.',
            'Save timestamps absent; sample envelope includes tail and logs.',
            'CPU time includes CUDA driver, SHA256, copying and kernel work.',
            'Last writer sample precedes exit; CPU is not exact full-save CPU.',
            'D-state samples do not measure D-state duration or identify syscall.',
            'Runqueue delay is not mutex or IO wait.',
            'Process write_bytes is host IO accounting, not physical NAND traffic.',
            'No syscall timing, device diskstats, NVMe temperature/GC or external IO attribution.',
            'No population or causal shared-toggle claim from one ordered pair.'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(output=str(output), sha256=sha(output),
                          bound_raw_files=len(bindings), cases=len(output_cases))))


if __name__ == '__main__':
    main()
