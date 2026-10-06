"""Independent review arithmetic over closed records; no project imports/tests."""
from pathlib import Path
import json, hashlib, time
R=Path(__file__).resolve().parent
x=json.loads((R/'observer-result-resource-review-extract.json').read_bytes())
ab=(R/'observer-analysis.json').read_bytes();a=json.loads(ab)
assert hashlib.sha256(ab).hexdigest()=='b94ea7185510563a29497d53933afb3487d0246a83d0c539d3514dac795ce73a'
sources={str(R/'observer-resources.json'):x['source_sha256'],str(R/'observer-analysis.json'):hashlib.sha256(ab).hexdigest()}
errors=('plans_failed','plans_scope_mismatch','claim_errors','read_errors','commit_errors','duplicate_claims','counter_overflow','entry_candidate_unclaimed','entry_candidate_to_read_other')
parts=('entry_candidate_to_mirror','entry_candidate_to_l2','entry_candidate_to_read_direct_active','entry_candidate_to_read_direct_published','entry_candidate_to_read_other','entry_candidate_unclaimed')
def direct(c):return c['entry_candidate_to_read_direct_active']+c['entry_candidate_to_read_direct_published']
def contract(c,decode):
 assert all(type(v)==int and 0<=v<2**64 for v in c.values())
 assert all(c[k]==0 for k in errors)
 assert c['plans_complete']==c['plans_started']
 assert c['planned_loads']==c['committed_loads']==sum(c[k] for k in ('source_l2','source_mirror','source_read'))
 assert c['entry_mirror_candidate']==sum(c[k] for k in parts)
 assert c['source_mirror']==c['entry_candidate_to_mirror']+c['source_mirror_outside_entry_candidates']
 assert c['writeback_reservations']==c['writeback_published']+c['writeback_aborted']
 assert c['samples_confirmed_total']==direct(c)
 if decode:assert c['source_mirror_outside_entry_candidates']==c['entry_claimed_mirrors']==0
counts={'http_records':0,'on_http_records':0,'phase_layers':0,'decode_layers':0,'source_log_records_equal':0};witnesses=[];summary=[];phase_totals={}
for name,g in x['groups'].items():
 items=g['supply']['requests'];enabled=g['supply']['observer_enabled'];last=None
 log=next(Path(p) for p in g['decision_source_sha256'] if p.endswith('/http/server.log'))
 raw=log.read_bytes();dig=hashlib.sha256(raw).hexdigest();assert dig==g['decision_source_sha256'][str(log)];sources[str(log)]=dig
 logged=[json.loads(line[len(b'[q4t][offload_diag] '):]) for line in raw.splitlines() if line.startswith(b'[q4t][offload_diag] ')]
 assert logged==[i['phase_record'] for i in items];counts['source_log_records_equal']+=len(items)
 for k,item in enumerate(items):
  counts['http_records']+=1;p=item['phase_record'];snap=p['snapshots'];start,bound,end=snap[0],snap[1],snap[-1]
  assert [s['event'] for s in (start,bound,end)]==['prefill_begin','prefill_end_decode_begin','inference_end']
  assert p['decode_forwards_completed']==item['actual_output']-1==end['decode_forward_count']
  if last is not None:assert start['stats']==last['stats'] and start['layers']==last['layers']
  last=end
  assert all(len(s['layers'])==48 and [z['layer'] for z in s['layers']]==list(range(48)) for s in (start,bound,end))
  if not enabled:
   assert 'supply_observer_serialization' not in p and item['observer_additional_json_bytes']==0
   assert all('supply_observer' not in l for s in (start,bound,end) for l in s['layers'])
   continue
  counts['on_http_records']+=1
  payload=sum(len(json.dumps(l['supply_observer'],separators=(',',':'),ensure_ascii=True).encode()) for s in (start,bound,end) for l in s['layers'])
  assert p['supply_observer_serialization']=={'bytes':payload,'limit_bytes':1048576,'complete':True}
  assert 0<item['observer_additional_json_bytes']<=1048576
  for phase,left,right in [('prefill',start,bound),('decode',bound,end)]:
   decode=phase=='decode';obs=item['supply_observer'][phase];tot={z:0 for z in obs['counters']}
   stats={z:right['stats'][z]-left['stats'][z] for z in ('loads','l2_hits','l2_misses','mirror_hits','shape_single_misses','l2_shape_single_hits','l2_shape_single_misses')}
   assert all(item[phase]['stats'][z]==v for z,v in stats.items())
   selected_all=[]
   for n,(la,lb,lr) in enumerate(zip(left['layers'],right['layers'],obs['layers'])):
    counts['phase_layers']+=1;counts['decode_layers']+=int(decode)
    ca,cb=la['supply_observer']['counters'],lb['supply_observer']['counters'];c={z:cb[z]-ca[z] for z in cb}
    assert lr['layer']==n and c==lr['counters'];contract(c,decode)
    assert lb['l2_clock']-la['l2_clock']==lr['l2_clock_delta']
    if decode:assert c['plans_complete']==lb['slot_clock']-la['slot_clock']==item['actual_output']-1 and lr['l2_clock_delta']==c['source_l2']+c['source_read']
    else:assert lr['l2_clock_delta']>=c['source_l2']+c['source_read']
    old={z['witness_seq']:z for z in la['supply_observer']['samples']};new=lb['supply_observer']['samples']
    selected=[s for s in new if s['witness_seq']>ca['samples_confirmed_total']]
    assert selected==lr['retained_phase_samples'] and c['samples_confirmed_total']-len(selected)==lr['confirmed_phase_samples_not_retained']
    assert len(new)==min(4,cb['samples_confirmed_total']) and cb['samples_overwritten']==max(0,cb['samples_confirmed_total']-4)
    for s in new:
     if s['witness_seq'] in old:assert s==old[s['witness_seq']]
    for s in selected:
     assert la['slot_clock']<s['plan_clock']<=lb['slot_clock'] and s['layer']==n
     assert 0<s['reserve_seq']<s['claim_seq']<s['publication_seq_or_zero']
     assert s['state_at_claim']=='active_reservation' and s['publication_final_outcome']=='published'
     assert s['source_task']!=s['overwriting_task'] and s['expert']!=s['GPU_victim']
     assert not s['entry_L2_present'] and s['entry_mirror_candidate'] and s['actual_source']=='READ'
     assert s['read_ok'] and s['commit_ok'] and s['plan_complete']
     if decode and name in a['cells']:witnesses.append({'group':name,'position':k,**s})
    for z,v in c.items():tot[z]+=v
   assert tot==obs['counters'];contract(tot,decode)
   assert tot['planned_loads']==stats['shape_single_misses'] and tot['source_l2']==stats['l2_shape_single_hits'] and tot['source_read']==stats['l2_shape_single_misses']
   assert stats['loads']==stats['l2_hits']+stats['l2_misses']+stats['mirror_hits']
   if decode:assert tot['source_read']==stats['l2_misses'] and tot['source_mirror']==stats['mirror_hits'] and tot['source_l2']==stats['l2_hits']
   phase_totals.setdefault(name,{}).setdefault(phase,[]).append(direct(tot))
   if name in a['cells']:
    row=a['cells'][name][k];assert row['observer_'+phase+'_counters']==tot
    if decode:
     assert row['layers']==obs['layers']
     assert row['direct_loss_read_fraction']==(direct(tot)/tot['source_read'] if tot['source_read'] else None)
     assert row['direct_loss_candidate_fraction']==(direct(tot)/tot['entry_mirror_candidate'] if tot['entry_mirror_candidate'] else None)
     summary.append({'group':name,'position':k,'plans':tot['plans_complete'],'entry_candidate_occurrences_across_plans':tot['entry_mirror_candidate'],'mirrors':tot['source_mirror'],'reads':tot['source_read'],'GPUloads':tot['committed_loads'],'direct_losses':direct(tot),'retained':sum(len(z['retained_phase_samples']) for z in obs['layers']),'not_retained':sum(z['confirmed_phase_samples_not_retained'] for z in obs['layers'])})
assert witnesses==a['retained_decode_witnesses'] and len(witnesses)==a['instrumented_decode_direct_read_losses_total']==22
for k,report in enumerate(a['instrumented_S_L_accounting']):
 s=a['cells']['s02-as-on'][k];l=a['cells']['s03-al-on'][k]
 for n in range(49):
  cs=s['observer_decode_counters'] if n==48 else s['layers'][n]['counters'];cl=l['observer_decode_counters'] if n==48 else l['layers'][n]['counters'];out=report if n==48 else report['layers'][n]
  mirror=cs['source_mirror']-cl['source_mirror'];candidate=cs['entry_mirror_candidate']-cl['entry_mirror_candidate'];loss=direct(cl)-direct(cs);to_l2=cl['entry_candidate_to_l2']-cs['entry_candidate_to_l2'];gpu=cl['committed_loads']-cs['committed_loads'];l2=cl['source_l2']-cs['source_l2'];reads=cl['source_read']-cs['source_read']
  assert mirror==candidate+loss+to_l2 and reads==gpu-l2+mirror
  assert all(out[key]==value for key,value in [('mirror_deficit_S_minus_L',mirror),('entry_candidate_difference_S_minus_L',candidate),('direct_loss_difference_L_minus_S',loss),('candidate_to_L2_difference_L_minus_S',to_l2),('GPU_load_difference_L_minus_S',gpu),('L2_hit_difference_L_minus_S',l2),('software_read_difference_L_minus_S',reads)])
result={'schema':1,'passed':True,'source_sha256':sources,'checks':counts,'rows':summary,'all_on_phase_direct_loss_counts':phase_totals,'decode_witnesses':witnesses,'totals':{key:sum(row[key] for row in summary) for key in ('plans','entry_candidate_occurrences_across_plans','mirrors','reads','GPUloads','direct_losses','retained','not_retained')},'contrast_aggregate_checks':4,'contrast_layer_checks':192,'recorded_t':time.time()}
with (R/'observer-result-arithmetic-review.json').open('x') as f:json.dump(result,f,indent=2,allow_nan=False);f.write('\n')
# Keep only the compact resource integrity review after using its embedded records.
for g in x['groups'].values():g.pop('supply');g.pop('decision_source_sha256')
with (R/'observer-result-resource-review-extract.json').open('w') as f:json.dump(x,f,indent=2,allow_nan=False);f.write('\n')
print(json.dumps({k:result[k] for k in ('passed','checks','totals','all_on_phase_direct_loss_counts','contrast_aggregate_checks','contrast_layer_checks')}))
print('compact_resource_bytes',(R/'observer-result-resource-review-extract.json').stat().st_size)
