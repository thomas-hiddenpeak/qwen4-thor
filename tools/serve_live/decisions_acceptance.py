"""Bounded SDK Decisions, model scoring, cache and quota check on real project records."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_live import responses_acceptance as live
from tools.serve_realtime import transcription_acceptance as base
from tools.serve_recovery import profile_acceptance as qualify
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=1, ingress_starts=1, decisions=4, model_questions=5,
              chats=1, controls=1800, seconds=420)
SCHEMA = 'q4t-decisions-business-v1'


def prepare(out, qualification):
    live.prepare(out, qualification)
    plan = read(out/'plan.json')
    plan.update(schema=SCHEMA, limits=LIMITS, port=18681, operations_port=18682, public_port=18683)
    plan['records'] = {
        name: dict(path=str(ROOT/path), sha256=sha(ROOT/path)) for name,path in (
            ('status','docs/STATUS.md'), ('evidence','docs/evidence/serve-live-responses-http-20261123/result.json'))}
    save(out/'plan.json',plan)


def observe(out):
    sdk = read(out/'sdk.json')
    require(sdk['status']=='PASS' and sdk['attempted']==['invalid','project','write','read']
            and sdk['invalid']=={'http':400,'code':'invalid_request'}, 'actual SDK requests')
    answers = sdk['responses']
    require(set(answers)=={'project','write','read'}, 'three successful requests')
    version=read(Path(read(out/'plan.json')['records']['evidence']['path']))['observation']['version']
    for name,result in answers.items():
        require(result['model']=='qwen3.8-flash-next' and set(result)=={'model','answers','usage'},'standard result')
        u=result['usage']
        require(u['input_tokens']>0 and u['output_tokens']==len(result['answers']) and
                u['total_tokens']==u['input_tokens']+u['output_tokens'] and
                0<=sum(u['input_tokens_details'].values())<=u['input_tokens'] and
                u['output_tokens_details']['reasoning_tokens']==0,'actual classification usage')
    predicate,choice,score=answers['project']['answers']
    require([v['name'] for v in (predicate,choice,score)]==['matches_record','version','agreement']
            and [v['type'] for v in (predicate,choice,score)]==['predicate','choice','score'],'question order')
    for answer in (score,choice):
        require(0<=answer['confidence']<=1 and all(0<=v['probability']<=1 for v in answer['probabilities'])
                and math.isclose(sum(v['probability'] for v in answer['probabilities']),1,abs_tol=1e-8),'probability distribution')
    require([v['value'] for v in choice['probabilities']]==['rev000',version,'unknown']
            and [v['label'] for v in score['probabilities']]==['different','unclear','match']
            and math.isclose(score['score'],sum(v['value']*v['probability'] for v in score['probabilities']),abs_tol=1e-8),'typed option and weighted level projection')
    before,after=read(out/'before.json'),read(out/'decisions-drained.json')
    def metric(values,key): return values[key+'{client="alpha"}']
    require(metric(after,'q4t_client_rate_admitted_total')-metric(before,'q4t_client_rate_admitted_total')==3 and
            metric(after,'q4t_client_rate_reserved_tokens')==0,'one RPM per accepted request, no leaked reservation')
    total=sum(v['usage']['total_tokens'] for v in answers.values())
    require(metric(after,'q4t_client_rate_settled_tokens_total')-metric(before,'q4t_client_rate_settled_tokens_total')==total,'actual tokens match returned usage')
    written=answers['write']['usage']['input_tokens_details']['cache_write_tokens']
    cached=answers['read']['usage']['input_tokens_details']['cached_tokens']
    positive,negative=(answers[k]['answers'][0]['probability'] for k in ('write','read'))
    return dict(protocol='PASS',actual_questions=5,settled_tokens=total,cache_written=written,cache_read=cached,
                cache_verified=cached>=1024 and written>=1024,version=version,choice=choice['choice'],
                positive=positive,negative=negative,agreement=score['score'],matches=predicate['probability'],
                quality=(choice['choice']==version and predicate['probability']>.5 and score['score']>1
                         and positive>.5 and negative<.5))


class Stage(base.Stage):
    limits = LIMITS

    def extra_model_args(self):
        return ['--object-store-dir',str(self.out/'objects')]

    def business(self):
        self.snapshot('before')
        self.budget('decisions',4)
        self.budget('model_questions',5)
        args=dict(base_url=f"https://127.0.0.1:{self.plan['public_port']}/v1",api_key=self.keys['alpha'],**self.plan['records'])
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        env.update(Q4T_NODE_SDK_DIR=self.plan['sdk_directory'],NODE_EXTRA_CA_CERTS=self.plan['tls']['certificate'])
        with (self.out/'sdk.json').open('xb') as output,(self.out/'sdk.stderr').open('xb') as errors:
            actor=subprocess.run(['node',str(ROOT/'tools/serve_live/decisions_sdk.mjs')],input=json.dumps(args).encode(),
                                 stdout=output,stderr=errors,timeout=225,env=env)
        self.same_model()
        self.snapshot('decisions-drained')
        self.core('core-after')
        self.snapshot('final-drained')
        require(actor.returncode==0,'original Decisions SDK failed')
        result=observe(self.out)
        save(self.out/'observation.json',result)
        require(result['quality'] and result['cache_verified'],'decision model answers and real shared-prefix read/write')


def finalize(out):
    result=read(out/'result.json')
    require(result['status']=='PASS' and result['seconds']<=LIMITS['seconds'] and
            all(result['counts'][k]==v for k,v in LIMITS.items() if k not in ('seconds','controls')), 'bounded actual work')
    env=qualify.environment(read(out/'lease/exit.json'),
                            [json.loads(row) for row in (out/'lease/nvml-queries.jsonl').read_text().splitlines()],
                            [read(out/'cleanup.json')],expected_starts=1)
    observed=observe(out)
    require(observed==read(out/'observation.json') and observed['quality'] and observed['cache_verified'],'independent verdict')
    save(out/'acceptance.json',dict(environment=env,observation=observed,seconds=result['seconds'],
                                    default_unchanged=result['default_unchanged'],core='42'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','finalize'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--qualification',type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'),'private work directory')
    if args.action=='prepare':
        require(args.qualification is not None,'expected current candidate and ingress identities')
        prepare(out,args.qualification)
    elif args.action=='run':
        plan=read(out/'plan.json')
        require(plan['schema']==SCHEMA,'Decisions stage identity')
        with base.lifecycle.child_subreaper():Stage(plan).run()
    else:finalize(out)
