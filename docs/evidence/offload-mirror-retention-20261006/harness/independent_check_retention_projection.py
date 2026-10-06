# Independent check of the documentation projection against already-loaded data.
p=R/'retention-summary.json';projection_bytes=p.read_bytes();projection_sha=hashlib.sha256(projection_bytes).hexdigest()
assert projection_sha=='06bf0f299000a161d0bda3bfeafc39e6753787d7792247f41f4633688f2875c1'
projection=json.loads(projection_bytes)
for key,value in projection.items():
    if key not in ('requests','comparisons','source_sha256','projection_limits'):assert value==analysis[key],key
assert len(projection['requests'])==8 and len(projection['comparisons'])==4
for row,original,independent in zip(projection['requests'],analysis['requests'],review_compact['rows']):
    for key in ('group','position','input_tokens','output_tokens','complete','exact_gpu_endpoint_checked','iterator_exhausted','counters','supply','observer_counters'):assert row[key]==original[key]
    opp=[layer['opportunities'] for layer in original['layers']]
    assert row['opportunity_totals']=={key:sum(value[key] for value in opp) for key in ('plan_count','missing_total','supported_missing_total','potential_candidate_capacity','observed_candidates')}
    assert row['support_counts']=={key:sum(value['support_counts'][key] for value in opp) for key in ('entry_only','prior_victim_only','entry_and_prior_victim','unsupported')}
    for field in ('latest_prior_victim_plan_gap_histogram','strictly_intervening_victims_histogram'):
        keys=set().union(*(set(value[field]) for value in opp))
        assert row[field]=={key:sum(value[field].get(key,0) for value in opp) for key in keys}
    events=[event for value in opp for event in value['entry_mirror_first_events']]
    for category in ('gpu_only','l2_only','both','sole'):
        selected=[event for event in events if event['entry_category']==category]
        expected_category={'entries':len(selected),'first_miss_observed':sum(e['first_gpu_miss_plan'] is not None for e in selected),'first_eviction_observed':sum(e['first_gpu_eviction_plan'] is not None for e in selected),'eviction_before_first_miss':sum(e['first_gpu_eviction_plan'] is not None and e['first_gpu_miss_plan'] is not None and e['first_gpu_eviction_plan']<e['first_gpu_miss_plan'] for e in selected)}
        assert row['entry_mirror_events_by_category'][category]==expected_category
    for index,(endpoint,actual) in enumerate(zip(row['endpoints'],original['endpoints'])):
        assert endpoint['event']==actual['event']
        assert endpoint['counts']==independent['endpoints_counts'][index]
        assert endpoint['per_layer_counts']==[{'layer':l['layer'],**l['counts']} for l in actual['layers']]
for row,original,independent in zip(projection['comparisons'],analysis['comparisons'],review_compact['comparisons']):
    for key,value in original.items():
        if key!='layers':assert row[key]==value
    assert row['layers']==[{key:value for key,value in l.items() if key!='per_plan'} for l in original['layers']]
    assert row['miss_counts']==independent['miss_count_totals']
    assert row['capacities']==independent['capacity_totals']
assert projection['source_sha256'][str(R/'retention-analysis.json')]==expected['retention-analysis.json']
assert projection['source_sha256'][str(R/'build_retention_summary.py')]==hashlib.sha256((R/'build_retention_summary.py').read_bytes()).hexdigest()
projection_review={'schema':1,'passed':True,'projection_sha256':projection_sha,'analysis_sha256':expected['retention-analysis.json'],'all_request_and_comparison_fields_compared':True,'requests':8,'classified_endpoints':24,'per_layer_intervals':192,'uses_already_loaded_analysis':True,'actual_trace_passes':0,'recorded_t':time.time()}
with (R/'result-projection-review.json').open('x') as f:json.dump(projection_review,f,indent=2);f.write('\n')
print('projection PASS',projection_sha)
print('k1 decode-entry counts',[(row['group'],row['decode_entry_counts']) for row in review_compact['rows'] if row['position']==1])
print('k1 token route gate',{k:review_compact['comparisons'][1][k] for k in ('same_input_tokens','same_input_length','route_equal','mixed_GPU_covered_and_sole_decode_entry_layers')})
