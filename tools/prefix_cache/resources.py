"""Offline unified-RAM, snapshot IO and accepted-work accounting."""
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('session_resources',ROOT/'tools/session_cache/analyze_resources.py')
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)


def main():
    base=ROOT/'.q4t-work/prefix-cache-20261028';phase=base/('phase-'+sys.argv[1])
    result=old.summarize(phase/'run')
    fields=lambda line:dict(re.findall(r'(\w+)=(\S+)',line))
    for name,service in result['services'].items():
        raw=(phase/'run'/name/'server.log').read_text().splitlines()
        shared=[fields(l) for l in raw if l.startswith('[prefix-cache] request=')]
        service['prefix_events']=shared
        service['prefix_disk_peak_bytes']=max((int(r['bytes']) for r in shared),default=0)
        service['prefix_save_wall_ms']=sum(float(r['ms']) for r in shared)
        service['prefix_save_failures']=[l for l in raw if l.startswith('[prefix-cache] request=') and 'error=none' not in l]
        service['restores_by_source']={source:sum(r.get('source')==source and r.get('event')=='restore' and int(r.get('prefix',0))>0 for r in service['session_events']) for source in ['session','shared']}
        service['snapshot_upper_bound_combined_disk_bytes']=service['disk_peak_bytes']+service['prefix_disk_peak_bytes']
    result['limits'] += ['Two snapshot pool maxima are reported separately; their sum is a bound, not a simultaneous SSD observation.',
        'Shared checkpoint save time is measured under the model lock, excluding lock acquisition but included in HTTP latency.',
        'No isolated allocation delta or GPU-duration claim. Host/device/managed/page cache share physical RAM.',
        'Prefix match time is included with restore and pending-row repair; zero-hit events isolate the lookup path only.']
    destination=base/('resources-'+sys.argv[1]+'.json');destination.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({name:dict(memory=s['memory'],prefix_disk=s['prefix_disk_peak_bytes'],session_disk=s['disk_peak_bytes'],prefix_save_ms=s['prefix_save_wall_ms'],restores=s['restores_by_source']) for name,s in result['services'].items()},indent=2))


if __name__=='__main__':main()
