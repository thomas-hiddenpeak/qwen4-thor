"""Offline reconstruction from retained inputs, raw HTTP and process evidence."""
import argparse
import base64
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess

import numpy as np
from PIL import Image
from tools.vision_service import closeout as c

s = c.s


def seal(phase):
    folder = c.BASE/phase
    plan = c.read(folder/'plan.json')
    paths = {Path(p) for p in plan['files']}
    paths.update(p for sub in (folder,c.BASE/'cpu') for p in sub.rglob('*') if p.is_file())
    cas = c.BASE/'originals'
    cas.mkdir(exist_ok=True)
    rows = {}
    for path in sorted(paths):
        digest = s.sha(path)
        if str(path) in plan['files']:
            s.require(digest == plan['files'][str(path)], 'source drift: '+str(path))
        target = cas/digest
        if not target.exists():shutil.copyfile(path,target)
        s.require(s.sha(target)==digest,'CAS corruption')
        rows[str(path)] = digest
    s.save(c.BASE/(phase+'-originals.json'),dict(source_commit=plan['source_commit'],files=rows))
    return len(rows)


class Evidence:
    def __init__(self, phase):
        self.folder = c.BASE/phase
        self.index = c.read(c.BASE/(phase+'-originals.json'))
        self.files = self.index['files']
        for digest in set(self.files.values()):
            s.require(s.sha(c.BASE/'originals'/digest)==digest,'retained object changed')
        self.plan = self.read('plan.json')
        for path,digest in self.plan['files'].items():
            s.require(self.files[path]==digest,'plan binding changed')
        tree = subprocess.check_output(['git','ls-tree','-r','-z',self.plan['source_commit']],cwd=s.ROOT)
        blobs = {}
        for item in tree.split(b'\0'):
            if item:
                header,name = item.split(b'\t',1)
                blobs[name.decode()] = header.split()[2].decode()
        self.git_sources = 0
        for name,digest in self.plan['files'].items():
            path = Path(name)
            if not path.is_relative_to(s.ROOT):continue
            relative = str(path.relative_to(s.ROOT))
            if relative not in blobs:continue
            data = (c.BASE/'originals'/digest).read_bytes()
            git_hash = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            s.require(git_hash==blobs[relative],'Git/source drift: '+relative)
            self.git_sources += 1

    def bytes(self, name):
        path = Path(name)
        if not path.is_absolute():path = self.folder/path
        return (c.BASE/'originals'/self.files[str(path)]).read_bytes()

    def read(self, name):
        return json.loads(self.bytes(name))

    def completion(self, label):
        body = self.read('execution/'+label+'-request.json')['body']
        transport = self.read('execution/'+label+'-transport.json')
        s.require(transport['status']==200,'failed HTTP '+label)
        wire = self.bytes('execution/'+label+'-wire.bin')
        result = (s.client.read_sse if body.get('stream') else s.client.read_json)(io.BytesIO(wire))
        s.require(result.id==body['request_id'] and result.model==body['model'],'wire identity')
        s.require(asdict(result)==self.read('execution/'+label+'-completion.json'),'derived response changed')
        return body,result


def direction_truth(body):
    part = body['messages'][0]['content'][0]
    xs = []
    for url in part['video_frames']:
        pixels = np.asarray(Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1]))).convert('RGB'))
        red = (pixels[:,:,0]>150)&(pixels[:,:,1]<90)&(pixels[:,:,2]<90)
        ys,x = np.where(red)
        s.require(len(x)>100,'missing red-object truth')
        xs.append(float(x.mean()))
    s.require(all(b>a for a,b in zip(part['video_timestamps'],part['video_timestamps'][1:])),
              'timestamps not increasing')
    s.require(abs(xs[-1]-xs[0])>20,'ambiguous geometric direction')
    return ('right' if xs[-1]>xs[0] else 'left'),xs


def json_content(text):
    # Only the explicitly unconstrained diagnostic may carry markdown fences.
    match = re.fullmatch(r'\s*```json\s*\n(.*?)\n```\s*',text,re.S)
    return json.loads(match.group(1) if match else text)


def final_counts(content, expected, citation):
    s.require(citation in content,'actual tool citation missing')
    for key,value in expected.items():
        s.require(re.search(r'(?m)^- '+re.escape(key)+r': '+str(value)+r'\s*$',content),
                  'incorrect final count: '+key)
        s.require(re.search(r'(?m)^\| '+re.escape(key)+r' \| '+str(value)+r' \| '+
                  str(value)+r' \| ✅ Yes \|$',content),'incorrect image/source comparison')


def lifecycle(evidence):
    result = evidence.read('execution/result.json')
    s.require(not result['cleanup_errors'] and not result['monitor_failures'],'cleanup/monitor failure')
    counts = result['counts']
    for key,count in counts.items():
        prefix = str(evidence.folder/('execution/attempt-'+key+'-'))
        attempts = [p for p in evidence.files if p.startswith(prefix)]
        s.require(len(attempts)==count and count<=evidence.plan['limits'][key],'attempt accounting')
    identity = evidence.read('execution/model/identity.json')
    wait = evidence.read('execution/model/waitable.json')
    reaped = evidence.read('execution/model/reaped.json')
    retained = evidence.read('execution/custody.json')['retained']
    s.require(len(retained)==1 and wait['returncode']==reaped['returncode']==0,'model exit')
    for key in ('pid','start_ticks','pgid','ppid'):
        s.require(identity[key]==wait['identity'][key]==retained[0][key],'owned identity drift')
    s.require(wait['identity']['state']==retained[0]['state']=='Z' and reaped['pid']==identity['pid'],
              'wait/reap barrier')
    gpu = evidence.read('execution/gpu-final.json')
    s.require(gpu['status']=='OK' and not gpu['pids'],'final GPU observation')
    events = [json.loads(x) for x in evidence.bytes('lease/events.jsonl').splitlines()]
    s.require(events[0]['event']=='lease_acquired' and events[-1]['event']=='lease_released',
              'lease boundaries')
    monitors = [e for e in events if e['event']=='monitor']
    s.require(monitors and all(not m['conflicts'] and m['unreadable_processes']==0 for m in monitors),
              'guard monitor error')
    s.require(any(m['monotonic']>wait['monotonic'] for m in monitors),'no post-exit monitor')
    status = evidence.read('lease/exit.json')['status']
    s.require(status in ('COMPLETED','COMMAND_FAILED'),'environment abort')
    seconds = events[-1]['monotonic']-events[0]['monotonic']
    s.require(seconds<evidence.plan['limits']['seconds'],'lease budget')
    return dict(status=result['status'],guard=status,counts=counts,lease_seconds=seconds,
        monitor_samples=len(monitors),owned_pid=identity['pid'],
        post_exit_monitor=True,final_gpu_empty=True,
        custody_ack_is_post_registration_not_post_exit=True)


def audit():
    out = c.BASE/'audit-01'
    out.mkdir()
    a,b = Evidence('phase-a'),Evidence('phase-b')
    phases = {name:lifecycle(e) for name,e in (('phase-a',a),('phase-b',b))}
    s.require(phases['phase-a']['status']=='FAILED' and phases['phase-a']['guard']=='COMMAND_FAILED',
              'original failed phase rewritten')
    s.require(phases['phase-b']['status']=='PASS_SCOPED_EXPLICIT_SCHEMA_AND_REAL_RECORD_LOOP'
        and phases['phase-b']['guard']=='COMPLETED','follow-up not passed')
    totals = {k:sum(p['counts'][k] for p in phases.values()) for k in ('starts','generations','controls','native')}
    c.budget_plans([a.plan,b.plan])
    rows = []
    for group in ('motion-forward','motion-reverse','motion-odd','encoded-video'):
        original = a.read('requests.json')
        original = next(r['request'] for r in original if r['label']==group+'-strict')
        if group=='encoded-video':
            old = a.read(s.BASE/'phase-04/execution/encoded-video-request.json')['body']
        else:
            old = next(r['request'] for r in a.read(s.BASE/'phase-04/requests.json') if r['label']==group)
        s.require(c.input_contract(original)==c.input_contract(old) and
            original['response_format']==old['response_format'],'original failed case changed')
        truth,centers = direction_truth(original)
        expected = 'left_to_right' if truth=='right' else 'right_to_left'
        item = dict(group=group,centers_x=centers,expected=expected,outputs={})
        tokens = None
        for mode in ('strict','json','free'):
            label = group+'-'+mode
            body,result = a.completion(label)
            s.require(c.input_contract(body)==c.input_contract(original),'A input changed')
            pre = a.read('execution/'+label+'-preflight-wire.bin')
            cpu = a.read('preflight.json')[label]
            s.require(pre['token_ids']==cpu['token_ids'] and pre['prompt']==cpu['prompt'],
                      'CPU/HTTP preflight mismatch')
            if tokens is None:tokens=pre['token_ids']
            s.require(tokens==pre['token_ids'] and len(tokens)==result.usage['prompt_tokens'],
                      'prompt mismatch')
            value = json_content(result.message['content'])['direction']
            s.require(result.finish_reason=='stop','incomplete diagnostic')
            s.require(value==truth if mode!='strict' else value!=expected,'unexpected A outcome')
            item['outputs'][mode]=value
        for mode in ('simple','explicit'):
            body,result = b.completion(group+'-'+mode)
            if mode=='simple':
                s.require(c.input_contract(body)==c.input_contract(original),'B simple prompt changed')
            else:
                stripped = json.loads(json.dumps(body))
                extra = stripped['messages'][-1]['content'].pop()
                s.require(c.input_contract(stripped)==c.input_contract(original),'B media/control changed')
                schema = json.loads(b.bytes(s.ROOT/'tools/examples/vision_direction.schema.json'))
                s.require(body['response_format']['json_schema']['schema']==schema and
                    json.dumps(schema,ensure_ascii=False,allow_nan=False) in extra['text'],'schema not visible')
            value = json.loads(result.message['content'])['direction']
            s.require(value==(truth if mode=='simple' else expected) and result.finish_reason=='stop',
                      'B direction failure')
            item['outputs'][mode]=value
        rows.append(item)
    actual_record = json.loads(a.bytes(s.ROOT/c.RECORD))
    truth = c.record_truth(actual_record)
    images = []
    for label,expected in (('record-table',truth['totals']),('record-chart',dict(phase=truth['tallest'])),
            ('record-multiple',dict(total=truth['totals']['generations'],phase05b=truth['chart']['phase-05b']))):
        body,result = a.completion(label)
        s.require(json.loads(result.message['content'])==expected and result.finish_reason=='stop','image truth')
        images.append(dict(label=label,actual=expected,usage=result.usage))
    body,call = a.completion('record-tool-call')
    calls = s.client.prepare_tool_calls(call,s.VALIDATORS)
    s.require(len(calls)==1 and calls[0].arguments==dict(path=c.RECORD,start_line=1,line_count=12),'tool call')
    tool = a.read('execution/record-tool-result.json')
    source = tool['source']
    data = a.bytes(s.ROOT/c.RECORD)
    s.require(source['sha256']==hashlib.sha256(data).hexdigest() and source['text']=='\n'.join(
        f'{i}: {line}' for i,line in enumerate(data.decode().splitlines()[:12],1)), 'tool source truth')
    failed = a.read('execution/record-tool-final-request.json')['body']
    s.require(a.read('execution/record-tool-final-transport.json')['status']==502 and
        a.read('execution/record-tool-final-wire.bin')['error']['code']=='structured_output_incomplete',
        'original 256-token failure changed')
    follow,final = b.completion('record-tool-final-768')
    expected_follow = dict(c.input_contract(failed),max_tokens=768)
    s.require(c.input_contract(follow)==expected_follow,'follow-up changed history/question')
    tool_messages = [m for m in follow['messages'] if m['role']=='tool']
    s.require(len(tool_messages)==1 and json.loads(tool_messages[0]['content'])==tool and
        tool_messages[0]['tool_call_id']==calls[0].call_id,'tool history not actual')
    final_counts(final.message['content'],truth['totals'],source['citation'])
    s.require(final.finish_reason=='stop' and 256<final.usage['completion_tokens']<=768,'budget diagnosis')
    runtime = c.read(s.BASE/'release-v1/candidate/runtime-source.json')
    s.require(all(s.sha(s.ROOT/p)==h for p,h in runtime.items()),'runtime changed')
    s.require(s.sha(s.ROOT/'build/q4t')==c.DEFAULT and s.sha(s.ROOT/'build/q4t.release.json')==
        '3eb463850cf07338406b811197a68a5dcb9474ac9c2cf039470aa4f654924724','default pair changed')
    report = dict(status='PASS_SCOPED_VISUAL_APPLICATION_CLOSEOUT',totals=totals,phases=phases,
        lease_seconds=sum(p['lease_seconds'] for p in phases.values()),
        original_file_bindings=sum(len(e.files) for e in (a,b)),
        git_source_bindings=sum(e.git_sources for e in (a,b)),runtime_sources_unchanged=len(runtime),
        directions=rows,images=images,tool=dict(source_sha256=source['sha256'],citation=source['citation'],
        call_id=calls[0].call_id,final_usage=final.usage,actual_tool_executions=1,final_attempts=2),
        original_direction_requests_still_failed=True,explicit_schema_four_fixtures_passed=True,
        renderer_not_natural_scans=True,runtime_change=False,default_pair_changed=False,
        global_media_acceptance=False,hardware_decode_accepted=False,long_stability=False,
        performance_acceptance=False,automatic_retry=False,
        explanation='Same-prompt natural direction succeeds; composite enum prefix trap is consistent with code and observed contrasts, not a full sequence-probability proof.')
    s.save(out/'result.json',report)
    return dict(status=report['status'],totals=totals,lease_seconds=report['lease_seconds'],
                original_file_bindings=report['original_file_bindings'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('seal-b','audit'))
    print(json.dumps(seal('phase-b') if parser.parse_args().action=='seal-b' else audit()))
