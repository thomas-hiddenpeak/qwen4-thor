"""Last frozen model start: default text/MTP, JSON/SSE and real tool loop.

Keeps the installed default untouched during acceptance. Same candidate ELF
as media phases; text numerical/performance code is inherited, not requalified
as an optimization. No long soak or automatic retries.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.vision_service import stage as s

FOLDER=s.BASE/'phase-07'


def prepare():
    prior=s.BASE/'phase-06'
    result=json.loads((prior/'execution/result.json').read_text())
    s.require(result['status']=='DIAGNOSTIC_RESPONSES_RECORDED_NOT_QUALITY_ACCEPTANCE' and
              not result['cleanup_errors'] and not result['monitor_failures'],'diagnostic custody')
    s.require(json.loads((prior/'lease/exit.json').read_text())['status']=='COMPLETED','diagnostic guard')
    plan=json.loads((prior/'plan.json').read_text())
    s.require(s.sha(plan['binary'])==plan['files'][plan['binary']],'candidate changed')
    # Count attempts, including the failed pre-load start, against total six.
    phases=('phase-01','phase-02','phase-03','phase-04','phase-05','phase-05b','phase-06')
    counts=[json.loads((s.BASE/p/'execution/result.json').read_text())['counts'] for p in phases]
    s.require(sum(c['starts'] for c in counts)==5 and sum(c['generations'] for c in counts)==65 and
              sum(c['native'] for c in counts)==5,'cumulative budget')
    FOLDER.mkdir()
    rows=[]
    for stream in (False,True):
        rows.append(dict(label='text-'+('sse' if stream else 'json'),
            request=s.request('Compute 17 + 25. Reply with the number only.',stream=stream),expected='42'))
    rows.append(dict(label='text-structured',request=s.request('Compute 17 + 25. Return JSON with integer key answer.',
        response_format=s.structured(dict(answer=dict(type='integer')))),expected=dict(answer=42)))
    s.save(FOLDER/'requests.json',rows)
    paths=[ROOT/n for n in subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
           if n.startswith(('src/','include/','cmake/','tools/','docs/serve-')) or n=='CMakeLists.txt']
    paths += [Path(n) for n in plan['files'] if Path(n).is_file()]
    paths += [FOLDER/'requests.json',prior/'execution/result.json',prior/'lease/exit.json']
    plan.update(files={str(p):s.sha(p) for p in paths},matrix=dict(generations=5,models=1,real_tool_calls=1),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    s.save(FOLDER/'plan.json',plan)
    return plan['matrix']


class Run(s.Run):
    success_status='PASS_DEFAULT_TEXT_MTP_PROTECTION'

    def launch(self):
        self.budget('starts')
        argv=[self.plan['binary'],'serve','--model-dir',self.plan['model'],'--host','127.0.0.1',
              '--port',str(s.PORT),'--max-seq','1','--max-prefill','2048','--max-len','8192',
              '--max-inflight-requests','8','--max-queued-requests','4']
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k not in ('LD_PRELOAD','LD_LIBRARY_PATH')}
        self.model=self.children.launch(self.output/'model',argv,env)
        end=time.monotonic()+180
        while 'serving on port' not in (self.output/'model/stderr.log').read_text():
            s.require(self.children.running(self.model) and time.monotonic()<end,'startup failed');time.sleep(.1)
        for label,path in (('caps','/v1/capabilities'),('config','/v1/config')):
            value=json.loads(self.http(label,path))
            s.require(value['runtime']['mtp_enabled'] is True and value['runtime']['media_enabled'] is False,
                      'default profile changed')

    def business(self):
        for row in json.loads((self.folder/'requests.json').read_text()):
            value=self.complete(row['label'],row['request'])
            actual=value.message.get('content','').strip()
            if isinstance(row['expected'],dict):actual=json.loads(actual)
            self.observe(row['label'],actual==row['expected'] and value.finish_reason=='stop',actual=actual)
        tools=copy.deepcopy(s.TOOLS[:1]);tools[0]['function']['strict']=True
        body=s.request('Read the first 16 lines of docs/STATUS.md using read_project_record. Then summarize the current stage in one sentence and cite the actual document.',
            stream=True,tools=tools,tool_choice=dict(type='function',function=dict(name='read_project_record')),
            parallel_tool_calls=False)
        value=self.complete('text-tool-call',body)
        calls=s.client.prepare_tool_calls(value,s.VALIDATORS)
        s.require(len(calls)==1 and calls[0].arguments['path']=='docs/STATUS.md','tool call identity')
        result=s.ProjectRecords(ROOT).execute(calls[0]);s.require(result['ok'],'actual read-only tool')
        s.save(self.output/'real-tool-result.json',result)
        history=s.client.append_tool_results(body['messages'],value,{calls[0].call_id:json.dumps(result,ensure_ascii=False)})
        follow=s.client.make_request(history,tools,stream=False,max_tokens=256,timeout_ms=120000)
        follow['tool_choice']='none'
        value=self.complete('text-tool-final',follow)
        self.observe('text-tool-loop',value.finish_reason=='stop' and 'docs/STATUS.md' in value.message.get('content',''),
                     content=value.message.get('content'))
        self.idle('final-idle')


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run'))
    print(json.dumps(prepare() if p.parse_args().action=='prepare' else Run(FOLDER).run()))
