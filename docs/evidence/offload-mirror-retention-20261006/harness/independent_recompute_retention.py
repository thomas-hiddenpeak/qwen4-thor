# Loaded into a read-only reviewer session holding the three once-read artifacts.
# No project imports, GPU replay, route parser, tests, model or raw-resource reads.
from collections import Counter

def review_classification(snapshot):
    gpu=set(snapshot['slot_experts'])-{ -1 }
    l2=set(snapshot['l2_experts'])-{ -1 }
    out={name:[] for name in ('gpu_only','l2_only','both','sole','empty_slots')}
    for slot,expert in enumerate(snapshot['mirror_experts']):
        if expert<0:out['empty_slots'].append(slot)
        else:
            label={(True,False):'gpu_only',(False,True):'l2_only',(True,True):'both',(False,False):'sole'}[(expert in gpu,expert in l2)]
            out[label].append(expert)
    out['mirror_cursor']=snapshot['mirror_cursor']
    out['counts']={name:len(out[name]) for name in ('gpu_only','l2_only','both','sole','empty_slots')}
    assert sum(out['counts'].values())==8
    return out

def review_counts(plan_rows):
    misses=sum(len(row['missing']) for row in plan_rows)
    lookups=10*len(plan_rows)
    return {'resolve_calls':len(plan_rows),'expert_lookups':lookups,'hits':lookups-misses,
            'misses':misses,'loads':misses,'evictions':sum(len(row['victims']) for row in plan_rows),
            'prefill_lookups':0,'decode_lookups':lookups,'prefill_misses':0,'decode_misses':misses}

def review_opportunities(entry, plan_rows, observed):
    classified=review_classification(entry)
    entry_ids={e for e in entry['mirror_experts'] if e>=0}
    all_evictions=[];support=[];latest={};gaps=Counter();between=Counter()
    categories=dict.fromkeys(('entry_only','prior_victim_only','entry_and_prior_victim','unsupported'),0)
    misses=0
    for index,plan in enumerate(plan_rows):
        needed,missing,victims=plan['needed'],plan['missing'],plan['victims']
        assert len(needed)==10 and len(set(needed))==10 and all(type(e)==int and 0<=e<512 for e in needed)
        assert missing==[e for e in needed if e in set(missing)] and len(set(missing))==len(missing)
        assert len(set(victims))==len(victims) and len(victims)<=len(missing) and not(set(victims)&set(needed))
        possible=entry_ids|{e for _,e in all_evictions}
        support.append(set(missing)&possible)
        misses+=len(missing)
        for expert in missing:
            origin=(expert in entry_ids,expert in latest)
            label={(True,False):'entry_only',(False,True):'prior_victim_only',(True,True):'entry_and_prior_victim',(False,False):'unsupported'}[origin]
            categories[label]+=1
            if expert in latest:
                previous=latest[expert]
                gaps[index-previous]+=1
                # Count all occupied victim events strictly between the two plans.
                between[sum(1 for at,_ in all_evictions if previous<at<index)]+=1
        all_evictions.extend((index,expert) for expert in victims)
        latest.update({expert:index for expert in victims})
    capacity=sum(min(8,len(s)) for s in support)
    assert 0<=observed<=capacity
    first=[]
    for slot,expert in enumerate(entry['mirror_experts']):
        if expert<0:continue
        eviction=next((i+1 for i,p in enumerate(plan_rows) if expert in p['victims']),None)
        miss=next((i+1 for i,p in enumerate(plan_rows) if expert in p['missing']),None)
        category=next(name for name in ('gpu_only','l2_only','both','sole') if expert in classified[name])
        first.append({'expert':expert,'mirror_slot':slot,'entry_category':category,
            'first_gpu_eviction_plan':eviction,'first_gpu_miss_plan':miss,
            'eviction_censored':eviction is None,'miss_censored':miss is None,'censor_after_plan':255})
    expected={'plan_count':255,'missing_total':misses,'supported_missing_total':sum(map(len,support)),
        'potential_candidate_capacity':capacity,'observed_candidates':observed,'support_counts':categories,
        'latest_prior_victim_plan_gap_histogram':{str(k):v for k,v in sorted(gaps.items())},
        'strictly_intervening_victims_histogram':{str(k):v for k,v in sorted(between.items())},
        'entry_mirror_first_events':first,'entry_classification':classified}
    return expected,support

def independent_recompute():
    assert analysis['passed'] is True and plans['passed'] is True and analysis['failure'] is None
    assert analysis['plans_sha256']==expected['decode-plans.json']
    assert analysis['plans_output']==str(R/'decode-plans.json')
    assert analysis['manifest_sha256']==plans['manifest_sha256']==execution['manifest_sha256']==expected['retention-inputs.json']
    assert analysis['scope_sha256']==plans['scope_sha256']==inp['scope_sha256']==execution['scope_sha256']
    esha=hashlib.sha256((R/'execution-plan.json').read_bytes()).hexdigest()
    assert analysis['execution_plan_sha256']==plans['execution_plan_sha256']==esha=='fd413fa124342029be9b9bd8c87cb7d76d420e8994bb40d87889cbb4fa0e797e'
    assert execution['runner_source_commit']=='a2fdcfda40dbdc3d422542e9ee8ce48a8aa364bc'
    ledger=dict(execution['source_sha256'])
    for path,digest in inp['source_sha256'].items():
        assert path not in ledger or ledger[path]==digest
        ledger[path]=digest
    ledger.update({str(R/'scope-plan.json'):analysis['scope_sha256'],str(R/'retention-inputs.json'):expected['retention-inputs.json'],str(R/'execution-plan.json'):esha,execution['validation_results_path']:execution['validation_results_sha256']})
    assert analysis['source_sha256']==ledger
    assert analysis['model_runs']==analysis['HTTP_requests']==0 and not analysis['runtime_changes'] and not analysis['performance_acceptance'] and not analysis['physical_IO_prediction']
    wanted=[(g,k) for g in ('s02-as-on','s03-al-on') for k in range(4)]
    assert [(r['group'],r['position']) for r in analysis['requests']]==[(r['group'],r['position']) for r in plans['requests']]==wanted
    originals={(g['id'],q['position']):q for g in inp['groups'] for q in g['requests']}
    pair_plans={};all_support={};summary_rows=[];checks=Counter();entry_events=0
    for out,projected in zip(analysis['requests'],plans['requests']):
        key=(out['group'],out['position']);q=originals[key];snap=q['snapshots'];entry=snap[1];end=snap[-1]
        assert out['complete'] and out['iterator_exhausted'] and out['exact_gpu_endpoint_checked'] and out['initialized_from_actual_decode_entry'] and not out['lower_cache_history_simulated'] and projected['complete']
        assert out['input_tokens']==projected['input_tokens']==q['input_tokens'] and out['output_tokens']==projected['output_tokens']==q['output_tokens']==256
        assert len(out['layers'])==len(projected['layers'])==48
        pair_plans[key]=[l['plans'] for l in projected['layers']];all_support[key]=[]
        assert [l['layer'] for l in out['layers']]==[l['layer'] for l in projected['layers']]==list(range(48))
        assert out['observer_counters']==q['observer_decode']['counters']
        aggregate=Counter();request_opportunity_totals=Counter();request_entry_counts=[]
        for endpoint_index,snap_index in enumerate((0,1,5)):
            observed_endpoint=out['endpoints'][endpoint_index];captured=snap[snap_index]
            assert observed_endpoint['event']==captured['event'] and observed_endpoint['decode_forwards']==captured['decode_forwards']
            classified=[]
            for layer_index,layer in enumerate(captured['layers']):
                item={'layer':layer_index,**review_classification(layer)}
                assert item==observed_endpoint['layers'][layer_index]
                classified.append(item);checks['endpoint_layer_classifications']+=1
            request_entry_counts.append({name:sum(item['counts'][name] for item in classified) for name in ('gpu_only','l2_only','both','sole','empty_slots')})
        for layer_index,plan_rows in enumerate(pair_plans[key]):
            assert len(plan_rows)==255;checks['decode_layer_plans']+=len(plan_rows)
            original=q['observer_decode']['layers'][layer_index]['counters'];layer_out=out['layers'][layer_index]
            assert layer_out['observed']==original
            c=review_counts(plan_rows);assert c==layer_out['counters'];aggregate.update(c)
            assert c['loads']==original['planned_loads']==original['committed_loads']==original['source_l2']+original['source_mirror']+original['source_read']
            assert end['layers'][layer_index]['slot_clock']-entry['layers'][layer_index]['slot_clock']==255
            assert end['layers'][layer_index]['l2_clock']-entry['layers'][layer_index]['l2_clock']==original['source_l2']+original['source_read']
            opp,supported=review_opportunities(entry['layers'][layer_index],plan_rows,original['entry_mirror_candidate'])
            assert opp==layer_out['opportunities'],(key,layer_index,'opportunity mismatch')
            checks['layer_opportunity_records']+=1;entry_events+=len(opp['entry_mirror_first_events']);all_support[key].append(supported)
            for name in ('missing_total','supported_missing_total','potential_candidate_capacity','observed_candidates'):request_opportunity_totals[name]+=opp[name]
        assert dict(aggregate)==out['counters']
        for i,cut in enumerate((1,8,32,255)):
            prefix_parts=[review_counts(rows[:cut]) for rows in pair_plans[key]]
            c={name:sum(part[name] for part in prefix_parts) for name in review_counts([])}
            observed={name:snap[i+2]['counters'][name]-entry['counters'][name] for name in c}
            assert c==observed==out['prefixes'][i]['counters']
            supply={name:snap[i+2]['stats'][name]-entry['stats'][name] for name in ('l2_hits','l2_misses','mirror_hits')}
            assert supply==out['prefixes'][i]['supply'] and sum(supply.values())==c['loads']
            checks['prefix_counter_and_supply_records']+=1
        assert out['supply']==supply
        checks['completed_requests']+=1
        summary_rows.append({'group':key[0],'position':key[1],'input_tokens':q['input_tokens'],'decode_entry_counts':request_entry_counts[1],'endpoints_counts':request_entry_counts,'opportunity_totals':dict(request_opportunity_totals),'GPU_counters':out['counters'],'supply':supply})
    compact_comparisons=[]
    for position,comparison in enumerate(analysis['comparisons']):
        sk=('s02-as-on',position);lk=('s03-al-on',position);cs=originals[sk]['observer_decode']['layers'];cl=originals[lk]['observer_decode']['layers']
        sums=Counter();total_counts=Counter();total_caps={'S':Counter(),'L':Counter()}
        for layer,layer_comparison in enumerate(comparison['layers']):
            srows=pair_plans[sk][layer];lrows=pair_plans[lk][layer];mismatch=[];set_mismatch=[];counts=Counter();caps={'S':Counter(),'L':Counter()};per_plan=[]
            for j,(sp,lp) in enumerate(zip(srows,lrows)):
                sm,lm=set(sp['missing']),set(lp['missing']);common=sm&lm;only=(sm-lm,lm-sm)
                if sp['needed']!=lp['needed']:mismatch.append(j+1)
                if set(sp['needed'])!=set(lp['needed']):set_mismatch.append(j+1)
                pc={'common':len(common),'S_only':len(only[0]),'L_only':len(only[1]),'S_total':len(sm),'L_total':len(lm)};counts.update(pc)
                cp={}
                for side,key,single in (('S',sk,only[0]),('L',lk,only[1])):
                    supported=all_support[key][layer][j]
                    cp[side]={'common':min(8,len(common&supported)),'only':min(8,len(single&supported)),'total':min(8,len(supported))};caps[side].update(cp[side])
                per_plan.append({'plan':j+1,'miss_counts':pc,'capacities':cp});checks['paired_plan_capacities']+=1
            C={'S':cs[layer]['counters']['entry_mirror_candidate'],'L':cl[layer]['counters']['entry_mirror_candidate']};intervals={}
            for side in ('S','L'):
                assert 0<=C[side]<=caps[side]['total']
                intervals[side]={'lower':max(0,C[side]-caps[side]['only']),'upper':min(C[side],caps[side]['common'])}
                assert intervals[side]['lower']<=intervals[side]['upper'];total_caps[side].update(caps[side])
            difference={'lower':intervals['S']['lower']-intervals['L']['upper'],'upper':intervals['S']['upper']-intervals['L']['lower']}
            rebuilt={'layer':layer,'plan_count':255,'route_equal':not mismatch,'route_mismatch_plans':mismatch,'needed_sets_equal':not set_mismatch,'needed_set_mismatch_plans':set_mismatch,'miss_counts':dict(counts),'capacities':{s:dict(caps[s]) for s in ('S','L')},'observed_candidates':C,'common_candidate_intervals':intervals,'common_candidate_difference_interval':difference,'per_plan':per_plan}
            assert rebuilt==layer_comparison,(position,layer,'comparison mismatch')
            sums.update(difference);total_counts.update(counts);checks['per_layer_common_intervals']+=1
        assert dict(sums)==comparison['common_candidate_difference_interval']
        assert comparison['position']==position
        token_sha=[originals[key]['validation']['bindings'][str(Path(originals[key]['trace_path']).with_suffix('.tokens'))]['sha256'] for key in (sk,lk)]
        assert comparison['input_token_sha256_S']==token_sha[0] and comparison['input_token_sha256_L']==token_sha[1]
        assert comparison['same_input_tokens']==(token_sha[0]==token_sha[1])
        assert comparison['same_input_length']==(originals[sk]['input_tokens']==originals[lk]['input_tokens'])
        assert comparison['route_equal']==all(c['route_equal'] for c in comparison['layers'])
        assert comparison['observed_candidate_difference']==originals[sk]['observer_decode']['counters']['entry_mirror_candidate']-originals[lk]['observer_decode']['counters']['entry_mirror_candidate']
        mixed=[]
        for key in (sk,lk):
            for layer,snapshot in enumerate(originals[key]['snapshots'][1]['layers']):
                c=review_classification(snapshot)['counts']
                if c['gpu_only']+c['both']>0 and c['sole']>0:mixed.append({'group':key[0],'layer':layer})
        assert mixed==comparison['mixed_GPU_covered_and_sole_decode_entry_layers']
        compact_comparisons.append({**{k:v for k,v in comparison.items() if k!='layers'},'miss_count_totals':dict(total_counts),'capacity_totals':{side:dict(total_caps[side]) for side in ('S','L')}})
    primary=compact_comparisons[1]
    nominee=primary['same_input_tokens'] and primary['same_input_length'] and primary['route_equal'] and primary['common_candidate_difference_interval']['lower']>0 and bool(primary['mixed_GPU_covered_and_sole_decode_entry_layers'])
    verdict='FUTURE_GPU_COVERED_MIRROR_RECYCLING_CANDIDATE_ONLY' if nominee else 'DEMAND_RETENTION_NON_IDENTIFIABLE_NO_CANDIDATE'
    assert analysis['decision']==verdict and analysis['hypothesis_identified'] is False and analysis['runtime_candidate_admitted'] is False
    assert analysis['reconstructed_decode_layer_plans']==checks['decode_layer_plans']==97920
    assert checks['endpoint_layer_classifications']==1152 and checks['per_layer_common_intervals']==192 and checks['paired_plan_capacities']==48960
    compact={'schema':1,'passed':True,'checks':dict(checks),'entry_mirror_first_event_records_recomputed':entry_events,'decision':verdict,'rows':summary_rows,'comparisons':compact_comparisons,'artifact_reads':read_records,'analysis_source_ledger_matches_manifest_execution_union':True,'route_payloads_reparsed':0,'GPU_state_replay_repeated':False,'recorded_t':time.time()}
    p=R/'result-arithmetic-independent-summary.json'
    with p.open('x') as f:json.dump(compact,f,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps({'passed':True,'checks':compact['checks'],'entry_first_events':entry_events,'decision':verdict,'comparison_intervals':[(c['position'],c['common_candidate_difference_interval'],c['miss_count_totals'],c['capacity_totals']) for c in compact_comparisons]}))
    print('arithmetic_summary_sha',hashlib.sha256(p.read_bytes()).hexdigest())
    return compact

review_compact=independent_recompute()
