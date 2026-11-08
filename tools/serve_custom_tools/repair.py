"""One failed mixed-tool task plus three previously unexecuted requests.

The candidate ELF and all production/client sources remain identical. This
repairs the business prompt's mixed-call format example, not its requirement.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_custom_tools import acceptance as base
from tools.serve_service.package import read,save,sha,require
from tools.examples.chat_client import prepare_tool_calls,append_tool_results
from tools.examples.project_records import ProjectRecords,VALIDATORS,CUSTOM_VALIDATORS

REPAIR_LIMITS=dict(starts=1,chats=4,controls=2000,seconds=800)
CUMULATIVE_LIMITS=dict(starts=2,chats=9,seconds=900)


def prepare(args):
    previous=args.previous.resolve(strict=True)
    old=read(previous/'plan.json');result=read(previous/'result.json');cleanup=read(previous/'cleanup.json')
    require(result['status']=='FAILED' and result['counts']['starts']==1 and result['counts']['chats']==5 and
        result['error'].endswith('RuntimeError: mixed calls missing\n'),'unexpected prior failure')
    require(cleanup['model_exit']==0 and not cleanup['remaining'] and cleanup['ports_released'],'old resources remain')
    require(sha(args.binary)==old['binary_sha256']==result['binary_sha256'],'candidate identity changed')
    for name in ('text','text-final','regex','lark'):
        require((previous/(name+'.result.json')).is_file(),'prior accepted request missing')
    for name,digest in old['sources'].items():
        blob=subprocess.check_output(['git','show',old['source_commit']+':'+name],cwd=ROOT)
        import hashlib
        require(hashlib.sha256(blob).hexdigest()==digest,'prior Git source mismatch')
        if name not in ('tools/serve_custom_tools/acceptance.py','tools/serve_custom_tools/test_acceptance.py'):
            require(sha(ROOT/name)==digest,'production/client source changed')
    require(all(value==base.requests()[name] for name,value in old['requests'].items() if name!='mixed'),'unrelated request drift')
    base.prepare(args)
    plan=read(args.output/'plan.json')
    plan.update(planned=dict(starts=1,chats=4),repair_limits=REPAIR_LIMITS,cumulative_limits=CUMULATIVE_LIMITS,
        previous=str(previous),previous_plan_sha256=sha(previous/'plan.json'),previous_result_sha256=sha(previous/'result.json'),
        reused_requests=['text','text-final','regex','lark'],repair='Explicit mixed-call example; still require both actual tool types and execute both')
    save(args.output/'plan.json',plan)


class Repair(base.Stage):
    def __init__(self,plan):
        require(plan['repair_limits']==REPAIR_LIMITS and plan['cumulative_limits']==CUMULATIVE_LIMITS,'repair budget drift')
        require(sha(Path(plan['previous'])/'plan.json')==plan['previous_plan_sha256'] and
                sha(Path(plan['previous'])/'result.json')==plan['previous_result_sha256'],'prior evidence changed')
        super().__init__(plan)

    def budget(self,kind):
        require(time.monotonic()-self.started<REPAIR_LIMITS['seconds']-40,'repair deadline')
        require(self.counts[kind]<REPAIR_LIMITS[kind],'repair attempt limit')
        super().budget(kind)

    def business(self):
        caps=read(self.out/'capabilities.json')
        self.client.protocol_sha256=caps['protocol_source_sha256']
        body=self.plan['requests']['mixed'];mixed=self.sdk_request('mixed',body)
        base.require_fragments((self.out/'mixed.wire').read_bytes())
        plans=prepare_tool_calls(mixed,VALIDATORS,custom_validators=CUSTOM_VALIDATORS)
        require(len(plans)==2 and {p.name for p in plans}=={'read_project_record_regex','read_project_record'},'mixed calls missing')
        results={}
        for call in plans:
            if getattr(call,'type',None)=='custom':require(call.input=='READ docs/MODEL.md 1 16','custom input differs')
            else:require(call.arguments==dict(path='docs/ARCHITECTURE.md',start_line=1,line_count=8),'function arguments differ')
            value=ProjectRecords(ROOT).execute(call)
            require(value['ok'] and value['source']['sha256']==self.plan['sources'][value['source']['path']],'actual source mismatch')
            results[call.call_id]=value
        save(self.out/'mixed-execution.json',dict(executions=2,plans=[asdict(p) for p in plans],results=results))
        history=append_tool_results(body['messages'],mixed,{k:json.dumps(v,ensure_ascii=False) for k,v in results.items()})
        save(self.out/'mixed-history.json',history)
        self.truncated_tool();self.cancel_tool()
        final=self.sdk_request('greedy-after-cancel',self.plan['requests']['greedy'])
        require(final.finish_reason=='stop' and final.message['content'].strip()=='42','normal MTP continuation')
        metrics=self.drained('final')
        require(metrics['q4t_requests_total']==4 and metrics['q4t_requests_success_total']==2 and
            metrics['q4t_requests_aborted_total']==1 and metrics['q4t_requests_error_total']==1,'repair accounting')


def main():
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='mode',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--binary',type=Path,required=True);prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--previous',type=Path,required=True);prep.add_argument('--port',type=int,default=18164)
    run=sub.add_parser('run');run.add_argument('--plan',type=Path,required=True);args=ap.parse_args()
    if args.mode=='prepare':prepare(args)
    else:
        with base.lifecycle.child_subreaper():Repair(read(args.plan)).run()


if __name__=='__main__':main()
