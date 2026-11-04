"""Targeted native policy fixture repair; retain first 16 observations and rerun seven."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_prepare_lock import cpu,stage

h,s=stage.h,stage.s
BASE=stage.BASE
ORIGINAL=stage.PACKAGE
FAILED=stage.OUTPUT
PACKAGE,OUTPUT=BASE/'package-native-fix',BASE/'execution-02'
PORT,OPS_PORT,TLS_PORT=stage.PORT,stage.OPS_PORT,stage.TLS_PORT
LIMITS=stage.LIMITS
FIXTURE='tools/serve_prepare_lock/probe.cpp'
SOURCE='tools/serve_prepare_lock/native_fix.py'


def prior_gate():
    result=cpu.read(FAILED/'business/result.json')
    guard=cpu.read(FAILED/'lease/exit.json')
    cpu.require(result['status']=='FAILED' and result['counts']==dict(
        starts=0,generation_attempts=0,controls=0) and guard['status']=='COMMAND_FAILED' and
        guard['child_returncode']==1,'only zero-model fixture failure is eligible')
    native=FAILED/'business/native'
    cpu.require(cpu.read(native/'waitable.json')['returncode']==1 and
        (native/'stderr.log').read_text().splitlines()[-1]=='prepare-lock probe failed: policy',
        'unexpected native failure')
    owner=cpu.read(native/'identity.json')
    cpu.require(not Path('/proc/'+str(owner['pid'])).exists(),'old native process remains')
    cpu.require(not (FAILED/'release').exists(),'failed phase installed a release')
    rows=[json.loads(line) for line in (native/'stdout.log').read_text().splitlines()]
    expected={(m,c) for m in ('encode-json','encode-sse','thinking-json','thinking-sse')
              for c in ('explicit','deadline','disconnect','shutdown')}
    cpu.require(len(rows)==16 and {(r['mode'],r['cause']) for r in rows}==expected and
        all(r['passed'] is True and r['returned_before_owner_release'] is True and
            0<=r['seconds']<1.1 for r in rows),'partial observation set changed')
    return rows


def verify_repaired(rows,returncode):
    cpu.require(len(rows)==7,'only remaining native cases may run')
    stage.verify_native(prior_gate()+rows,returncode)


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(),'preserve repaired attempt')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit fixture repair first')
    prior_gate();h.verify_seal(ORIGINAL)
    sources=cpu.read(ORIGINAL/'sources.json')
    cpu.require({n for n,d in sources.items() if cpu.sha(ROOT/n)!=d}=={FIXTURE},
                'unreviewed repair source changes')
    built=cpu.read(BASE/'build-native-fix/result.json')
    cpu.require(built['passed'] and cpu.sha(cpu.ENGINE/'q4t')==built['production_sha256'] and
        cpu.sha(cpu.ENGINE/'q4t_prepare_lock_probe')==built['native_sha256'],'repair build')
    tested=cpu.read(BASE/'driver-native-fix/result.json')
    cpu.require(tested['passed'],'repair driver gate')
    for name,digest in tested['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'repair source drift')
    h.seal(FAILED)
    PACKAGE.mkdir()
    skip={'sources.json','plan.json','sealed-files.json','candidate/q4t_prepare_lock_probe'}
    for path in sorted(ORIGINAL.rglob('*')):
        rel=path.relative_to(ORIGINAL)
        if path.is_file() and str(rel) not in skip:
            target=PACKAGE/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
    h.copy_checked(h.identity(cpu.ENGINE/'q4t_prepare_lock_probe'),PACKAGE/'candidate/q4t_prepare_lock_probe')
    sources[FIXTURE]=cpu.sha(ROOT/FIXTURE);sources[SOURCE]=cpu.sha(ROOT/SOURCE)
    cpu.save(PACKAGE/'sources.json',sources)
    plan=cpu.read(ORIGINAL/'plan.json')
    cpu.require(plan['candidate_sha256']==built['production_sha256'],'production ELF changed')
    plan.update(candidate=str(PACKAGE/'candidate/q4t'),binary=str(PACKAGE/'candidate/q4t'),
        original_source_commit=plan['source_commit'],source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    for path in (FAILED/'sealed-files.json',BASE/'build-native-fix/result.json',BASE/'driver-native-fix/result.json'):
        plan['local'][str(path)]=cpu.sha(path)
    plan['native_repair']=dict(original_package=h.identity(ORIGINAL/'sealed-files.json'),
        original_failure=h.identity(FAILED/'sealed-files.json'),reused_cases=16,new_cases=7,
        changed=[FIXTURE],production_unchanged=True,prior_models=0,prior_generations=0,
        partial_scope='16 completed unscoped cases reused; original process/phase failed on invalid scoped fixture',
        new_native_sha256=built['native_sha256'])
    cpu.save(PACKAGE/'plan.json',plan);h.seal(PACKAGE)
    return dict(prepared=True,models_started=0,reused_native_cases=16,remaining_native_cases=7)


def driver():
    out=BASE/'driver-native-fix';out.mkdir()
    rows=[dict(mode=m,cause=c,passed=True,returned_before_owner_release=True,seconds=.01)
          for m in ('scoped-sse','preflight') for c in ('explicit','deadline','disconnect','shutdown')
          if (m,c)!=('preflight','explicit')]
    verify_repaired(rows,0)
    rejected=0
    for items,code in ((rows[:-1],0),(rows,1),(rows[:-1]+rows[:1],0)):
        try:verify_repaired(items,code)
        except RuntimeError:rejected+=1
        else:raise RuntimeError('incomplete repair evidence accepted')
    compile((ROOT/SOURCE).read_bytes(),SOURCE,'exec')
    cpu.save(out/'result.json',dict(passed=True,synthetic_rejections=rejected,
        sources={n:cpu.sha(ROOT/n) for n in (SOURCE,FIXTURE)},models=0,http=0,gpu=0))


class Run(stage.Run):
    def execute(self):
        result=dict(status='FAILED',limits=LIMITS,counts=self.counts)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT,OPS_PORT,TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    sock.bind(('127.0.0.1',port))
            folder=s.OUT/'native'
            native=self.children.launch(folder,[str(PACKAGE/'candidate/q4t_prepare_lock_probe'),'--remaining'])
            self.children.wait(native,folder,15)
            rows=[json.loads(line) for line in (folder/'stdout.log').read_text().splitlines()]
            verify_repaired(rows,cpu.read(folder/'waitable.json')['returncode'])
            cpu.save(s.OUT/'native-result.json',dict(passed=True,cases=rows,models=0,reused=h.identity(FAILED/'business/native/stdout.log'),reused_cases=16))
            self.launch()
            self.request('capabilities',path='/v1/capabilities',method='GET')
            self.cancel_active()
            body=s.a.ordinary(True);body['thinking_token_budget']=0
            value=self.complete('thinking-recovery',body)
            cpu.require(value.finish_reason=='stop' and value.message['content'].strip()=='710003',
                        'thinking preparation/recovery quality')
            for client,name in enumerate(('alpha','beta')):
                self.current_client=client;self.tools('project-'+name)
            metrics=self.idle('final-idle');cpu.save(s.OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==5 and self.cancelled==1 and
                metrics['q4t_requests_aborted_total']==1 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,
                'request/grammar accounting')
            self.stop(True);self.children.reap(s.OUT);s.a.require_idle(s.OUT/'final-gpu.json')
            result.update(status='PASS_BOUNDED_PREPARE_LOCK',successes=self.successes,
                cancelled=self.cancelled,native_cases=len(rows),reused_native_cases=16,total_native_cases=23,real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            for connection in self.sockets:connection.close()
            try:self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED',cleanup_error=repr(error));raise
            finally:
                result['seconds']=time.monotonic()-self.started;cpu.save(s.OUT/'result.json',result)


def bind():
    prior_gate();h.verify_seal(FAILED)
    stage.PACKAGE,stage.OUTPUT,stage.Run=PACKAGE,OUTPUT,Run
    stage.check()


def execute():bind();stage.execute()

def finalize():bind();stage.finalize()


if __name__=='__main__':
    os.umask(0o077);parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('driver','prepare','execute','finalize'))
    print(json.dumps(globals()[parser.parse_args().action]() or dict(passed=True)))
