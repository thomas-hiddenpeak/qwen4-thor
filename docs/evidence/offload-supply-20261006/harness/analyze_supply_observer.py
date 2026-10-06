"""Closed-scope arithmetic of actual supplies; no route replay/model/HTTP."""
import argparse
from pathlib import Path
import time
from observer_common import R, frozen, passed, read, save, sha


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--plan-sha256',required=True);args=parser.parse_args()
    plan=frozen(args.plan_sha256);sources={}
    def bind(path):
        path=Path(path).resolve();sources[str(path)]=sha(path);return read(path)
    def add_binding(path):
        path=Path(path).resolve();sources[str(path)]=sha(path)
    admission=bind(R/'observer-analysis-admission.json')
    assert admission['execution_admitted'] is True and admission['plan_sha256']==args.plan_sha256
    assert admission['analysis_script_sha256']==sha(__file__)
    for path,digest in admission['source_sha256'].items():
        assert sha(path)==digest,path
        sources[path]=digest
    reports={}
    for group in plan['group_ids']:
        reports[group]=passed(group,plan,args.plan_sha256)
        add_binding(R/(group+'-decision.json'))
    contracts=bind(R/'observer-contracts-decision.json');assert contracts['passed'] and contracts['total']==46
    resources=bind(R/'observer-resources.json')
    assert contracts['plan_sha256']==args.plan_sha256 and contracts['runtime_binary_sha256']==plan['runtime_binary_sha256']
    assert resources['audit_complete'] and resources['http_request_count']==27 and resources['client_envelope_count']==17
    assert resources['plan_sha256']==args.plan_sha256
    assert resources['runtime_source_commit']==plan['runtime_source_commit'] and resources['runtime_binary_sha256']==plan['runtime_binary_sha256']
    for path,record in resources['metadata_source_sha256'].items():
        assert sha(path)==record['sha256'],path
    for path,record in resources['source_sha256'].items():
        stat=Path(path).stat()
        assert record['complete_file'] and tuple(record['stat_before'])==tuple(record['stat_after'])==(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns),path
    for path,record in resources['trace_binary_source_bindings'].items():
        stat=Path(path).stat()
        assert tuple(record['signature'])==(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns),path
    assert sum(v['request_count'] for v in reports.values())==27
    cells={}; direct_total=0; witnesses=[]
    for group in plan['group_ids'][1:]:
        report=reports[group];rows=[]
        assert len(report['supply']['requests'])==len(report['metrics'])==4
        for k,(item,metric) in enumerate(zip(report['supply']['requests'],report['metrics'])):
            assert item['response_id']==metric['response_id']
            pre=item['prefill']['stats'];dec=item['decode']['stats']
            row={'position':k,'actual_input':item['actual_input'],'actual_output':item['actual_output'],
                 'http_metric':metric,'prefill_stats':pre,'decode_stats':dec,
                 'decode_phase_elapsed_ns':item['decode']['elapsed_ns'],
                 'decode_pid_storage_read_bytes':item['decode']['pid_read_bytes'],
                 'observer_additional_json_bytes':item['observer_additional_json_bytes']}
            if report['supply']['observer_enabled']:
                observed=item['supply_observer']['decode'];c=observed['counters']
                loss=observed['direct_read_losses'];direct_total+=loss
                assert c['entry_mirror_candidate']==c['source_mirror']+loss+c['entry_candidate_to_l2']
                assert c['source_read']==dec['l2_misses'] and c['source_mirror']==dec['mirror_hits']
                row['observer_decode_counters']=c
                row['observer_prefill_counters']=item['supply_observer']['prefill']['counters']
                row['direct_loss_read_fraction']=loss/c['source_read'] if c['source_read'] else None
                row['direct_loss_candidate_fraction']=loss/c['entry_mirror_candidate'] if c['entry_mirror_candidate'] else None
                row['layers']=observed['layers']
                for layer in observed['layers']:
                    witnesses.extend({'group':group,'position':k,**sample} for sample in layer['retained_phase_samples'])
            rows.append(row)
        cells[group]=rows
    contrasts=[]
    s=cells['s02-as-on'];l=cells['s03-al-on']
    for k,(short,long) in enumerate(zip(s,l)):
        cs=short['observer_decode_counters'];cl=long['observer_decode_counters']
        losses=lambda c:c['entry_candidate_to_read_direct_active']+c['entry_candidate_to_read_direct_published']
        deficit=cs['source_mirror']-cl['source_mirror']
        opportunities=cs['entry_mirror_candidate']-cl['entry_mirror_candidate']
        direct=losses(cl)-losses(cs)
        diverted=cl['entry_candidate_to_l2']-cs['entry_candidate_to_l2']
        assert deficit==opportunities+direct+diverted
        gpu=cl['committed_loads']-cs['committed_loads'];l2=cl['source_l2']-cs['source_l2']
        read_delta=cl['source_read']-cs['source_read']
        assert read_delta==gpu-l2+deficit
        layers=[]
        assert len(short['layers'])==len(long['layers'])==48
        assert [x['layer'] for x in short['layers']]==[x['layer'] for x in long['layers']]==list(range(48))
        for a,b in zip(short['layers'],long['layers']):
            ca,cb=a['counters'],b['counters'];d=ca['source_mirror']-cb['source_mirror']
            pieces=[ca['entry_mirror_candidate']-cb['entry_mirror_candidate'],losses(cb)-losses(ca),cb['entry_candidate_to_l2']-ca['entry_candidate_to_l2']]
            assert sum(pieces)==d
            layer_gpu=cb['committed_loads']-ca['committed_loads']
            layer_l2=cb['source_l2']-ca['source_l2']
            layer_read=cb['source_read']-ca['source_read']
            assert layer_read==layer_gpu-layer_l2+d
            layers.append({'layer':a['layer'],'mirror_deficit_S_minus_L':d,
                           'entry_candidate_difference_S_minus_L':pieces[0],
                           'direct_loss_difference_L_minus_S':pieces[1],
                           'candidate_to_L2_difference_L_minus_S':pieces[2],
                           'GPU_load_difference_L_minus_S':layer_gpu,'L2_hit_difference_L_minus_S':layer_l2,
                           'software_read_difference_L_minus_S':layer_read})
        contrasts.append({'position':k,'same_input_length':short['actual_input']==long['actual_input'],
            'mirror_deficit_S_minus_L':deficit,'entry_candidate_difference_S_minus_L':opportunities,
            'direct_loss_difference_L_minus_S':direct,'candidate_to_L2_difference_L_minus_S':diverted,
            'GPU_load_difference_L_minus_S':gpu,'L2_hit_difference_L_minus_S':l2,
            'software_read_difference_L_minus_S':read_delta,'layers':layers,
            'interpretation':'Exact accounting identity in new instrumented runs; entry opportunities depend on preceding cache/scheduling history and are not an independent causal component.'})
    off_on=[]
    for predecessor,off,on in [('S','s01-as-off','s02-as-on'),('L','s04-al-off','s03-al-on')]:
        for k,(a,b) in enumerate(zip(cells[off],cells[on])):
            off_on.append({'predecessor':predecessor,'position':k,
                'ttft_on_minus_off':b['http_metric']['ttft']-a['http_metric']['ttft'],
                'decode_tps_on_minus_off':b['http_metric']['decode_tps']-a['http_metric']['decode_tps'],
                'decode_reads_on_minus_off':b['decode_stats']['l2_misses']-a['decode_stats']['l2_misses'],
                'decode_GPU_loads_on_minus_off':b['decode_stats']['loads']-a['decode_stats']['loads'],
                'limit':'Single sequential cell; scheduling/cache state can change. Not pure observer overhead or stable speed effect.'})
    add_binding(__file__);add_binding(R/'observer-execution-plan.json');add_binding(R/'observer_common.py')
    result={'schema':1,'passed':True,'plan_sha256':args.plan_sha256,
        'runtime_source_commit':plan['runtime_source_commit'],'runtime_binary_sha256':plan['runtime_binary_sha256'],
        'service_count':5,'request_count':27,'diagnostic_request_count':16,'contract_count':46,'resource_envelope_count':17,
        'raw_resource_integrity_scope':'Reuse completed raw-reader SHA with unchanged device/inode/size/mtime; raw stream not re-parsed or rehashed by this analysis.',
        'cells':cells,'instrumented_S_L_accounting':contrasts,'off_on_descriptive_contrasts':off_on,
        'instrumented_decode_direct_read_losses_total':direct_total,'retained_decode_witnesses':witnesses,
        'decision':'DIRECT_SAME_PLAN_LOSS_OBSERVED_FUTURE_GUARD_CANDIDATE_ONLY' if direct_total else 'NO_DIRECT_LOSS_OBSERVED_NO_OPTIMIZATION_CANDIDATE',
        'future_candidate':{'name':'Skip writeback victims that hold an unclaimed needed entry mirror during the same plan',
            'status':'NOT_IMPLEMENTED_NOT_PERFORMANCE_ADMITTED',
            'rationale':'Observed successful direct READ losses provide a concrete removable local mechanism; changing writeback retention may change later history and speed.',
            'required_next_scope':'Separate default-off implementation and frozen HTTP-first quality, full five-tier/target/history/resource performance acceptance.'} if direct_total else None,
        'limits':['No causal frequency for the old16 requests: observer can perturb worker ordering.',
            'New entry opportunities and direct losses are exact bookkeeping, not a counterfactual decomposition of historical speed.',
            'No old direct bound is assumed valid for new entry histories; no new counterfactual GPU/lower-cache replay.',
            'Raw cumulative/nested timers and PID read bytes are not exclusive SSD/H2D waiting or expert-only storage traffic.',
            'At most one future candidate; no optimization implementation or performance acceptance. Prior NO_GO/default-off and54GB unknown persist.'],
        'source_sha256':sources,'recorded_t':time.time()}
    for path,digest in sources.items():assert sha(path)==digest,path
    save(R/'observer-analysis.json',result)
    print({k:result[k] for k in ('passed','decision','instrumented_decode_direct_read_losses_total')},flush=True)


if __name__=='__main__':main()
