import sqlite3,json,collections,hashlib
from pathlib import Path
root=Path('.q4t-work/hc-up-cause-20261016'); summaries={}
fields=['launchType','cacheConfig','registersPerThread','gridX','gridY','gridZ','blockX','blockY','blockZ','staticSharedMemory','dynamicSharedMemory','localMemoryPerThread','localMemoryTotal','sharedMemoryExecuted','sharedMemoryLimitConfig','clusterX','clusterY','clusterZ','clusterSchedulingPolicy','maxPotentialClusterSize','maxActiveClusters','sharedMemoryRequestedPercentage','tensorSizeMinusOneElements']
for v in ['A','B']:
 p=root/v/'context-204800/trace.sqlite';c=sqlite3.connect(f'file:{p}?mode=ro',uri=True);names=dict(c.execute('select id,value from StringIds'))
 end=c.execute('select end from CUPTI_ACTIVITY_KIND_MEMCPY where bytes=496640 and copyKind=2').fetchall();assert len(end)==1
 counts=collections.Counter();streams=collections.defaultdict(list)
 for row in c.execute('select streamId,demangledName,'+','.join(fields)+' from CUPTI_ACTIVITY_KIND_KERNEL where end<=? order by start',(end[0][0],)):
  stream,name,*values=row;key=json.dumps([names[name],*values]);counts[key]+=1;streams[stream].append(key)
 summaries[v]={'rows':sum(counts.values()),'counts':counts,'streams':{k:hashlib.sha256('\n'.join(s).encode()).hexdigest() for k,s in streams.items()},'stream_metadata':list(c.execute('select streamId,priority,flag from TARGET_INFO_CUDA_STREAM')),'missing_address_data':not bool(c.execute("select name from sqlite_master where type='table' and name like '%MEMORY%'").fetchall())}
a,b=summaries['A']['counts'],summaries['B']['counts'];diff=[{'signature':json.loads(k),'A':a[k],'B':b[k]} for k in a.keys()|b.keys() if a[k]!=b[k]]
for v in summaries:del summaries[v]['counts']
result={'fields':fields,'sides':summaries,'resource_count_differences':diff,'stream_sequence_hash_multiset_equal':sorted(summaries['A']['streams'].values())==sorted(summaries['B']['streams'].values()),'limitation':'A timing contaminated; only immutable launch configurations audited; no kernel parameters, allocation addresses, physical placement or executed instruction counts recorded'}
Path('.q4t-work/hc-up-launch-audit-20261017/result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
