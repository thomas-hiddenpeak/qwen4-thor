"""A05 schema/tool/text-cache business checks under one continuous GPU lease."""
import argparse
import copy
from dataclasses import asdict
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_grammar_reap import acceptance as lifecycle
from tools.serve_custom_tools.acceptance import require_fragments
from tools.serve_protocol.js_client import JsSdkClient, JsSdkError
from tools.serve_protocol.stage import sdk_identity
from tools.serve_recovery.bundle import assets, check_assets
from tools.serve_service.package import require, save, sha
from tools.examples.project_records import ProjectRecords, VALIDATORS
from tools.examples.chat_client import prepare_tool_calls, append_tool_results, load_json

LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)
DOCUMENT = "docs/MODEL.md"


def closed(properties):
    return dict(type="object", properties=properties, required=list(properties),
                additionalProperties=False)


def feature_schema():
    node = closed(dict(label=dict(type="string", const="leaf"), children=dict(
        type="array", items={"$ref":"#/$defs/node"}, maxItems=1)))
    value = closed(dict(answer=dict(type="integer", minimum=42, maximum=42, multipleOf=2),
        exact=dict(type="integer", const=9007199254740993),
        recorded_at=dict(type="string", format="date-time", enum=["2024-02-29T12:00:00Z"]),
        note=dict(anyOf=[dict(type="string", pattern="^cache$"), dict(type="null")]),
        node={"$ref":"#/$defs/node"}))
    return {"$ref":"#/$defs/result", "$defs":dict(result=value, node=node)}


def final_schema():
    return closed(dict(title=dict(type="string", minLength=1, maxLength=256),
        citation=dict(type="string", pattern=r"^\[docs/MODEL\.md:L1-L16\]$"),
        requested_line_count=dict(type="integer", minimum=16, maximum=16, multipleOf=8),
        note=dict(anyOf=[dict(type="null"), dict(type="string", const="read")])))


def tagged(label, body):
    result=copy.deepcopy(body)
    result.update(request_id="schema-v2-"+label,
                  cancel_token=hashlib.sha256(label.encode()).hexdigest())
    return result


def requests():
    base=dict(model="qwen3.8-flash-next", enable_thinking=False, temperature=0,
              max_completion_tokens=384, stream=False)
    instruction=("Return one JSON object: answer is 17+25, exact is the integer "
        "9007199254740993, recorded_at is 2024-02-29T12:00:00Z, note is null, "
        "node is {\"label\":\"leaf\",\"children\":[]}. No markdown.")
    # Token count is checked by actual preflight before spending a Chat attempt.
    # This targets the existing 8192-token private-cache publication boundary.
    feature=dict(base, messages=[dict(role="system", content="Archive context:\n"+
        "archive record\n"*2800), dict(role="user", content=instruction)],
        response_format=dict(type="json_schema", json_schema=dict(name="features",
            strict=True, schema=feature_schema())), session_id="a"*64,
        prefix_cache_scope="b"*64, prefix_cache_write_tokens=1024,
        logprobs=True, top_logprobs=2)
    params={"$ref":"#/$defs/read", "$defs":dict(
        path=dict(type="string", pattern=r"^docs/MODEL\.md$", minLength=13, maxLength=13),
        read=closed(dict(path={"$ref":"#/$defs/path"},
            start_line=dict(type="integer", const=1),
            line_count=dict(type="integer", minimum=16, maximum=16, multipleOf=8))))}
    tool=dict(base, stream=True, stream_options=dict(include_usage=True),
        messages=[dict(role="user",content="Read the first 16 lines of docs/MODEL.md "
            "with read_project_record. Then return its actual first-line title and exact "
            "source citation in JSON; requested_line_count is 16 and note is null.")],
        tools=[dict(type="function", function=dict(name="read_project_record",
            description="Read actual project documentation lines; use the returned citation verbatim.",
            strict=True, parameters=params))], tool_choice="required",
        parallel_tool_calls=False, response_format=dict(type="json_schema",json_schema=dict(
            name="record",strict=True,schema=final_schema())))
    truncated=dict(feature, session_id="c"*64, prefix_cache_scope="d"*64,
        stream=True, stream_options=dict(include_usage=True), max_completion_tokens=8)
    recovered=dict(truncated, max_completion_tokens=384, prefix_cache_write_tokens=0)
    greedy=dict(base, max_completion_tokens=32,
        messages=[dict(role="user",content="Compute 17 + 25. Reply only with the decimal number.")])
    return {label:tagged(label,body) for label,body in dict(
        features=feature, warm=dict(feature,stream=True,stream_options=dict(include_usage=True)),
        tool=tool, truncated=truncated, recovered=recovered, greedy=greedy).items()}


def check_features(value):
    require(value.finish_reason=="stop" and not value.message.get("tool_calls"),
            "features missing successful JSON terminal")
    actual=load_json(value.message["content"],exact_numbers=True)
    require(actual==dict(answer=42,exact=9007199254740993,
        recorded_at="2024-02-29T12:00:00Z",note=None,
        node=dict(label="leaf",children=[])), "schema or supplied task values differ")
    return actual


def check_final(value, result):
    require(value.finish_reason == "stop" and not value.message.get("tool_calls"),
            "final JSON missing")
    answer = load_json(value.message["content"], exact_numbers=True)
    title = result["source"]["text"].splitlines()[0].split(": ", 1)[1]
    require(answer["title"] in (title, title.lstrip("# ")) and
            answer["citation"] == result["source"]["citation"] and
            answer["requested_line_count"] == 16 and answer["note"] is None,
            "real tool/JSON closure differs")
    return answer


def restore_record(log, request_id):
    rows=re.findall(r"\[session-cache\] request="+re.escape(request_id)+
        r" event=restore prefix=(\d+) ms=[^ ]+ error=(\S+) source=(\w+) layout=(\w+)",log)
    require(len(rows)==1,"missing or ambiguous restore record: "+request_id)
    prefix,error,source,layout=rows[0]
    require(error=="none" and layout=="target","cache restore error/layout")
    return dict(prefix=int(prefix),source=source,layout=layout)


def prepare(args, *, rows=None, limits=LIMITS, scope=None):
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),"artifacts must be inside workspace")
    require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
            "commit reviewed sources before freezing")
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    binary=args.binary.resolve(strict=True)
    model=Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
    names=subprocess.check_output(['git','ls-files','src','include','cmake','CMakeLists.txt',
        'tools/serve_schema_v2','tools/serve_custom_tools','tools/serve_grammar_reap',
        'tools/serve_protocol','tools/examples','tools/evalscope','tools/serve_recovery',
        'tools/serve_service','tools/serve_gateway','tools/release',DOCUMENT,
        'third_party/licenses/structured_output'],cwd=ROOT,text=True).splitlines()
    protected=[ROOT/p for p in ('build/q4t','build/q4t.release.json',
        'build/service.release.json','build/service/manifest.json')]
    plan=dict(version=1,limits=limits,output=str(out),binary=str(binary),
        binary_sha256=sha(binary),model=str(model),model_assets=assets(model),
        port=args.port,operations_port=args.port+1,
        requests=requests() if rows is None else rows,
        sources={name:sha(ROOT/name) for name in names},
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        protected={str(p):sha(p) for p in protected},sdk=sdk_identity(),
        planned=dict(starts=limits['starts'],chats=limits['chats']),automatic_retries=False,
        scope=scope or "Rich schema JSON/SSE, exact data, shared/private target cache, real document tool/JSON closure, failure nonpublication and same-PID MTP. No full correctness/performance matrix.")
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),sources=len(names))))


class Stage(lifecycle.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_SCHEMA_V2_TEXT_BUSINESS'

    def __init__(self,plan):
        self.plan,self.out=plan,Path(plan['output']);self.started=time.monotonic()
        self.counts=dict(starts=0,chats=0,controls=0)
        self.model=self.model_identity=self.fault_thread=None
        self.history={};self.fault_result={}
        self.lease=int(os.environ['Q4T_MANAGED_LEASE_FD'])
        lifecycle.verify_lease(self.lease,lifecycle.guard.common_lock())
        require(plan['limits']==self.limits and
                plan['requests']==self.request_templates(),'frozen plan drift')
        for name,digest in plan['sources'].items():require(sha(ROOT/name)==digest,'source drift: '+name)
        for name,digest in plan['protected'].items():require(sha(name)==digest,'default drift: '+name)
        require(sha(plan['binary'])==plan['binary_sha256'],'binary drift')
        require(sdk_identity()==plan['sdk'],'SDK drift')
        check_assets(plan['model'],plan['model_assets'])
        self.client=JsSdkClient(f"http://127.0.0.1:{plan['port']}/v1",
            plan['sdk']['sdk_directory'],timeout=180)

    def budget(self,kind):
        require(time.monotonic()-self.started<self.limits['seconds']-40,'stage deadline')
        require(self.counts[kind]<self.limits[kind],'attempt limit: '+kind)
        self.counts[kind]+=1;save(self.out/'counts.json',self.counts)

    def start(self):
        for port in (self.plan['port'],self.plan['operations_port']):
            with socket.socket() as sock:sock.bind(('127.0.0.1',port))
        for folder in ('private-cache','shared-cache'):(self.out/folder).mkdir(mode=0o700)
        args=[self.plan['binary'],'serve','--model-dir',self.plan['model'],'--host','127.0.0.1',
            '--port',str(self.plan['port']),'--operations-port',str(self.plan['operations_port']),
            '--mtp','--max-seq','1','--max-len','16384','--seq-capacities','16384',
            '--max-prefill','2048','--max-tokens','384','--max-inflight-requests','8',
            '--max-queued-requests','2','--session-cache-dir',str(self.out/'private-cache'),
            '--prefix-cache-dir',str(self.out/'shared-cache'),'--prefix-cache-checkpoints','1024']
        save(self.out/'model-command.json',args);self.budget('starts')
        with (self.out/'model.log').open('xb',buffering=0) as log:
            self.model=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                pass_fds=(self.lease,))
        self.model_identity=lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid]=self.model_identity
        save(self.out/'model-identity.json',dict(pid=self.model.pid,**self.model_identity))
        end=time.monotonic()+180
        while time.monotonic()<end:
            self.same_model()
            try:
                status,_=self.http('/readyz',timeout=1)
                if status==200:break
            except (OSError,http.client.HTTPException):pass
            time.sleep(.1)
        else:raise TimeoutError('model readiness')
        for name in ('capabilities','config'):
            status,raw=self.http('/v1/'+name,label='initial-'+name)
            require(status==200,name+' unavailable');save(self.out/(name+'.json'),json.loads(raw))
        caps=json.loads((self.out/'capabilities.json').read_text())
        require(caps['structured_output']['schema_profile']=='q4t-json-schema-v2', 'schema profile')
        self.client.protocol_sha256=caps['protocol_source_sha256']

    def sdk_request(self,label,body):
        self.budget('controls')
        status,raw=self.http('/v1/chat/preflight',dict(request=body,
            include_prompt=True,include_token_ids=True),label=label+'-preflight')
        require(status==200,'preflight failed: '+label)
        self.budget('chats');save(self.out/(label+'.request.json'),body)
        with (self.out/(label+'.wire')).open('xb') as wire:
            answer=self.client.complete(body,sink=wire.write)
        save(self.out/(label+'.sdk.json'),self.client.last_result)
        save(self.out/(label+'.result.json'),asdict(answer))
        self.same_model();self.drained(label+'-drain');return answer

    def record(self,body):
        return restore_record((self.out/'model.log').read_text(),body['request_id'])

    def business(self):
        rows=self.plan['requests']
        cold=self.sdk_request('features',rows['features']);check_features(cold)
        require(8192<=cold.usage['prompt_tokens']<=14500,'private cache prompt bound')
        warm=self.sdk_request('warm',rows['warm']);check_features(warm)
        require(cold.message==warm.message and cold.logprobs==warm.logprobs,
                'same cache computation boundary changed token probabilities')
        require(self.record(rows['warm'])==dict(prefix=1024,source='shared',layout='target'),
                'shared cache did not restore')
        private=tagged('private',rows['features']);private.pop('prefix_cache_scope');private.pop('prefix_cache_write_tokens')
        private['messages'] += [dict(role='assistant',content=warm.message['content']),
            dict(role='user',content='Return the same JSON object again, with node.children empty and note null.')]
        value=self.sdk_request('private',private);check_features(value)
        record=self.record(private)
        require(record['prefix']>=8192 and record['source']=='session','private target cache missed')
        call=self.sdk_request('tool',rows['tool']);require_fragments((self.out/'tool.wire').read_bytes())
        plans=prepare_tool_calls(call,VALIDATORS)
        require(len(plans)==1 and plans[0].name=='read_project_record' and
            plans[0].arguments==dict(path=DOCUMENT,start_line=1,line_count=16),'real read arguments differ')
        result=ProjectRecords(ROOT).execute(plans[0])
        require(result['ok'] and result['source']['sha256']==self.plan['sources'][DOCUMENT],
                'document source identity differs')
        save(self.out/'tool-execution.json',dict(plan=asdict(plans[0]),result=result))
        follow=tagged('final',rows['tool']);follow.update(stream=False,tool_choice='auto');follow.pop('stream_options')
        follow['messages']=append_tool_results(follow['messages'],call,
            {plans[0].call_id:json.dumps(result,ensure_ascii=False)})
        follow['messages'].append(dict(role='developer',content='Return the actual document title, exact citation, requested_line_count 16 and note null as one JSON object. The tool result is already available.'))
        final=self.sdk_request('final',follow)
        check_final(final,result)
        self.remainder()

    def remainder(self, total_requests=8):
        rows=self.plan['requests']
        body=rows['truncated'];self.budget('chats');save(self.out/'truncated.request.json',body)
        with (self.out/'truncated.wire').open('xb') as wire:
            try:self.client.complete(body,sink=wire.write)
            except JsSdkError as error:
                require(error.details.get('code')=='structured_output_incomplete','wrong length error')
                save(self.out/'truncated.result.json',dict(error=error.details,tools_executed=0))
            else:raise RuntimeError('incomplete structured response claimed success')
        raw=(self.out/'truncated.wire').read_bytes()
        events=[json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: {')]
        require(raw.count(b'data: [DONE]')==1 and not any(e.get('usage') for e in events) and
            not any(c.get('finish_reason') for e in events for c in e.get('choices',[])),
            'length error claimed successful finish/usage')
        self.drained('truncated-drain')
        recovery=self.sdk_request('recovered',rows['recovered']);check_features(recovery)
        require(self.record(rows['recovered'])['prefix']==0,'failed request published cache')
        final=self.sdk_request('greedy',rows['greedy'])
        require(final.finish_reason=='stop' and final.message['content'].strip()=='42','MTP continuation failed')
        require('[mtp-accept] request='+rows['greedy']['request_id'] in (self.out/'model.log').read_text(),
                'normal greedy request did not exercise MTP')
        metrics=self.drained('final')
        require(metrics['q4t_requests_total']==total_requests and
            metrics['q4t_requests_success_total']==total_requests-1 and
            metrics['q4t_requests_error_total']==1,'terminal accounting differs')

    def run(self):
        result=dict(status='FAILED')
        try:self.start();self.business();result['status']=self.success_status
        except BaseException:result['error']=traceback.format_exc();raise
        finally:
            try:self.cleanup()
            except BaseException:
                result['status']='FAILED';result['cleanup_error']=traceback.format_exc();raise
            finally:
                result.update(counts=self.counts,seconds=time.monotonic()-self.started,
                              binary_sha256=self.plan['binary_sha256'])
                save(self.out/'result.json',result)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--port',type=int,default=18164)
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    else:
        with lifecycle.child_subreaper():Stage(json.loads(args.plan.read_text())).run()


if __name__=='__main__':main()
