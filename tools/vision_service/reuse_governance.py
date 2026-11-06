"""Independently check the completed media governance, keeping FAILED intact."""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.vision_service import stage as s, governance as g


def check(out):
    folder=s.BASE/'phase-05b'
    raw=folder/'execution'
    result=json.loads((raw/'result.json').read_text())
    s.require(result['status']=='FAILED' and 'logical usage mismatch' in result['failure'], 'original failure')
    s.require(not result['cleanup_errors'] and not result['monitor_failures'], 'custody/monitor')
    s.require(json.loads((folder/'lease/exit.json').read_text())['status']=='COMMAND_FAILED','guard outcome')
    s.require(result['counts']==dict(starts=1,generations=13,controls=21,native=0),'attempt inventory')
    out.mkdir(exist_ok=False)
    def body(label):return json.loads((raw/(label+'-request.json')).read_text())['body']
    def wire(label):return (raw/(label+'-wire.bin')).read_bytes()
    completed=[]
    for label in ('tls-json','tls-sse','a1','a2','b1','beta-recovery'):
        request=body(label)
        value=(s.client.read_sse if request.get('stream') else s.client.read_json)(io.BytesIO(wire(label)))
        s.require(value.id==request['request_id'] and value.message['content'].strip()=='12' and
                  value.finish_reason=='stop','completed visual reply: '+label)
        completed.append(dict(label=label,usage=value.usage))
    for label in ('active','a3'):g.cancelled_wire(wire(label),body(label)['request_id'])
    s.require(b'cancelled during media processing' in wire('active'),'active cancellation stage')
    active=body('active')
    probe=s.BASE/'engine/q4t_preflight_probe'
    envelope=dict(allow_media=True,max_len=16384,max_prefill=8192,body=dict(request=active))
    run=subprocess.run([str(probe),str(s.MODEL/'tokenizer.json')],input=json.dumps(envelope)+'\n',
                       text=True,capture_output=True,timeout=30)
    (out/'probe.stdout').write_text(run.stdout);(out/'probe.stderr').write_text(run.stderr)
    run.check_returncode();pre=json.loads(run.stdout)
    charged_prompt=pre['prompt_tokens']
    log=(raw/'model/stderr.log').read_text()
    order=re.findall(r'id=(\S+) event=admitted',log)
    expected=[body(n)['request_id'] for n in ('tls-json','tls-sse','active','b1','a1','a2','beta-recovery')]
    s.require(order==expected and body('a3')['request_id'] not in order,'actual fair admission')
    s.require(not re.search('id='+active['request_id']+r' event=stage stage=prefill',log),'active entered language prefill')
    for label,status,code in (('direct-no-token',401,'client_authentication_failed'),
            ('tls-no-key',401,None),('expanded-tpm-denied',429,'client_tpm_exhausted'),
            ('invalid-media',400,None),('rpm-denied',429,'client_rpm_exhausted')):
        s.require(json.loads((raw/(label+'-transport.json')).read_text())['status']==status,'refusal status')
        if code:s.require(json.loads(wire(label))['error']['code']==code,'refusal classification')
    s.require(json.loads((raw/'cross-client-cancel-transport.json').read_text())['status']==404,'cancel isolation')
    before,after=[wire(n).decode() for n in ('quota-before','quota-after')]
    for who in ('alpha','beta'):
        for field in ('requests','reserved_tokens','settled_tokens_total'):
            s.require(g.rate(before,who,field)==g.rate(after,who,field),'preflight/refusal charge')
    before=wire('before-cancel').decode()
    after_files=list(raw.glob('after-cancel-*-wire.bin'))
    after=max(after_files,key=lambda p:int(p.stem.split('-')[-2])).read_text()
    queued=json.loads(wire('a3-preflight'))
    cost=queued['prompt_tokens']+queued['output']['available_tokens']
    s.require(g.rate(before,'alpha','reserved_tokens')-g.rate(after,'alpha','reserved_tokens')==cost and
              g.rate(before,'alpha','settled_tokens_total')==g.rate(after,'alpha','settled_tokens_total'),'queued refund')
    final=wire('final-ledger').decode();metrics=s.parse_metrics(final)
    completed_tokens=sum(r['usage']['total_tokens'] for r in completed)
    settled=sum(g.rate(final,c,'settled_tokens_total') for c in ('alpha','beta'))
    s.require(settled==completed_tokens+charged_prompt,'slot admission logical accounting')
    s.require(metrics['q4t_prompt_tokens_total']+metrics['q4t_generation_tokens_total']==completed_tokens,
              'generation-path metrics')
    s.require(all(g.rate(final,c,'reserved_tokens')==0 for c in ('alpha','beta')),'final reservation')
    s.require(all(metrics[n]==0 for n in ('q4t_active_chats','q4t_queued_requests','q4t_structured_active')),
              'final requests active')
    s.require(metrics['q4t_gpu_healthy']==1 and metrics['q4t_seq_slots_free']==1,'final GPU health')
    pre=json.loads(wire('expanded-preflight'))
    s.require(pre['prompt_tokens']-pre['media']['visual_tokens']+1+32<1000<
              pre['prompt_tokens']+pre['output']['available_tokens'],'expanded token boundary')
    values=dict(status='PASS_SCOPED_MEDIA_GOVERNANCE',prior_status='FAILED',
        prior_failure='driver equated admitted logical billing with generation-path metrics',
        completed=completed,queued_refund=cost,active_admitted_prompt_charge=charged_prompt,
        settled_tokens=settled,generation_path_tokens=completed_tokens,order=order,
        unexecuted_temporal_diagnostics=4,model_or_gpu_work=False,
        sources={str(p):s.sha(p) for p in [Path(__file__),probe,ROOT/'src/server/chat_generation.cpp',
            ROOT/'src/server/client_policy.cpp',ROOT/'docs/SERVE_CLIENT_GOVERNANCE_2026-11-05.md']})
    s.save(out/'result.json',values)
    return {k:v for k,v in values.items() if k not in ('sources','completed','order')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    print(json.dumps(check(p.parse_args().output)))
