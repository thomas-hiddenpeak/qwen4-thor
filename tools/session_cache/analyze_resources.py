"""CPU-only accounting of completed session-cache telemetry and work logs."""
import argparse
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'existing_resources', ROOT / 'tools/mtp_suffix_agent/resources.py')
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)


def fields(line):
    return dict(re.findall(r'(\w+)=(\S+)', line))


def summarize(root):
    state = json.loads((root / 'state.json').read_text())
    if state['status'] == 'RUNNING':
        raise ValueError('Do not analyze live measurement files')
    services, windows = {}, []
    for log in sorted(root.glob('*/server.log')):
        directory = log.parent
        event_path = directory / 'events.jsonl'
        if not event_path.exists():
            continue
        events = [json.loads(line) for line in event_path.read_text().splitlines()]
        start = next(e['monotonic_seconds'] for e in events
                     if e['event'] == 'server_before_start')
        ends = [e['monotonic_seconds'] for e in events
                if e['event'] == 'server_after_shutdown']
        if not ends:
            raise ValueError('Service shutdown window missing')
        raw = log.read_text().splitlines()
        audit = dict(suffix=[fields(l) for l in raw if l.startswith('[suffix] ')],
            acceptance=[fields(l) for l in raw if l.startswith('[mtp-accept] ')])
        sessions = [fields(l) for l in raw
                    if l.startswith('[session-cache] request=')]
        service = dict(memory={}, cases={}, session_events=sessions,
            disk_peak_bytes=max((int(s.get('disk_bytes', 0)) for s in sessions), default=0),
            successful_restores=sum(s['event']=='restore' and int(s['prefix'])>0
                                    and s['error']=='none' for s in sessions),
            save_wall_ms=sum(float(s['ms']) for s in sessions if s['event']=='save'),
            restore_wall_ms=sum(float(s['ms']) for s in sessions if s['event']=='restore'),
            streamed_prefill=[fields(l) for l in raw if l.startswith('[mtp-prefill] ')],
            exit=json.loads((directory / 'exit.json').read_text()))
        services[directory.name] = service
        windows.append((start, max(ends), service['memory']))
        paths = sorted(directory.glob('repeat-*/result.json'))
        paths += sorted(directory.glob('context-*/result.json'))
        for path in paths:
            case = json.loads(path.read_text())
            if case.get('reused_from'):
                service['cases'][path.parent.name] = dict(
                    reused_from=case['reused_from'],
                    note='RAM and work attribution remain in original service')
                continue
            # The generic work accounting distinguishes CPU index wall times
            # from verify/extend row counts. Neither is an isolated GPU time.
            row = existing.workload(case, audit)
            row['memory'] = {}
            service['cases'][path.parent.name] = row
            windows.append((min(r['start'] for r in case['rows']),
                            max(r['end'] for r in case['rows']), row['memory']))
    overall = {}
    with (root / 'unified-memory.jsonl').open() as stream:
        for line in stream:
            sample = json.loads(line)
            existing.add_sample(overall, sample)
            for start, end, result in windows:
                if start <= sample['monotonic'] <= end:
                    existing.add_sample(result, sample)
    return dict(source=str(root), state=state, memory=overall, services=services,
        limits=['One-second physical-RAM observations cannot bound unsampled peaks.',
                'RSS, CUDA allocations and file cache are not added to physical RAM.',
                'Snapshot disk bytes are SSD, not additional resident RAM.',
                'Save wall time includes waiting for model mutex; restore wall time '
                'starts under that mutex. End-to-end TTFT includes both waits.',
                'GPU memcpy/internal pageable staging is included in host RAM '
                'observations; no isolated GPU section timing was collected.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.root)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
