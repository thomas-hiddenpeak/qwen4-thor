"""Application-call lifetime ledger; async release != physical reclamation."""
import argparse
import collections
import json
from pathlib import Path

ALLOC={'cudaMalloc','cudaMallocHost','cudaHostAlloc','cudaMallocAsync'}
FREE={'cudaFree','cudaFreeHost','cudaFreeAsync'}


def audit(path):
    calls={};live={};records=[];normalized=[];errors=[];failed=[]
    current=collections.Counter();peak=collections.Counter();counts=collections.Counter()
    allocation_id=0;freed=0;async_freed=0;ready=False;ready_snapshot=None
    for line in path.read_text().splitlines():
        if 'serving on port' in line:
            ready=True
            ready_snapshot=dict(live=len(live),bytes=dict(current))
        marker='[memory-trace] '
        if marker not in line:continue
        record=json.loads(line.split(marker,1)[1])
        if record['seq']!=len(records):raise ValueError('non-contiguous trace sequence')
        records.append(record);ident=record['call'];api=record['api']
        if record['phase']=='begin':
            if ident in calls:raise ValueError('duplicate call id')
            calls[ident]=record;continue
        begin=calls.pop(ident)
        for key in ('api','bytes','tid','stream','flags','caller','backend'):
            if begin[key]!=record[key]:raise ValueError('call identity differs: '+key)
        counts[api]+=1
        if record['rc']:
            failed.append(record);continue
        ptr=record['ptr']
        if api in ALLOC and ptr:
            if ptr in live:errors.append(dict(reason='reuse while logically live',old=live[ptr],new=record))
            kind='host' if api in ('cudaMallocHost','cudaHostAlloc') else 'device'
            item=dict(ordinal=allocation_id,api=api,bytes=record['bytes'],ptr=ptr,
                      caller=record['caller'],kind=kind,backend=record['backend'],startup=not ready)
            live[ptr]=item;allocation_id+=1
            current[kind]+=record['bytes'];peak[kind]=max(peak[kind],current[kind])
            normalized.append(['alloc',item['ordinal'],api,record['bytes'],record['flags'],not ready])
        elif api in FREE and ptr:
            item=live.pop(ptr,None)
            if item is None:
                errors.append(dict(reason='unmatched application free',record=record));continue
            expected='host' if api=='cudaFreeHost' else 'device'
            if expected!=item['kind']:errors.append(dict(reason='wrong free kind',record=record))
            current[item['kind']]-=item['bytes'];freed+=1
            async_freed+=api=='cudaFreeAsync'
            normalized.append(['free',item['ordinal'],api,not ready])
    return dict(records=len(records),completed_calls=dict(counts),allocations=allocation_id,
        frees=freed,async_free_submissions=async_freed,peak_logical_bytes=dict(peak),
        live_logical_bytes=dict(current),remaining=list(live.values()),incomplete_calls=list(calls.values()),
        failed_calls=failed,errors=errors,ready_snapshot=ready_snapshot,
        normalized=normalized,scope='application linked API calls; library internal allocation excluded; async free is submission, not physical release')


def main():
    p=argparse.ArgumentParser();p.add_argument('log',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=audit(a.log);a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('records','allocations','frees','peak_logical_bytes','live_logical_bytes','ready_snapshot')}))
    print('errors',len(result['errors']),'incomplete',len(result['incomplete_calls']),'failed',len(result['failed_calls']))
    return bool(result['errors'])

if __name__=='__main__':raise SystemExit(main())
