"""Rebuild scoped visual acceptance from raw responses and BF16 arrays.

Original failed stages remain failed. Output separates protocol, numerical
contracts, model task truth, diagnostics, hardware failures and delivery.
No model, GPU, HTTP, retries or installation in this program.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.vision_service import stage as s
from tools.serve_gateway.acceptance import cancelled_wire

PHASES=('phase-01','phase-02','phase-03','phase-04','phase-05','phase-05b','phase-06','phase-07')


def read(path):return json.loads(Path(path).read_bytes())


def completion(folder,label):
    body=read(folder/(label+'-request.json'))['body']
    transport=read(folder/(label+'-transport.json'))
    s.require(transport['status']==200,'successful HTTP status')
    raw=(folder/(label+'-wire.bin')).read_bytes()
    value=(s.client.read_sse if body.get('stream') else s.client.read_json)(io.BytesIO(raw))
    s.require(value.id==body['request_id'] and value.model==body['model'],'response identity')
    s.require(value.finish_reason=='stop','final response truncated')
    return body,value


def tool_loop(folder,source_commit,prefix):
    body=read(folder/(prefix+'-call-request.json'))['body']
    raw=(folder/(prefix+'-call-wire.bin')).read_bytes()
    value=s.client.read_sse(io.BytesIO(raw))
    calls=s.client.prepare_tool_calls(value,s.VALIDATORS)
    s.require(len(calls)==1 and calls[0].arguments['path']=='docs/STATUS.md','tool invocation')
    tool=read(folder/'real-tool-result.json');source=tool['source']
    canonical=subprocess.check_output(['git','show',source_commit+':docs/STATUS.md'],cwd=ROOT)
    s.require(hashlib.sha256(canonical).hexdigest()==source['sha256'],'real project document identity')
    lines=canonical.decode().splitlines()
    quoted='\n'.join(f'{n}: {lines[n-1]}' for n in range(source['start_line'],source['end_line']+1))
    s.require(quoted==source['text'],'actual tool contents')
    _,final=completion(folder,prefix+'-final')
    s.require(source['citation'] in final.message.get('content',''),'final source citation')
    if prefix=='visual-tool':s.require('12' in final.message['content'],'visual document value')
    return dict(source_sha256=source['sha256'],citation=source['citation'],call_id=calls[0].call_id)


def report(out):
    out.mkdir(parents=True,exist_ok=False)
    phases=[];totals=dict(starts=0,generations=0,controls=0,native=0)
    cas=s.BASE/'frozen-inputs';cas.mkdir(exist_ok=True)
    for name in PHASES:
        folder=s.BASE/name;result=read(folder/'execution/result.json');plan=read(folder/'plan.json')
        terminal=read(folder/'lease/exit.json')
        if name=='phase-05':
            s.require(result['counts']==dict(starts=1,generations=0,controls=0,native=0) and
                len(result['cleanup_errors'])==1 and 'child exit:' in result['cleanup_errors'][0] and
                read(folder/'execution/model/waitable.json')['returncode']==1,
                'original startup/custody failure differs')
        else:s.require(not result['cleanup_errors'],'phase custody: '+name)
        s.require(not result['monitor_failures'],'phase monitor: '+name)
        expected='COMPLETED' if name in ('phase-06','phase-07') else 'COMMAND_FAILED'
        s.require(terminal['status']==expected,'original lease status: '+name)
        if name!='phase-05':
            gpu=read(folder/'execution/gpu-final.json')
            s.require(gpu['status']=='OK' and not gpu['pids'],'final GPU observation: '+name)
        for identity in (folder/'execution').glob('*/identity.json'):
            if name=='phase-05':continue  # No fresh child acknowledgment; never accepted/reused.
            reaped=read(identity.parent/'reaped.json')
            s.require(reaped['pid']==read(identity)['pid'],'owned child identity')
        for path,digest in plan['files'].items():
            p=Path(path)
            if p.is_file() and s.sha(p)==digest:
                if not (cas/digest).exists():shutil.copy2(p,cas/digest)
            elif not (cas/digest).exists() and p.is_relative_to(ROOT):
                historical=subprocess.run(['git','show',plan['source_commit']+':'+str(p.relative_to(ROOT))],
                    cwd=ROOT,capture_output=True)
                s.require(historical.returncode==0 and hashlib.sha256(historical.stdout).hexdigest()==digest,
                          'original committed input unavailable: '+path)
                (cas/digest).write_bytes(historical.stdout)
            s.require((cas/digest).is_file() and s.sha(cas/digest)==digest,'frozen input missing: '+path)
        events=[json.loads(line) for line in (folder/'lease/events.jsonl').read_text().splitlines()]
        begin=next(e['monotonic'] for e in events if e['event']=='lease_acquired')
        seconds=terminal['finished']['monotonic']-begin
        phases.append(dict(phase=name,status=result['status'],guard=terminal['status'],
                           counts=result['counts'],driver_seconds=result['seconds'],lease_seconds=seconds,
                           cleanup_errors=result['cleanup_errors'],
                           accepted_custody=name!='phase-05'))
        for key in totals:totals[key]+=result['counts'][key]
    s.require(totals['starts']==6 and totals['generations']==70 and totals['native']==5,'cumulative attempts')
    s.require(sum(p['lease_seconds'] for p in phases)<5400,'cumulative lease time')
    # Check every independently generated reference element, not test stdout.
    numbers=[]
    for group in ('small','rectangular'):
        data=read(s.BASE/'cpu/hf-tower-01'/(group+'.json'))
        for case,row in data.items():
            file=s.BASE/'phase-04/execution'/('hf-'+group+'-output-'+case+'.bf16')
            raw=np.fromfile(file,dtype=np.uint16)
            actual=(raw.astype(np.uint32)<<16).view(np.float32)
            expected=np.asarray(row['output'],dtype=np.float32).ravel()
            s.require(actual.shape==expected.shape and np.isfinite(actual).all(),'full tower tensor shape/finite')
            diff=actual.astype(np.float64)-expected.astype(np.float64)
            maximum=float(abs(diff).max());relative=float(np.linalg.norm(diff)/np.linalg.norm(expected.astype(np.float64)))
            s.require(maximum<.05 and relative<.05,'unchanged full tower bounds')
            numbers.append(dict(group=group,case=case,elements=len(raw),max_abs=maximum,rel_l2=relative))
    negative=read(s.BASE/'cpu/hf-tower-01/manifest.json')['old_cuda_against_independent_reference']
    s.require(len(negative)==2 and all(not v['passed'] and v['rel_l2']>.05 for v in negative),'old negative control')
    rows=read(s.BASE/'phase-04/requests.json');folder=s.BASE/'phase-04/execution'
    observations=[];quality_failures=[]
    for row in rows:
        label=row['label'];body,value=completion(folder,label)
        pre=read(folder/(label+'-preflight-wire.bin'))
        s.require(value.usage['prompt_tokens']==pre['prompt_tokens'],'expanded preflight consistency')
        content=value.message['content']
        actual=json.loads(content) if row['check']=='json' else content.strip().strip('.。').lower()
        good=(actual.get('color')=='red') if label=='image-json-object' else actual==row['expected']
        if not good:quality_failures.append(label)
        observations.append(dict(label=label,quality_passed=good,actual=actual,expected=row['expected'],
            tokens=value.usage,http_seconds=read(folder/(label+'-transport.json'))['seconds'],
            oracle_note='Generic JSON object permits extra keys' if label=='image-json-object' else None))
    _,encoded=completion(folder,'encoded-video')
    s.require(json.loads(encoded.message['content'])['direction']=='right_to_left','preserved encoded direction failure')
    quality_failures.append('encoded-video')
    s.require(quality_failures==['motion-forward','motion-reverse','motion-odd','encoded-video'],
              'quality outcomes differ from original evidence')
    for i in range(5):
        s.require(read(folder/f'invalid-{i}-transport.json')['status']==400 and
            read(folder/f'invalid-{i}-wire.bin')['error']['code'],'input rejection')
    cancelled=read(folder/'cancelled-visual-request.json')['body']
    cancelled_wire((folder/'cancelled-visual-wire.bin').read_bytes(),cancelled['request_id'])
    s.require(completion(folder,'recovery')[1].message['content'].strip().lower().strip('.')=='red','cancel recovery')
    image_tool=tool_loop(folder,read(s.BASE/'phase-04/plan.json')['source_commit'],'visual-tool')
    # Scoped governance replay was independently rebuilt, including the
    # original admitted-prompt charge that generation metrics exclude.
    governance=read(s.BASE/'cpu/governance-replay-01/result.json')
    s.require(governance['status']=='PASS_SCOPED_MEDIA_GOVERNANCE' and
        governance['settled_tokens']==governance['generation_path_tokens']+governance['active_admitted_prompt_charge'],
        'scoped media governance')
    folder=s.BASE/'phase-07/execution'
    for row in read(s.BASE/'phase-07/requests.json'):
        _,value=completion(folder,row['label'])
        actual=value.message['content'].strip()
        if isinstance(row['expected'],dict):actual=json.loads(actual)
        s.require(actual==row['expected'],'default text protection')
    text_tool=tool_loop(folder,read(s.BASE/'phase-07/plan.json')['source_commit'],'text-tool')
    config=read(folder/'config-wire.bin')
    s.require(config['runtime']['mtp_enabled'] and not config['runtime']['media_enabled'],'default text profile')
    diagnostics={n:completion(s.BASE/'phase-06/execution',n)[1].message['content']
                 for n in ('first-frame','last-frame','video-plain','endpoint-images')}
    result=dict(status='PASS_SCOPED_VISION_SERVICE_V1_WITH_TASK_LIMITS',
        candidate_sha256=s.sha(s.BASE/'engine/q4t'),protocol_sha256=config['protocol_source_sha256'],
        totals=totals,lease_seconds=sum(p['lease_seconds'] for p in phases),phases=phases,
        image_business_accepted=True,video_input_and_numerical_contract_accepted=True,
        structured_output_syntax_accepted=True,structured_temporal_direction_accepted=False,
        temporal_quality_failures=quality_failures,video_diagnostics=diagnostics,
        independent_tower=numbers,old_same_input_negative=negative,image_observations=observations,
        image_tool=image_tool,text_tool=text_tool,media_governance=governance,
        default_text_mtp_protection=True,hardware_encoding='H264_NVENC_BOUNDED_FIXTURE_PASSED',
        hardware_decoding='FAILED_BUFFER_POOL_NOT_ACCEPTED',
        jpeg='stb/Pillow non-bit-equivalence retained; PNG-equivalent postdecode pipeline and actual document passed',
        previous_failed_stages_preserved=True,performance_acceptance=False,long_stability=False,
        real_lan_deployment=False,managed_task_package_migration=False,default_installation=False)
    s.save(out/'result.json',result)
    # Complete local original files are sealed by identity. The report is
    # small and portable; large raw requests/media stay in the protected
    # workspace, with frozen source versions addressable by content hash.
    paths=[]
    for phase in PHASES:paths += [p for p in (s.BASE/phase).rglob('*') if p.is_file()]
    paths += [p for p in (s.BASE/'cpu').rglob('*') if p.is_file()]
    paths += [p for p in (s.BASE/'fixtures').iterdir() if p.is_file()]
    manifest={str(p.relative_to(s.BASE)):dict(bytes=p.stat().st_size,sha256=s.sha(p)) for p in paths}
    s.save(out/'originals.json',manifest)
    s.save(out/'binding.json',dict(report_sha256=s.sha(out/'result.json'),originals_sha256=s.sha(out/'originals.json'),
        verifier_sha256=s.sha(Path(__file__)),source_cas=str(cas),raw_base=str(s.BASE)))
    return dict(status=result['status'],totals=totals,lease_seconds=result['lease_seconds'],original_files=len(manifest))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    print(json.dumps(report(p.parse_args().output)))
