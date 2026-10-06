"""Compact, prompt-free documentation projection of closed observer reports."""
import hashlib
import json
from pathlib import Path
import time
R=Path(__file__).resolve().parent
sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
a=json.loads((R/'observer-analysis.json').read_text())
z=json.loads((R/'observer-resources.json').read_text())
assert a['passed'] and z['audit_complete']
result={k:a[k] for k in ['schema','plan_sha256','runtime_source_commit','runtime_binary_sha256','service_count','request_count','diagnostic_request_count','contract_count','resource_envelope_count','decision','future_candidate','limits']}
result['recorded_t']=time.time()
result['request_rows']=[]
for group,rows in a['cells'].items():
 for row in rows:
  out={k:row[k] for k in ['position','actual_input','actual_output','http_metric','observer_additional_json_bytes']}
  out['group']=group
  out['decode_stats']=row['decode_stats']
  for key in ['observer_decode_counters','observer_prefill_counters','direct_loss_read_fraction','direct_loss_candidate_fraction']:
   if key in row:out[key]=row[key]
  result['request_rows'].append(out)
result['S_L_accounting']=[{k:v for k,v in row.items() if k!='layers'} for row in a['instrumented_S_L_accounting']]
result['off_on_descriptive_contrasts']=a['off_on_descriptive_contrasts']
result['retained_decode_witnesses']=a['retained_decode_witnesses']
reads=sum(x['observer_decode_counters']['source_read'] for x in result['request_rows'] if 'observer_decode_counters' in x)
result['observed_direct_frequency']={'direct_READ':a['instrumented_decode_direct_read_losses_total'],'software_READ':reads,'fraction':a['instrumented_decode_direct_read_losses_total']/reads,'scope':'Eight new observer-on decode requests only. Events are not independent service replicates. This is not a global counterfactual speed/read saving bound.'}
result['resources']={}
for group,row in z['groups'].items():
 i=row['inspection'];m=row['memory']
 result['resources'][group]={'counts':i['counts'],'PSI_status':i['PSI_status'],
  'PID_IO_unknown_records':len(i['process_io_unknown_exact']),
  'cgroup_unknown_records':len(i['cgroup_unknown_nonpsi_exact']),
  'client_envelopes':len(row['client_envelopes']),
  'memory':{k:m[k] for k in ['memory_max_bytes','charge_peak_bytes','observed_current_peak_bytes',
      'charge_peak_minus_max_bytes','observed_current_peak_minus_max_bytes','final_memory_events',
      'final_swap_current','final_swap_peak','scope']},
  'IO_windows':[{k:e[k] for k in ['label','pid_storage_read_bytes','pid_logical_rchar_bytes','cgroup_read_bytes_by_device','cgroup_vs_PID']} for e in row['client_envelopes']]}
result['resource_limits']=z['interpretation_limits']
result['whole_physical_RAM_54GB']=z['whole_physical_RAM_54GB']
result['source_sha256']={str(R/name):sha(R/name) for name in ['observer-analysis.json','observer-resources.json','observer-contracts-decision.json','build_delivery_summary.py']}
with (R/'observer-delivery-summary.json').open('x') as f:json.dump(result,f,indent=2);f.write('\n')
print({'bytes':(R/'observer-delivery-summary.json').stat().st_size,'direct_reads':result['observed_direct_frequency']})
