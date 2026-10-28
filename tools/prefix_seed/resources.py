"""Offline physical-RAM, snapshot IO and accepted-work accounting."""
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/prefix-seed-20261028'
spec = importlib.util.spec_from_file_location(
    'session_resources', ROOT / 'tools/session_cache/analyze_resources.py')
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)


def main():
    phase = BASE / ('phase-' + sys.argv[1])
    result = existing.summarize(phase / 'run')
    for name, service in result['services'].items():
        raw = (phase / 'run' / name / 'server.log').read_text().splitlines()
        shared = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in raw
                  if line.startswith('[prefix-cache] request=')]
        service['prefix_events'] = shared
        service['prefix_disk_peak_bytes'] = max(
            (int(row['bytes']) for row in shared), default=0)
        service['prefix_save_wall_ms'] = sum(float(r['ms']) for r in shared)
        service['prefix_save_failures'] = [r for r in shared
                                           if r.get('error') != 'none']
        service['restores_by_source'] = {
            source: sum(r.get('source') == source and r.get('event') == 'restore'
                        and int(r.get('prefix', 0)) > 0
                        for r in service['session_events'])
            for source in ['session', 'shared']}
        service['combined_disk_upper_bound_bytes'] = (
            service['disk_peak_bytes'] + service['prefix_disk_peak_bytes'])
    preparation = phase / 'io-preparation.json'
    if preparation.exists():
        result['io_preparation'] = json.loads(preparation.read_text())
    result['limits'] += [
        'Pool byte fields are cumulative resident SSD bytes, not per-save bytes.',
        'Sum of separate pool maxima is a bound, not a simultaneous observation.',
        'Prefix save wall time starts under model mutex and is in HTTP latency.',
        'No isolated GPU-duration or allocation-delta measurement.',
        'Fixed IO preparation precedes the stage sampler: no sampled RAM peak '
        'for it. Source allocates an 8 MiB mmap plus a temporary 8 MiB payload, '
        'in addition to Python and filesystem memory.']
    output = BASE / ('resources-' + sys.argv[1] + '.json')
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({name: dict(memory=s['memory'],
        prefix_disk=s['prefix_disk_peak_bytes'], session_disk=s['disk_peak_bytes'],
        prefix_save_ms=s['prefix_save_wall_ms'], restores=s['restores_by_source'])
        for name, s in result['services'].items()}, indent=2))


if __name__ == '__main__':
    main()
