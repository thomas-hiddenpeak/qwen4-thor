"""One targeted startup-stop recheck using the unchanged production CLI."""
import argparse
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_supervisor_deadlines import stage as s
from tools.serve_service import lease_custody

BASE=s.BASE
PLAN=BASE/'recheck-plan.json'
OUT=BASE/'startup-recheck'
SECONDS=100
pkg=s.pkg


def changes():
    names=('tools/serve_supervisor_deadlines/finish.py',
        'tools/serve_supervisor_deadlines/test_finish.py',
        'tools/serve_supervisor_deadlines/closeout.py',
        'docs/SERVE_SUPERVISOR_DEADLINES_RECHECK_2026-11-04.md')
    return {n:pkg.sha(ROOT/n) for n in names}


def cpu():
    from tools.serve_supervisor_deadlines import test_finish
    out=BASE/'recheck-cpu';out.mkdir(mode=0o700)
    source=changes();log=io.StringIO()
    suite=unittest.defaultTestLoader.loadTestsFromModule(test_finish)
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    passed=result.wasSuccessful() and not result.skipped and changes()==source
    pkg.save(out/'result.json',dict(passed=passed,tests=result.testsRun,sources=source,
        models=0,http=0,gpu=0))
    pkg.require(passed,'targeted driver CPU failure')
    print(json.dumps(dict(passed=True,tests=result.testsRun)))


def prepare():
    s.configure();original=s.check()
    tested=pkg.read(BASE/'recheck-cpu/result.json')
    pkg.require(tested['passed'] and tested['sources']==changes(),'recheck CPU/source')
    pkg.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit recheck')
    pkg.require(not PLAN.exists() and not OUT.exists(),'preserve earlier attempt')
    pkg.require(pkg.read(s.OUT/'result.json')['status']=='ENV_ABORT' and
        pkg.read(BASE/'postflight/result.json')['passed'],'original failure/cleanup')
    pkg.save(PLAN,dict(package=original['package'],original_plan_sha256=pkg.sha(s.PLAN),
        original_result_sha256=pkg.sha(s.OUT/'result.json'),sources=changes(),
        cpu_sha256=pkg.sha(BASE/'recheck-cpu/result.json'),seconds=SECONDS,
        attempts=1,complete_models=0,generation_attempts=0,real_tools=0,
        observe_seconds=20,stop_seconds=5,gpu_timer_seconds=60,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    check()


def check():
    s.configure();original=s.check();plan=pkg.read(PLAN)
    pkg.require(plan['sources']==changes() and plan['package']==original['package'] and
        plan['original_plan_sha256']==pkg.sha(s.PLAN) and
        plan['original_result_sha256']==pkg.sha(s.OUT/'result.json') and
        plan['cpu_sha256']==pkg.sha(BASE/'recheck-cpu/result.json') and
        plan['seconds']==SECONDS and plan['attempts']==1 and
        plan['generation_attempts']==plan['complete_models']==0,'recheck identity/bounds')
    return plan


def warming(directory):
    path=directory/'status.json'
    if not path.exists():return None
    state=pkg.read(path)
    pkg.require(state['state'] not in ('FAILED','STOPPED','READY','DEGRADED'),'not unready startup')
    if not state.get('backend'):return None
    log=(directory/'backend.log').read_text() if (directory/'backend.log').exists() else ''
    peers=s.established_probe(state['owner']['pid'],s.base.OPS_PORT)
    if not s.startup_snapshot(state,log,peers):return None
    primary=s.sup.identity.process(state['backend']['pid'],s.base.PORT)
    operations=s.sup.identity.operations(primary,s.base.OPS_PORT)
    return dict(state=state,peers=peers,primary=primary,operations=operations,
        monotonic=time.monotonic(),backend_log_sha256=pkg.sha(directory/'backend.log'))


class Run:
    def __init__(self,plan):
        self.plan=plan;self.directory=OUT/'managed-run'
        self.process=None;self.custody=None;self.log=None;self.owner=None
        self.started=time.monotonic()

    def wait(self,predicate,seconds,description):
        until=time.monotonic()+seconds
        while True:
            if self.custody is not None:self.custody.check()
            value=predicate()
            if value:return value
            pkg.require(time.monotonic()<until,description)
            time.sleep(.02)

    def cleanup(self):
        errors=[]
        if self.process is not None:
            try:
                if (self.directory/'controller.json').exists():s.service.stop(self.directory)
                elif self.process.poll() is None:self.process.terminate()
                self.process.wait(35)
            except BaseException as error:
                errors.append(repr(error))
                try:s.service.guard.stop_owned(self.process,2)
                except BaseException as failure:errors.append(repr(failure))
        try:
            s.base.client.a.require_idle(OUT/'final-gpu.json')
            pkg.require(self.process is None or self.process.poll() is not None,'controller retained')
            pkg.require(not any(s.base.inspection.listeners(p) for p in
                (s.base.PORT,s.base.OPS_PORT,s.base.TLS_PORT)),'listener retained')
        except BaseException as error:errors.append(repr(error))
        if self.custody is not None:
            try:self.custody.finish(cleanup_confirmed=not errors)
            except BaseException as error:errors.append(repr(error))
        pkg.require(not errors,'targeted cleanup: '+repr(errors))

    def execute(self):
        result=dict(status='FAILED',attempts=0,complete_models=0,generation_attempts=0,
                    real_tools=0,long_run=False)
        pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        def interrupted(signum,_):raise RuntimeError('ENV_ABORT: recheck interrupted '+str(signum))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGINT,signal.SIGTERM)}
        try:
            s.base.client.a.require_idle(OUT/'initial-gpu.json')
            self.log=(OUT/'controller.log').open('xb')
            signal.setitimer(signal.ITIMER_REAL,self.plan['gpu_timer_seconds'])
            self.process=subprocess.Popen([sys.executable,'-I','-B',
                str(ROOT/'tools/serve_service/controller_exec.py'),'--plan',str(s.PLAN),
                '--output',str(self.directory),'--parent',str(os.getpid())],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,
                start_new_session=True)
            result['attempts']=1
            self.owner=s.sup.process_identity(self.process.pid)
            pkg.save(OUT/'controller-identity.json',self.owner)
            def observed():
                pkg.require(self.process.poll() is None,'controller exited before warming sample')
                return warming(self.directory)
            before=self.wait(observed,self.plan['observe_seconds'],'no real warming probe')
            pkg.save(OUT/'before-stop.json',before)
            target=OUT/'custody';target.mkdir(mode=0o700)
            self.custody=lease_custody.Custody(self.owner,s.service.guard.common_lock(),target,
                on_failure=lambda:os.kill(os.getpid(),signal.SIGTERM))
            self.custody.start()
            # The normal controller is its own process-group leader. No monitor
            # root change and no second controller under this lease are needed.
            pkg.save(OUT/'monitor-root.json',dict(owner=self.owner,
                actual_pgid=os.getpgid(self.process.pid),monitor_pgid=self.process.pid))
            start=time.monotonic();intent=s.service.stop(self.directory)
            self.wait(lambda:self.process.poll() is not None,self.plan['stop_seconds'],'startup stop too slow')
            seconds=time.monotonic()-start
            terminal=pkg.read(self.directory/'terminal.json')
            pkg.require(self.process.returncode==0 and terminal['state']=='STOPPED' and
                terminal['cleanup_confirmed'] and terminal['guard']['status']=='COMPLETED' and
                not terminal['guard']['external_lease'] and terminal['worker']['attempts']==1 and
                terminal['worker']['restarts']==0,'normal production startup stop')
            events=(self.directory/'events.log').read_text().splitlines()
            pkg.require(not any(r.endswith((' READY',' DEGRADED')) for r in events),'late healthy publication')
            pkg.save(OUT/'stop-result.json',dict(passed=True,seconds=seconds,intent=intent,
                terminal_sha256=pkg.sha(self.directory/'terminal.json'),no_ready_publication=True))
            result['status']='PASS_STARTUP_STOP_RECHECK'
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:
                self.cleanup()
                time.sleep(10)
                observed=s.base.inspection.inspect(self.directory)
                pkg.save(OUT/'stopped-inspection.json',observed)
                pkg.require(observed['cleanup_observed'] and len(list(self.directory.glob('attempt-*.json')))==1,
                            'cleanup/reactivation observation')
                check();pkg.require(time.monotonic()-self.started<SECONDS,'recheck wall deadline')
                result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or
                    'ENV_ABORT' in str(error) else 'FAILED',cleanup_error=repr(error));raise
            finally:
                path=self.directory/'lease/exit.json'
                if path.exists() and pkg.read(path)['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):
                    result['status']='ENV_ABORT'
                for sig,handler in handlers.items():signal.signal(sig,handler)
                if self.log is not None:self.log.close()
                result['seconds']=time.monotonic()-self.started
                pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    os.umask(0o077);p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('cpu','prepare','check','run'));action=p.parse_args().action
    if action=='run':
        plan=check();OUT.mkdir(mode=0o700)
        with s.sup.processes.child_subreaper():print(json.dumps(Run(plan).execute()))
    elif action=='cpu':cpu()
    elif action=='prepare':prepare()
    else:check();print(json.dumps(dict(checked=True)))
