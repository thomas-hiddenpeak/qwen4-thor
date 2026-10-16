import collections,json,re,sqlite3
from pathlib import Path
OUT=Path(__file__).resolve().parent
SOURCE=Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/hc-up-clean-trace-v2-20261017')
windows=json.loads((SOURCE/'device-audit.json').read_text());report={}
for v in 'AB':
 c=sqlite3.connect(f'file:{SOURCE/v/"context-204800/trace.sqlite"}?mode=ro',uri=True)
 names=dict(c.execute('select id,value from StringIds'))
 tables=[r[0] for r in c.execute("select name from sqlite_master where type='table' and name like 'CUPTI_ACTIVITY_%'")]
 counts={t:c.execute('select count(*) from '+t).fetchone()[0] for t in tables}
 ds=[r[0] for r in c.execute('select text from DIAGNOSTIC_EVENT')]
 produced=int(re.search(r'produced:\s*(\d+)',next(x for x in ds if 'events produced:' in x))[1])
 collected=int(re.search(r'collected:\s*(\d+)',next(x for x in ds if 'events collected:' in x))[1])
 selected=sum(counts['CUPTI_ACTIVITY_KIND_'+t] for t in ('KERNEL','RUNTIME','MEMCPY','MEMSET'))
 assert selected==collected
 ks=collections.Counter(x[0] for x in c.execute('select correlationId from CUPTI_ACTIVITY_KIND_KERNEL'))
 ids=[n for n,s in names.items() if 'Launch' in s];assert len(ids)==3
 lo=windows[v]['chunks'][0]['start_ns'];hi=windows[v]['chunks'][-1]['end_ns']
 missing=[];apis=collections.Counter();api_count=0;in_window=0
 for a,b,corr,n,rv,tid in c.execute('select start,end,correlationId,nameId,returnValue,globalTid from CUPTI_ACTIVITY_KIND_RUNTIME where nameId in ('+','.join(map(str,ids))+')'):
  api_count+=1;apis[corr]+=1
  if lo<=a<hi:in_window+=1
  if corr not in ks:missing.append({'start_ns':a,'end_ns':b,'correlation':corr,'api':names[n],'return':rv,'globalTid':tid,'entry_in_window':lo<=a<hi,'before_window':b<=lo,'after_window':a>=hi})
 orphan=[k for k in ks if k not in apis]
 thread_names={tid:names[n] for n,tid in c.execute('select nameId,globalTid from ThreadNames')}
 for row in missing:row['thread_name']=thread_names.get(row['globalTid'])
 report[v]={'table_counts':counts,'produced':produced,'collected':collected,'collected_equals_four_primary_tables':selected==collected,'produced_minus_all_exported_activity_rows':produced-sum(counts.values()),'launch_api_rows':api_count,'launch_entries_in_window':in_window,'kernel_rows':sum(ks.values()),'duplicate_kernel_correlations':sum(n>1 for n in ks.values()),'duplicate_launch_correlations':sum(n>1 for n in apis.values()),'kernel_correlations_without_launch_api':orphan,'launches_without_kernel':missing,'unmatched_launch_entries_in_window':sum(x['entry_in_window'] for x in missing)}
 print(v,{k:val for k,val in report[v].items() if k not in ('table_counts','launches_without_kernel')},flush=True)
 c.close()
report['limits']=['Correlation bijection only for recorded launches, cannot prove both API and kernel not jointly omitted','Counts do not establish producer counter semantics or forced flush for this capture','No GPU requests; no timing acceptance']
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
