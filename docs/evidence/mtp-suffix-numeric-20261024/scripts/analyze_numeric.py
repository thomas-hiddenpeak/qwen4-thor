"""Offline numeric-suffix evidence report. Never invokes HTTP, models or tests.

Run only after the outer phase terminates. Partial/failed runs retain missing
items and never acquire a passing throughput or engineering conclusion.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re

ROOT = Path('/home/rm01/models/dev/qwen4-thor')
BASE = ROOT / '.q4t-work/suffix-numeric-gate-20261023'
HELPER = ROOT / '.q4t-work/suffix-k3-20261023/analyze.py'
SPEC = importlib.util.spec_from_file_location('frozen_suffix_analysis_helpers', HELPER)
old = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(old)
sha, read_json, read_jsonl = old.sha, old.read_json, old.read_jsonl


def issue(items, scope, reason, **details):
    items.append(dict(scope=scope, reason=reason, **details))


def numeric_totals(rows, acceptance):
    """Matched length includes rejected numeric candidates in its denominator."""
    value = old.suffix_totals(rows, acceptance)
    for key in ['numeric_checks','classified_tokens','numeric_rejected','numeric_ns']:
        numbers = [r.get(key) for r in rows]
        value[key] = sum(numbers) if numbers and all(isinstance(x,int) for x in numbers) else None
    keys = ['hits','incomplete','d0_mismatch','numeric_rejected']
    eligible = all(isinstance(value.get(k),int) for k in keys)
    denominator = sum(value[k] for k in keys) if eligible else None
    valid = (eligible and isinstance(value.get('matched'),int) and
             value.get('invalid')==0 and value.get('exhausted')==0)
    value['matched_tokens_per_matched_query'] = value['matched']/denominator if valid and denominator else None
    value['matched_query_count'] = denominator if valid else None
    value['matched_query_scope'] = ('All >=3-token matches before completeness/d0/numeric admission; '
        'denominator=hits+incomplete+d0_mismatch+numeric_rejected. Not successful-hit mean length.')
    value['numeric_cost_scope'] = 'numeric_ns is already included in query_ns; never add both.'
    value['numeric_mean_ns_per_check'] = (value['numeric_ns']/value['numeric_checks']
        if value.get('numeric_checks') and value.get('numeric_ns') is not None else None)
    return value


def whole_generation_work(row, histogram, hits):
    if row.get('actual_output')!=256 or row.get('finish')!=['length'] or not histogram:
        return None
    if len(histogram)!=4 or any(not isinstance(x,int) or x<0 for x in histogram):
        return None
    verifies = sum(histogram)
    emitted = sum((i+1)*n for i,n in enumerate(histogram))
    tail = 256-emitted
    if tail<0 or hits is None or hits>verifies:
        return None
    ordinary = max(0,tail-1)
    neural = 2*(verifies-hits)
    return dict(verify_calls=verifies, mtp_emitted_tokens=emitted,
        ordinary_tail_output_tokens=tail, ordinary_tail_target_T1=ordinary,
        total_target_forward_calls=verifies+ordinary,
        target_processed_rows=4*verifies+ordinary,
        target_restore_calls=sum(histogram[:3]), neural_proposal_T1=neural,
        draft_extend_calls=verifies, total_mtp_forward_calls=neural+verifies,
        scope='Whole iterative generation including first MTP round; excludes prefill/init. '
              'latency-minus-TTFT has a different window. Nonterminal full-256 requests only; '
              'counts are source/log derivations, not GPU timing.')


def attach_case(group, name, rows, expected, metadata, missing):
    value = dict(id=name, expected_requests=expected, role=metadata.get('role'),
                 source_metadata=metadata, **old.metric_summary(rows))
    prefix_count = sum(len(x['raw']) for x in group['cases'])
    suffix_map = {r.get('request'):r for r in group['mtp']['suffix']}
    accept_map = {r.get('request'):r for r in group['mtp']['acceptance']}
    for index, raw in enumerate(value['raw']):
        rid = raw['response_id']
        suffix = suffix_map.get(rid)
        accepted = accept_map.get(rid)
        raw['server_request_ordinal'] = prefix_count+index+1
        raw['first_request_of_service'] = prefix_count+index==0
        raw['post_ttft_seconds'] = (raw['latency']-raw['ttft']
            if isinstance(raw.get('latency'),(int,float)) and
               isinstance(raw.get('ttft'),(int,float)) else None)
        raw['suffix'] = suffix
        raw['accepted_a0_a1_a2_a3'] = accepted['counts'] if accepted else None
        raw['whole_generation_work'] = whole_generation_work(raw,
            accepted['counts'] if accepted else None,
            suffix.get('hits') if suffix else 0 if not group['suffix_enabled'] else None)
        if group['head']!='plain' and not accepted:
            issue(missing,group['name']+'/'+name,'request acceptance summary missing',request=rid)
        if group['suffix_enabled'] and not suffix:
            issue(missing,group['name']+'/'+name,'request suffix summary missing',request=rid)
    # metric_summary's aliases refer to the same raw objects, including first_request.
    ids = {r['response_id'] for r in value['raw']}
    value['suffix'] = [r for r in group['mtp']['suffix'] if r.get('request') in ids]
    value['acceptance'] = [r for r in group['mtp']['acceptance'] if r.get('request') in ids]
    value['suffix_totals'] = numeric_totals(value['suffix'],value['acceptance'])
    value['timing_by_suffix'] = old.timing_summary([r for r in group['mtp']['timing'] if r.get('request') in ids])
    value['complete'] = len(rows)==expected
    value['expected_input_identity_matches'] = (all(
        r['prompt_sha256']==metadata['prompt_sha256'] and r['actual_input']==metadata['input_tokens']
        for r in value['raw']) if 'prompt_sha256' in metadata else None)
    if not value['complete']:
        issue(missing,group['name']+'/'+name,'request count incomplete',expected=expected,observed=len(rows))
    if value['expected_input_identity_matches'] is False:
        issue(missing,group['name']+'/'+name,'frozen input hash/token identity mismatch')
    if group['kind']!='quality' and rows and not value['full_256_length']:
        issue(missing,group['name']+'/'+name,'not all requests produced 256 length-finished tokens')
    if rows and not value['deterministic']:
        issue(missing,group['name']+'/'+name,'within-start repeated outputs differ')
    group['cases'].append(value)


def analyze_group(plan, step, missing):
    folder = Path(plan['output'])/step['name']
    group = dict(name=step['name'],kind=step['kind'],head=step.get('head'),
        suffix_enabled=step.get('suffix',False),block=step.get('block'),
        expected_requests=step['http_requests'],path=str(folder),cases=[])
    if step['kind']=='contract':
        log = Path(plan['output'])/(step['name']+'-driver.log')
        group.update(log=str(log),log_sha256=sha(log) if log.is_file() else None,
            log_tail='\n'.join(log.read_text().splitlines()[-30:]) if log.is_file() else None)
        if not log.is_file():
            issue(missing,step['name'],'contract log missing')
        return group
    group['exit'] = read_json(folder/'exit.json',missing)
    group['server_command'] = read_json(folder/'server-command.json',missing)
    group['events'] = read_jsonl(folder/'events.jsonl',missing)
    group['server_audit'] = read_json(folder/'server-audit.json',missing)
    group['numeric_audit'] = read_json(folder/'numeric-audit.json',missing) if group['suffix_enabled'] else None
    if not group['exit'] or group['exit'].get('server')!=0 or not group['exit'].get('http_output_checks_passed'):
        issue(missing,step['name'],'HTTP group did not close with clean checked exit')
    rows = read_json(folder/'results.json',missing,[])
    group['mtp'] = old.parse_mtp(folder/'server.log',missing)
    group['mtp']['totals'] = numeric_totals(group['mtp']['suffix'],group['mtp']['acceptance'])
    log = (folder/'server.log').read_text() if (folder/'server.log').is_file() else ''
    group['server_log_sha256'] = sha(folder/'server.log') if log else None
    table = []
    group['fallbacks'] = []
    for line_number,line in enumerate(log.splitlines(),1):
        if line.startswith('[suffix-classes] '):
            fields = dict(re.findall(r'(\w+)=(\S+)',line))
            table.append(dict(line=line_number,raw=line,**{k:int(v) if v.isdigit() else v for k,v in fields.items()}))
        if any(marker in line for marker in ['MTP trunk alloc failed','plain decode for this request',
               'MTP load failed','MTP init failed','scheduler logits alloc failed',
               'scheduler tokens alloc failed','trunk alloc failed for',' disabled=']):
            group['fallbacks'].append(dict(line=line_number,raw=line))
    group['shared_class_table'] = table
    init = [r for r in table if r.get('event')=='init']
    release = [r for r in table if r.get('event')=='release']
    if group['suffix_enabled']:
        summaries=group['mtp']['suffix']
        lifecycle = len(init)==len(release)==1 and len(table)==2
        group['shared_class_table_complete'] = lifecycle
        group['table_order_valid'] = bool(lifecycle and summaries and
            init[0]['line']<min(r['line'] for r in summaries) and
            release[0]['line']>max(r['line'] for r in summaries))
        if not lifecycle or not group['table_order_valid']:
            issue(missing,step['name'],'classification table lifecycle/order incomplete')
        if lifecycle and (init[0].get('bytes')!=248320 or init[0].get('cap_bytes')!=262144 or
                          init[0].get('unknown')!=243 or release[0].get('bytes')!=248320):
            issue(missing,step['name'],'classification table size/capacity/domain mismatch')
        if len(summaries)!=step['http_requests'] or len({r.get('request') for r in summaries})!=len(summaries):
            issue(missing,step['name'],'suffix request coverage or uniqueness mismatch')
        for row in summaries:
            keys=['queries','hits','numeric_checks','classified_tokens','numeric_rejected','numeric_ns','query_ns','peak_bytes']
            if not all(isinstance(row.get(k),int) and row[k]>=0 for k in keys):
                issue(missing,step['name'],'missing/invalid numeric admission counters',request=row.get('request'))
                continue
            checks=row['numeric_checks']
            if (row['peak_bytes']<=0 or checks!=row['hits']+row['numeric_rejected'] or
                checks>row['queries'] or not 3*checks<=row['classified_tokens']<=64*checks or
                row['numeric_ns']>row['query_ns'] or row.get('invalid')!=0 or row.get('exhausted')!=0):
                issue(missing,step['name'],'numeric gate/resource/timing counters inconsistent',request=row.get('request'))
    elif table or group['mtp']['suffix']:
        issue(missing,step['name'],'suffix-off unexpectedly constructed/used suffix resources')
    if group['fallbacks']:
        issue(missing,step['name'],'explicit runtime fallback',records=group['fallbacks'])
    if group['mtp']['unparsed']:
        issue(missing,step['name'],'unparsed MTP lines',records=group['mtp']['unparsed'])
    for record in group['mtp']['timing']:
        if not record.get('request') or record.get('gpu_ms') is None:
            issue(missing,step['name'],'timing request binding or GPU elapsed fields missing',line=record['line'])
    if step['kind']=='quality':
        group['quality'] = dict(expected=11,observed=len(rows),exact_matches=sum(r.get('exact_match') is True for r in rows))
        if group['quality']['observed']!=11 or group['quality']['exact_matches']!=11:
            issue(missing,step['name'],'quality gate incomplete or not exact-match passing')
        for row in rows:
            attach_case(group,str(row.get('id')), [row],1,{'role':'quality'},missing)
    elif step['kind']=='performance':
        metadata={int(r['nominal_input']):r for r in plan['performance']}
        for length in [1024,4096,8192,45056,204800]:
            responses=read_json(folder/f'context-{length}/responses.json',missing,[])
            attach_case(group,f'context-{length}',responses,3,metadata[length],missing)
    elif step['kind'] in ('block','timing'):
        recorded={r['id']:r for r in rows} if isinstance(rows,list) else {}
        for item in step['cases']:
            responses=recorded.get(item['id'],{}).get('responses',[])
            attach_case(group,item['id'],responses,item['repeats'],item,missing)
    elif step['kind']=='lifecycle':
        group['lifecycle']=rows
        group['lifecycle_artifacts']={}
        for artifact in ['suffix-'+step['head']+'-cancel-response.json',
                         'cancel-control-response.json','after-cancel-health.json','after-recovery-health.json']:
            p=folder/artifact
            content=read_json(p,missing)
            group['lifecycle_artifacts'][artifact]=dict(path=str(p),sha256=sha(p) if p.is_file() else None,
                                                       recorded=content)
    events={e['event']:e for e in group['events']}
    start,ready,end=[events.get(k) for k in ['server_started','server_ready','server_after_shutdown']]
    if start:
        group['server_pid']=start['pid']
        group['window']=dict(start_monotonic=start['monotonic_seconds'],
                            end_monotonic=end['monotonic_seconds'] if end else None)
        group['startup_to_ready_seconds']=ready['monotonic_seconds']-start['monotonic_seconds'] if ready else None
    group['observed_response_count']=(rows.get('requests',0) if step['kind']=='lifecycle' and isinstance(rows,dict)
                                      else sum(len(c['raw']) for c in group['cases']))
    group['timing_by_suffix']=old.timing_summary(group['mtp']['timing'])
    if group['observed_response_count']!=step['http_requests']:
        issue(missing,step['name'],'HTTP group request count mismatch',expected=step['http_requests'],observed=group['observed_response_count'])
    return group


def compare_case(before,after):
    if before is None or after is None:
        return dict(complete=False,reason='one or both arms absent')
    result=dict(complete=before['complete'] and after['complete'],
        before_raw=before['raw'],after_raw=after['raw'],
        input_hashes_equal={r['prompt_sha256'] for r in before['raw']}=={r['prompt_sha256'] for r in after['raw']},
        actual_input_counts_equal={r['actual_input'] for r in before['raw']}=={r['actual_input'] for r in after['raw']},
        output_hashes_equal={r['output_sha256'] for r in before['raw']}=={r['output_sha256'] for r in after['raw']},
        within_arm_deterministic=before['deterministic'] and after['deterministic'])
    for metric in ['decode_tps','ttft','latency','total_tps']:
        a,b=before[metric]['median'],after[metric]['median']
        ratio=b/a if a and b is not None else None
        result[metric]=dict(off_median=a,on_median=b,on_off_ratio=ratio,
                            change_pct=100*(ratio-1) if ratio else None)
    return result


def paired_cases(groups):
    output={}
    for head in ['full','subset']:
        result={}
        for block in [1,2]:
            left=groups.get(f'block-{block}-{head}-off',{}).get('cases',[])
            right=groups.get(f'block-{block}-{head}-on',{}).get('cases',[])
            left={r['id']:r for r in left};right={r['id']:r for r in right}
            for name in sorted(set(left)|set(right)):
                entry=result.setdefault(name,dict(role=(left.get(name) or right[name])['role'],blocks=[]))
                entry['blocks'].append(dict(block=block,**compare_case(left.get(name),right.get(name))))
        for name,entry in result.items():
            blocks=entry['blocks']
            complete=len(blocks)==2 and all(b['complete'] for b in blocks)
            entry['complete']=complete
            entry['aggregate']={}
            for metric in ['decode_tps','ttft','latency','total_tps']:
                ratios=[b.get(metric,{}).get('on_off_ratio') for b in blocks]
                valid=complete and all(isinstance(r,(int,float)) and r>0 for r in ratios)
                gm=math.exp(sum(math.log(r) for r in ratios)/2) if valid else None
                entry['aggregate'][metric]=dict(paired_ratios=ratios,geometric_mean_ratio=gm,
                    change_pct=100*(gm-1) if gm else None,
                    each_block_ratio_gt_one=all(r>1 for r in ratios) if valid else None,
                    each_block_ratio_at_least_one=all(r>=1 for r in ratios) if valid else None)
            all_rows=[r for b in blocks for side in ['before_raw','after_raw'] for r in b.get(side,[])]
            entry['all_blocks_input_identity_equal']=bool(all_rows) and len({(r['prompt_sha256'],r['actual_input']) for r in all_rows})==1
            entry['all_blocks_output_identity_equal']=bool(all_rows) and len({r['output_sha256'] for r in all_rows})==1
            if name=='old-8k':
                count_checks=[]
                for b in blocks:
                    left=b.get('before_raw',[]);right=b.get('after_raw',[])
                    l=left[0].get('whole_generation_work') if len(left)==1 else None
                    r=right[0].get('whole_generation_work') if len(right)==1 else None
                    keys=['verify_calls','ordinary_tail_output_tokens','ordinary_tail_target_T1']
                    count_checks.append(dict(block=b['block'],off=l,on=r,
                        protected_count_deltas={k:r[k]-l[k] for k in keys} if l and r else None,
                        does_not_increase=all(r[k]<=l[k] for k in keys) if l and r else None))
                entry['old_8k_work_protection']=count_checks
        output[head]=result
    return output


def goals(pairs,measurement_complete):
    result={}
    for head,cases in pairs.items():
        repeats={}
        for name in ['repeat-code','repeat-text','old-repeat-code','old-repeat-text']:
            c=cases.get(name,{})
            agg=c.get('aggregate',{}).get('decode_tps',{})
            gm=agg.get('geometric_mean_ratio')
            identity=c.get('all_blocks_input_identity_equal') is True and c.get('all_blocks_output_identity_equal') is True
            repeats[name]=dict(role=c.get('role'),paired_ratios=agg.get('paired_ratios'),
                geometric_mean_ratio=gm,each_block_positive=agg.get('each_block_ratio_gt_one'),
                input_output_identity_equal=identity,
                numeric_threshold_met=(gm>=1.05 and agg['each_block_ratio_gt_one']) if gm else None,
                frozen_goal_met=(gm>=1.05 and agg['each_block_ratio_gt_one']) if measurement_complete and identity and gm else None)
        c=cases.get('old-8k',{});agg=c.get('aggregate',{}).get('decode_tps',{})
        work=c.get('old_8k_work_protection',[])
        work_ok=len(work)==2 and all(w['does_not_increase'] is True for w in work)
        old_gate=(agg.get('each_block_ratio_at_least_one') is True and work_ok)
        result[head]=dict(repeat_scenarios=repeats,old_8k=dict(
            role='development_regression',paired_ratios=agg.get('paired_ratios'),
            geometric_mean_ratio=agg.get('geometric_mean_ratio'),work=work,
            input_output_identity_equal=c.get('all_blocks_input_identity_equal') is True and c.get('all_blocks_output_identity_equal') is True,
            frozen_runtime_gate_met=old_gate if measurement_complete and c.get('all_blocks_input_identity_equal') and c.get('all_blocks_output_identity_equal') else None,
            cpu_eight_mistake_rejection='See numeric-development-replay contract log; runtime need not contain exactly eight rejections.'))
    return result


def protection_panel(pairs,plain):
    """Expose every block's direction; no pooled gain can clear a local risk."""
    entries=[]
    def add(head,name,block,comparison,role):
        left=comparison.get('before_raw',[]);right=comparison.get('after_raw',[])
        metrics={}
        directions=[]
        for metric,lower_is_better in [('decode_tps',False),('ttft',True),
                                       ('latency',True),('total_tps',False)]:
            summary=comparison.get(metric,{})
            ratio=summary.get('on_off_ratio')
            adverse=(ratio>1 if lower_is_better else ratio<1) if ratio is not None else None
            metrics[metric]=dict(summary,off_values=[r.get(metric) for r in left],
                on_values=[r.get(metric) for r in right],observed_adverse_direction=adverse)
            if adverse and metric in ['decode_tps','ttft']:
                directions.append(metric)
        entries.append(dict(head=head,case=name,block=block,role=role,
            complete=comparison.get('complete',False),metrics=metrics,
            input_hashes_equal=comparison.get('input_hashes_equal'),
            actual_input_counts_equal=comparison.get('actual_input_counts_equal'),
            output_hashes_equal=comparison.get('output_hashes_equal'),
            within_arm_deterministic=comparison.get('within_arm_deterministic'),
            off_first_request=left[0] if left else None,
            on_first_request=right[0] if right else None,
            off_work=[r.get('whole_generation_work') for r in left],
            on_work=[r.get('whole_generation_work') for r in right],
            off_acceptance=[r.get('accepted_a0_a1_a2_a3') for r in left],
            on_acceptance=[r.get('accepted_a0_a1_a2_a3') for r in right],
            on_numeric_rejected=[(r.get('suffix') or {}).get('numeric_rejected') for r in right],
            review_triggers=directions,
            conclusion='REQUIRES_REVIEW; observed direction is not a significance or credibility test'))
    for head,cases in pairs.items():
        for name,case in cases.items():
            for block in case['blocks']:
                add(head,name,block['block'],block,case.get('role'))
    for name,comparison in plain.items():
        add('plain',name,None,comparison,'contemporary_parent_candidate_protection')
    return dict(entries=entries,observed_adverse_directions=[
        dict(head=e['head'],case=e['case'],block=e['block'],metrics=e['review_triggers'])
        for e in entries if e['review_triggers']],
        interpretation='Each block and protection is retained separately. No percentage tolerance, '
            'no cross-tier/head/scenario offset, and no automatic credibility inference. '
            'Repeat-goal success cannot clear a credible protection regression. '
            'Plain off/on labels mean parent/candidate; first rows are first of case, '
            'and their first_request_of_service flag distinguishes the actual service first request.')


def budget_audit(plan,state,observed,missing):
    frozen=dict(http_requests=238,http_loads=18,stage_timeout_seconds=14400,
        direct_contract_timeout_seconds=1200,deployment_separate_max_requests=3,
        deployment_separate_max_loads=3,total_authorized_max_http=241,retries=0,warmups=0)
    if any(plan.get(k)!=v for k,v in frozen.items()):
        issue(missing,'budget','frozen budget differs from authorized numeric-gate design')
    observations=dict(http_requests_in_artifacts=observed,
        http_requests_completed=state.get('requests_completed'),
        http_loads_started=state.get('loads_started'),
        stage_elapsed_seconds=state.get('elapsed_seconds'),
        direct_contract_seconds=state.get('contract_seconds'))
    bounds={'http_requests_in_artifacts':238,'http_requests_completed':238,
            'http_loads_started':18,'stage_elapsed_seconds':14400,'direct_contract_seconds':1200}
    within={k:isinstance(v,(int,float)) and 0<=v<=bounds[k]
            for k,v in observations.items()}
    for key,ok in within.items():
        if not ok:
            issue(missing,'budget','budget value missing or above bound',field=key,value=observations[key],limit=bounds[key])
    return dict(frozen=frozen,observed=observations,within_frozen_bounds=within,
        all_within_frozen_bounds=all(within.values()),
        deployment='Three requests/starts are separate and conditional on acceptance; never assumed executed.',
        scope='Inference HTTP only; readiness/health/cancel control calls remain separate. No retries, extra warmups, or added sampling.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,default=BASE/'acceptance-v1/frozen-plan.json')
    parser.add_argument('--output',type=Path,default=BASE/'analysis.json')
    args=parser.parse_args()
    missing=[]
    plan=read_json(args.plan,missing)
    if not plan:
        print(json.dumps(dict(status='NOT_RUN',reason='frozen plan not available',missing=missing)))
        return
    run=Path(plan['output']);state=read_json(run/'state.json',[],{})
    lease=read_json(Path(plan['lease'])/'exit.json',[],{})
    if state.get('status') not in ['MEASURED_REQUIRES_REVIEW','STOPPED','ENV_ABORT'] or not lease:
        print(json.dumps(dict(status='NOT_RUN',reason='wait for complete stage and outer lease termination',state=state.get('status'))))
        return
    if args.output.exists():
        raise SystemExit('refusing to overwrite an existing analysis; use a separate explicit output for a corrected report')
    groups={s['name']:analyze_group(plan,s,missing) for s in plan['steps']}
    for step in plan['steps']:
        if step['name'] not in state.get('completed',[]):
            issue(missing,step['name'],'step not recorded complete')
    observed=sum(g.get('observed_response_count',0) for g in groups.values())
    complete=(state.get('status')=='MEASURED_REQUIRES_REVIEW' and
              state.get('requests_completed')==238 and state.get('loads_started')==18 and
              len(state.get('completed',[]))==len(plan['steps']) and observed==238 and not missing)
    pairs=paired_cases(groups)
    plain_left={r['id']:r for r in groups['performance-parent-plain']['cases']}
    plain_right={r['id']:r for r in groups['performance-candidate-plain']['cases']}
    plain={name:compare_case(plain_left.get(name),plain_right.get(name))
           for name in [f'context-{x}' for x in [1024,4096,8192,45056,204800]]}
    resources=old.resources(plan,groups,missing)
    budget=budget_audit(plan,state,observed,missing)
    complete=complete and not missing
    result=dict(created_utc=datetime.now(timezone.utc).isoformat(),analysis_tool_sha256=sha(Path(__file__)),
        helper_path=str(HELPER),helper_sha256=sha(HELPER),plan_path=str(args.plan),plan_sha256=sha(args.plan),
        source_commit=plan['source_commit'],candidate_sha256=plan['candidate_sha256'],parent_sha256=plan['parent_sha256'],
        state=state,lease=lease,expected_http=238,observed_http=observed,expected_services=18,
        first_gate_passed=groups['quality-full-on'].get('quality',{}).get('exact_matches')==11,
        measurement_complete=complete,missing_or_partial=missing,groups=groups,paired_comparisons=pairs,
        plain_comparisons=plain,goals=goals(pairs,complete),resources=resources,
        protections=protection_panel(pairs,plain),budget=budget,
        service_first_requests={n:next((r for c in g['cases'] for r in c['raw']
            if r.get('first_request_of_service')),None) for n,g in groups.items()
            if g['kind'] not in ['contract','lifecycle']},
        quality={n:g.get('quality') for n,g in groups.items() if g['kind']=='quality'},
        lifecycle={n:g.get('lifecycle') for n,g in groups.items() if g['kind']=='lifecycle'},
        timing={n:dict(cases=g['cases'],by_suffix=g['timing_by_suffix']) for n,g in groups.items() if g['kind']=='timing'},
        reused_evidence=plan['reused_evidence'],
        statistics='Two paired startup blocks per head; per-tier medians use three within-start requests, '
                   'scenarios use one request/start. Geometric mean of two ON/OFF ratios is descriptive; '
                   'no independent-request bootstrap or confidence/noninferiority claim.',
        timing_scope='Timing arms are diagnostics only. Numeric timing is nested inside query timing; '
                     'CUDA event elapsed includes stream idle; wall extend+accept is combined. '
                     'Whole-generation work counts include pre-first-token round, unlike latency-minus-TTFT.',
        evidence_validity='REQUIRES_REVIEW',engineering_decision='REQUIRES_REVIEW; never inferred from hit rate or a subset of goals',
        decision_fields=dict(evidence_validity='REQUIRES_REVIEW; inspect identities, lease, monitoring, budget and raw records',
            measurement_completeness=dict(
                required_request_scope_recorded=(observed==238 and state.get('requests_completed')==238 and
                    state.get('loads_started')==18 and len(state.get('completed',[]))==len(plan['steps'])),
                checked_report_complete=complete,expected_http=238,observed_http=observed,
                missing_or_inconsistent_items=len(missing)),
            statistical_conclusion='DESCRIPTIVE_ONLY; two paired starts do not establish population noninferiority',
            engineering_decision='REQUIRES_REVIEW; all frozen goals and each protection must be assessed separately'),
        protection_rule='Report each block/tier/head, input/output differences and ordinary decode separately. '
                        'No generic loss tolerance and no averaging away credible protection regression.')
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(output=str(args.output),sha256=sha(args.output),measurement_complete=complete,
                         observed_http=observed,missing_items=len(missing)),indent=2))


if __name__=='__main__':
    main()
