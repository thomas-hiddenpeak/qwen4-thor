"""Read one terminal observer cell; no HTTP, model or performance acceptance."""
import argparse
import hashlib
import math
from pathlib import Path
import sys
import time
from observer_common import R, O, M, frozen, read, save, sha
sys.path.insert(0, str(O/'tools/evalscope'))
sys.path.insert(0, str(O/'tools/trace'))
sys.path.insert(0, str(M))
from supply_observer_protocol import (observer_environment, read_supply_sequence,
    read_supply_records, audit_supply_group, supply_request_identity)
from request_policy_protocol import request_path_evidence, decode_log_path_evidence
import offload_trace
from observation_contract import validate_route_rows


def audit(cell, expected):
    plan = frozen(expected)
    assert cell in plan['group_ids']
    quality = cell == 'q01-quality-on'
    enabled = cell not in ('s01-as-off','s04-al-off')
    q = O/'.q4t-work/evidence'/cell
    sources = {}
    def bind(path):
        path=Path(path).resolve(); digest=sha(path)
        assert str(path) not in sources or sources[str(path)]==digest
        sources[str(path)]=digest
        return path
    def load(path):return read(bind(path))
    for path in plan['audit_sources']:bind(path)
    controller=load(R/(cell+'-stage.json'))
    assert controller['returncode']==0 and controller['failure'] is None
    assert controller['cleanup_complete'] is True and controller['plan_sha256']==expected
    assert controller['command']==load(R/(cell+'-command.json'))
    assert controller['first_runtime_test'] is quality
    wrapper=load(q/'wrapper-exit.json'); exit_record=load(q/'http/exit.json')
    group=load(q/'runner-process-group.json');cleanup=load(q/'http/isolation/cleanup.json')
    isolation=load(q/'http/isolation/identity.json');capacity=load(q/'http/capacity.json')
    protocol=load(q/'protocol.json');command=load(q/'http/server-command.json')
    assert wrapper['runner_rc']==wrapper['monitor_rc']==exit_record['server']==0
    assert wrapper['failure'] is exit_record['failure'] is exit_record['cleanup_failure'] is None
    assert not wrapper['cleanup_failed'] and wrapper['unit_after_cleanup']['LoadState']=='not-found'
    assert exit_record['http_output_checks_passed'] and exit_record['performance_acceptance'] is False
    assert group['cleanup_complete'] and group['runner_reaped'] and group['returncode']==0 and group['failure'] is None
    assert group['after_cleanup']['absent'] and all(not group['after_cleanup'][k] for k in ('live_pids','zombie_pids','errors'))
    assert cleanup['stop_rc']==0 and cleanup['unit_removed'] and cleanup['properties_after']['MainPID']=='0'
    assert capacity['matches_requested'] and capacity['effective']==capacity['requested']=={'max_len':262144,'max_seq':1,'max_prefill':8192}
    assert isolation['properties']['MemoryMax']==str(16<<30) and isolation['properties']['MemorySwapMax']=='0'
    assert protocol['host_cache_max_bytes']==16<<30 and protocol['swap_max_bytes']==0
    assert protocol['effective_environment']==command['effective_q4t_environment']==observer_environment(enabled,{})
    assert protocol['binary_sha256']==plan['runtime_binary_sha256']
    assert bind(q/'http/binary.sha256').read_text().strip()==plan['runtime_binary_sha256']
    assert bind(q/'http/commit.txt').read_text().strip()==plan['runtime_source_commit']
    assert not bind(q/'http/worktree.patch').read_bytes()
    assert sha(bind(q/'http/CMakeCache.txt'))==plan['frozen_files'][str(R/'observer-build/CMakeCache.txt')]
    assert command['argv'][:2]==[plan['runtime_binary_path'],'serve']
    assert command['argv'].count('--no-mtp')==1 and '--mtp' not in command['argv']
    assert command['isolation']['systemd_unit']==protocol['unit']
    for key,value in {'--max-seq':'1','--max-prefill':'8192','--max-len':'262144','--moe-resident-slots':'256','--port':'8185'}.items():
        assert command['argv'].count(key)==1 and command['argv'][command['argv'].index(key)+1]==value
    for name,digest in protocol['tool_sha256'].items():
        assert digest==plan['tool_sha256'][name]==sha(bind(q/'tools'/name))
    for path,digest in protocol['input_config_sha256'].items():
        assert digest==plan['frozen_files'][path]==sha(bind(path))
    entry={x['path']:x for x in load(R/'model-entry.json')['files']}
    assert len(protocol['model_files'])==226
    for item in protocol['model_files']:assert item==entry[item['path']]
    gate=load(q/'cache-gate.json')
    assert gate['cold_payload_established'] and gate['payload_resident_bytes']==0 and not gate['advice_errors']
    assert gate['max_advice_rounds']==protocol['cold_advice_rounds']==2 and gate['pre_service_only'] and not gate['inference_retry']
    assert 1<=len(gate['rounds'])<=2
    for index,summary in enumerate(gate['rounds'],1):
        current=load(q/f'cache-advice-round-{index:02d}.json')
        assert all(current[k]==v for k,v in summary.items()) and not summary['advice_errors']
    assert load(q/'cache-after-advice.json')==current['observation']
    result=load(q/'http/results.json');rows=[];metrics=[];sequence=None
    if quality:
        rows=load(q/'http/quality/responses.json');reference=load(plan['quality_reference_path'])
        assert len(result)==len(rows)==len(reference)==11
        refs={r['prompt_sha256']:r for r in reference};assert len(refs)==11
        assert all(r['success'] and r['exact_match'] and r['length_match'] for r in result)
        for row in rows:
            ref=refs[row['prompt_sha256']]
            assert all(row[k]==ref[k] for k in ('text','actual_input','actual_output','finish'))
        assert not (q/'http/trace').exists()
    else:
        path=R/'observer-sequences'/(cell+'.json')
        sequence,raw=read_supply_sequence(bind(path),sha(path))
        assert protocol['performance_plan']==load(q/'http/performance-plan.json')==sequence
        assert bind(q/'mechanism-sequence.json').read_bytes()==bind(q/'http/mechanism-sequence.json').read_bytes()==raw
        assert len(result)==len(sequence['requests'])==4
        refs=load(plan['diagnostic_reference_paths'][sequence['predecessor']])
        for position,(item,request) in enumerate(zip(result,sequence['requests'])):
            actual=load(q/'http'/request['case']/'responses.json');assert len(actual)==1
            row=actual[0];assert row['actual_input']==request['input_tokens'] and row['actual_output']==256 and row['finish']==['length']
            assert all(item[k]==v for k,v in supply_request_identity(sequence,request).items())
            ref=refs[position]
            assert all(row[k]==ref[k] for k in ('text','prompt_sha256','actual_input','actual_output','finish'))
            assert item['outputs']==[hashlib.sha256(row['text'].encode()).hexdigest()]
            rows.append(row)
    for row in rows:
        assert row['success'] and row['response_id_valid'] and row['timing']['within_client_boundaries']
        assert all(type(row[k]) in (int,float) and math.isfinite(row[k]) and row[k]>0 for k in ('ttft','latency'))
        assert row['actual_output'] <= 1 or row['latency'] > row['ttft']
        metrics.append({'response_id':row['response_id'],'actual_input':row['actual_input'],'actual_output':row['actual_output'],
                        'ttft':row['ttft'],'latency':row['latency'],
                        'decode_tps':(row['actual_output']-1)/(row['latency']-row['ttft']) if row['actual_output']>1 else None})
    assert len({r['response_id'] for r in rows})==len(rows)
    log=bind(q/'http/server.log');content=log.read_text()
    runtime=request_path_evidence(content,rows,0);layers=decode_log_path_evidence(content,rows,0,0)
    assert runtime['runtime_eligible'] and layers['runtime_eligible']
    records=read_supply_records(log,sources)
    supply=audit_supply_group(records,rows,enabled,'quality' if quality else 'diagnostic',
                              None if quality else [r['input_tokens'] for r in sequence['requests']])
    traces=[];manifest=None
    if not quality:
        td=q/'http/trace';files=sorted(td.glob('request-*.bin'),key=lambda p:int(p.stem.split('-')[-1]));assert len(files)==4
        manifest=offload_trace._package(files[0],Path(plan['runtime_binary_path']))
        assert manifest['requests_started']==manifest['requests_published']==4
        assert manifest['binary_sha256']==plan['runtime_binary_sha256']
        assert manifest['max_length']==262144 and manifest['quota_bytes']==128*(1<<20)
        assert sum(p.stat().st_size for p in td.iterdir() if p.is_file())<=128*(1<<20)
        for path in td.iterdir():
            if path.is_file():bind(path)
        assert (td/'workload.json').read_bytes()==raw
        actual_command=(td/'command.bin').read_bytes();assert actual_command.endswith(b'\0')
        assert [x.decode() for x in actual_command[:-1].split(b'\0')]==command['argv']
        environment=(td/'environment.bin').read_bytes();assert environment.endswith(b'\0')
        items=[x.decode().split('=',1) for x in environment[:-1].split(b'\0')];assert len(items)==len({x[0] for x in items})
        assert {k:v for k,v in items if k.startswith('Q4T_')}=={**observer_environment(enabled,{}),'Q4T_MOE_STREAMS':'1'}
        assert not any(k=='LD_PRELOAD' and v for k,v in items)
        previous=0
        for index,(path,row) in enumerate(zip(files,rows),1):
            metadata=load(path.with_suffix('.json'));assert metadata['request_id']==index
            summary={};forwards=validate_route_rows(offload_trace._layers(path,manifest,metadata,summary),metadata,row,manifest,summary)
            assert forwards[0]['forward_id']>previous;previous=forwards[-1]['forward_id']
            traces.append({'path':str(path),'metadata':metadata,'summary':summary,'forwards':forwards})
    for path,digest in sources.items():assert sha(path)==digest,path
    return {'schema':1,'passed':True,'status':'PASS_FIXED_HTTP_11' if quality else 'PASS_GROUP_CONTRACTS',
        'group_id':cell,'plan_sha256':expected,'runtime_binary_sha256':plan['runtime_binary_sha256'],
        'runtime_source_commit':plan['runtime_source_commit'],'recorded_t':time.time(),'ended_t':controller['ended_t'],
        'source_sha256':sources,'request_count':len(rows),'metrics':metrics,'runtime_path':runtime,'all_layer_runtime':layers,
        'supply':supply,'trace_manifest':manifest,'traces':traces,'performance_acceptance':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('group');parser.add_argument('--plan-sha256',required=True);args=parser.parse_args()
    result=audit(args.group,args.plan_sha256);save(R/(args.group+'-decision.json'),result)
    print({k:result[k] for k in ('passed','group_id','request_count','status')},flush=True)


if __name__=='__main__':main()
