"""One owned lease across managed business and a startup-stop generation."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_supervisor_deadlines import cpu
from tools.serve_operations_managed import stage as base
from tools.serve_service import package as pkg,supervisor as sup,service

BASE=cpu.BASE
PLAN,OUT=BASE/'plan.json',BASE/'execution'
HERE='tools/serve_supervisor_deadlines/'
OLD=ROOT/'.q4t-work/serve-startup-listeners-20261104/package/plan.json'
ORIGINAL_CHECK=base.check
LIMITS=dict(starts=2,generation_attempts=4,controls=80,seconds=300)
GPU_SECONDS=240


def configure():
    base.BASE,base.PLAN,base.OUT,base.HERE=BASE,PLAN,OUT,HERE
    base.LIMITS,base.GPU_SECONDS=LIMITS,GPU_SECONDS
    base.check=check


def sources():
    values=base.sources()
    values['docs/SERVE_SUPERVISOR_DEADLINES_2026-11-04.md']=cpu.sha(ROOT/'docs/SERVE_SUPERVISOR_DEADLINES_2026-11-04.md')
    return values


def cpu_gate():
    value=cpu.read(BASE/'cpu-01/result.json')
    cpu.require(value['passed'] and value['tests']==114 and not value['failures'] and not value['errors'],'CPU gate')
    for name,digest in cpu.read(BASE/'cpu-01/sources.json').items():
        cpu.require(cpu.sha(ROOT/name)==digest,'CPU source changed: '+name)
    driver=cpu.read(BASE/'driver-cpu/result.json')
    cpu.require(driver['passed'],'driver gate')
    for name,digest in driver['sources'].items():cpu.require(cpu.sha(ROOT/name)==digest,'driver changed: '+name)


def prepare():
    configure();cpu_gate()
    cpu.require(not PLAN.exists() and not OUT.exists(),'preserve prior attempt')
    cpu.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed stage')
    old=cpu.read(OLD)
    receipt=cpu.read(OLD.parent.parent/'accepted-package.json')['package']
    previous=pkg.verify(receipt['package'],receipt['manifest_sha256'])
    backend=pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',old['model_dir'],
        BASE/'backend-package',port=base.PORT,operations_port=base.OPS_PORT,
        client_policy=old['policy'],restart_limit=0)
    current=pkg.verify(backend['package'],backend['manifest_sha256'])
    changed={n for n,d in current['files'].items() if previous['files'].get(n)!=d}
    cpu.require(changed=={'source/tools/serve_service/supervisor.py','source/tools/serve_ops/transport.py'},'unexpected package delta')
    private=BASE/'private';private.mkdir(mode=0o700)
    front=cpu.read(old['frontend']);pkg.save(private/'gateway.json',front)
    tls=Path(old['gateway']['package'])
    nginx=cpu.read(base.client.VENDOR)['binary']
    gateways,rejected=[],[]
    for label,value in (('good-1',front),('good-2',front),
        ('missing-operations',{k:v for k,v in front.items() if k!='upstream_operations_port'}),
        ('wrong-operations',dict(front,upstream_operations_port=18134))):
        path=private/(label+'.json');pkg.save(path,value)
        row=base.gateway.prepare(path,nginx,tls/'certificate.pem',tls/'private-key.pem',
            BASE/('gateway-'+label),old['policy'],trusted_ca=tls/'certificate.pem')
        (gateways if label.startswith('good') else rejected).append(row)
    outer,_=base.gateway.verify(gateways[0]['package'],gateways[0]['manifest_sha256'])
    preserved=dict(old['preserved'])
    for path in (ROOT/'build/q4t',ROOT/'build/q4t.release.json',OLD,
                 OLD.parent.parent/'accepted-package.json',Path(old['policy']),*base.DAYS):
        preserved[str(path)]=cpu.sha(path)
    release=cpu.read(ROOT/'build/q4t.release.json')
    plan=dict(package=backend,previous_package=receipt,gateways=gateways,rejected_gateways=rejected,
        gateway=outer['gateway'],frontend=str(private/'gateway.json'),policy=old['policy'],
        protocol=release['chat_protocol']['protocol_sha256'],limits=LIMITS,gpu_seconds=GPU_SECONDS,
        sources=sources(),preserved=preserved,runtime=cpu.read(release['tested_source_manifest']),
        model_dir=old['model_dir'],model_inputs=old['model_inputs'],cpu_sha256=cpu.sha(BASE/'cpu-01/result.json'),
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        planned=dict(backend_attempts=2,complete_models=1,startup_stops=1,gateway_starts=2,
            rejected_gateways=2,responses=4,real_tools=2,public_slow_headers=136,transport_requests=3),
        supervisor_http_upper_bound=4*(GPU_SECONDS//5+6),
        startup_stop_seconds=5,startup_observe_seconds=20,
        long_run=False,actual_lan=False,persistent_installation=False,performance_acceptance=False)
    pkg.save(PLAN,plan);check()
    return dict(prepared=True,package=backend,planned=plan['planned'])


def check():
    plan=ORIGINAL_CHECK()
    cpu_gate()
    pkg.verify(plan['previous_package']['package'],plan['previous_package']['manifest_sha256'])
    current=pkg.read(Path(plan['package']['package'])/'manifest.json')
    previous=pkg.read(Path(plan['previous_package']['package'])/'manifest.json')
    cpu.require({n for n,d in current['files'].items() if previous['files'].get(n)!=d}==
        {'source/tools/serve_service/supervisor.py','source/tools/serve_ops/transport.py'},'package source drift')
    cpu.require(plan['planned']['backend_attempts']==2 and plan['planned']['transport_requests']==3 and
                plan['startup_stop_seconds']==5 and plan['startup_observe_seconds']==20,'frozen limits')
    return plan


def established_probe(worker_pid,operations_port):
    """A real supervisor-owned TCP connection to its warming operations port."""
    inodes=set()
    for entry in Path(f'/proc/{worker_pid}/fd').iterdir():
        try:target=os.readlink(entry)
        except FileNotFoundError:continue
        if target.startswith('socket:['):inodes.add(target[8:-1])
    matches=[]
    for line in Path(f'/proc/{worker_pid}/net/tcp').read_text().splitlines()[1:]:
        fields=line.split()
        if fields[3]=='01' and fields[2]==f'0100007F:{operations_port:04X}' and fields[9] in inodes:
            matches.append(dict(local=fields[1],remote=fields[2],state=fields[3],inode=fields[9]))
    return matches


def startup_snapshot(state,log,peers):
    return (state['attempts']==1 and state['restarts']==0 and
            state['state'] in ('STARTING','OBSERVATION_UNAVAILABLE') and
            '[q4t] serving on port' not in log and bool(peers))


class Run(base.Run):
    def launch(self):
        super().launch()
        self.phase_owner=sup.process_identity(os.getpid())
        custody=self.custody
        def scan():
            cpu.require(sup.process_identity(os.getpid())==self.phase_owner,'validation parent identity changed')
            return service.guard.inspect(os.getpid(),custody.history,
                compute_query=lambda:service.guard.nvml_pids(custody.output/'nvml.jsonl'))
        # Keep the exact duplicated flock, broadening only the validation
        # monitor root to this owning driver before a second child is possible.
        with custody.mutex:custody.scan=scan
        pkg.save(OUT/'phase-custody-root.json',dict(owner=self.phase_owner,
            first_controller=custody.owner,reason='sequential owned generations, same open file description'))
        custody.sample()

    def helper(self,name,argv,*,lease=False):
        log=(OUT/(name+'.log')).open('xb');self.logs.append(log)
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and
             k not in ('LD_PRELOAD','LD_LIBRARY_PATH','PYTHONPATH','PYTHONHOME')}
        process=subprocess.Popen(list(map(str,argv)),cwd=ROOT,stdin=subprocess.DEVNULL,
            stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env,
            pass_fds=(self.custody.fd,) if lease else ())
        self.processes[name]=process
        pkg.save(OUT/(name+'-identity.json'),sup.process_identity(process.pid))
        return process

    def transport_probes(self):
        for _ in range(3):self.budget('controls')
        process=self.helper('transport-probes',[sys.executable,'-I','-B',ROOT/HERE/'transport_probe.py',
            '--package',self.package,'--digest',self.plan['package']['manifest_sha256'],
            '--output',OUT/'transport','--parent',os.getpid()])
        cpu.require(process.wait(10)==0,'real transport probes failed')
        result=cpu.read(OUT/'transport/result.json')
        cpu.require(result['passed'] and result['requests']==3,'transport coverage')

    def startup_stop(self):
        self.budget('starts')
        directory=OUT/'startup-run'
        process=self.helper('startup-controller',[sys.executable,'-I','-B',ROOT/HERE/'borrowed_controller.py',
            '--package',self.package,'--digest',self.plan['package']['manifest_sha256'],
            '--output',directory,'--parent',os.getpid(),'--lease-fd',self.custody.fd],lease=True)
        def warming():
            cpu.require(process.poll() is None,'startup controller exited before stop')
            path=directory/'status.json'
            if not path.exists():return None
            state=pkg.read(path)
            cpu.require(state['state'] not in ('FAILED','STOPPED','READY','DEGRADED'),'not an unready startup')
            if not state.get('backend'):return None
            log=(directory/'backend.log').read_text() if (directory/'backend.log').exists() else ''
            peers=established_probe(state['owner']['pid'],base.OPS_PORT)
            if not startup_snapshot(state,log,peers):return None
            primary=sup.identity.process(state['backend']['pid'],base.PORT)
            operations=sup.identity.operations(primary,base.OPS_PORT)
            return dict(state=state,peers=peers,primary=primary,operations=operations,
                monotonic=time.monotonic(),backend_log_sha256=pkg.sha(directory/'backend.log'))
        before=self.wait(warming,self.plan['startup_observe_seconds'],'no warming supervisor TCP probe')
        pkg.save(OUT/'startup-before-stop.json',before)
        start=time.monotonic();intent=service.stop(directory)
        self.wait(lambda:process.poll() is not None,self.plan['startup_stop_seconds'],'startup stop too slow')
        terminal=pkg.read(directory/'terminal.json')
        elapsed=time.monotonic()-start
        cpu.require(process.returncode==0 and terminal['state']=='STOPPED' and terminal['cleanup_confirmed'] and
            terminal['guard']['status']=='COMPLETED' and terminal['guard']['external_lease'] and
            terminal['worker']['attempts']==1 and terminal['worker']['restarts']==0,'startup stop terminal')
        events=(directory/'events.log').read_text().splitlines()
        cpu.require(not any(row.endswith((' READY',' DEGRADED')) for row in events),'startup became ready')
        cpu.require(not any(base.inspection.listeners(p) for p in (base.PORT,base.OPS_PORT)),'startup listener retained')
        base.client.a.require_idle(OUT/'after-startup-stop-gpu.json')
        pkg.save(OUT/'startup-stop-result.json',dict(passed=True,seconds=elapsed,intent=intent,
            terminal_sha256=pkg.sha(directory/'terminal.json'),no_ready_publication=True,
            same_lease=True,controller_entry='validation bridge to sealed service.run with existing borrowed guard API'))

    def stop_helpers(self):
        errors=[]
        for name in ('startup-controller','transport-probes'):
            process=self.processes.get(name)
            if process is None or process.poll() is not None:continue
            history={}
            try:
                service.guard.owned_processes(service.guard.processes(),process.pid,history)
                directory=OUT/'startup-run'
                if name=='startup-controller' and (directory/'controller.json').exists():
                    service.stop(directory)
                else:process.terminate()
                process.wait(35 if name=='startup-controller' else 3)
            except BaseException as error:
                errors.append(repr(error))
                try:service.guard.stop_owned(process,2,history)
                except BaseException as failure:errors.append(repr(failure))
        cpu.require(not errors,'helper cleanup failure: '+repr(errors))

    def cleanup(self):
        try:self.stop_helpers()
        finally:super().cleanup()

    def execute(self):
        pkg.save(OUT/'begin.json',dict(monotonic=self.started,unix_seconds=time.time()))
        result=dict(status='FAILED',long_run=False,performance_acceptance=False)
        def interrupted(signum,_):raise RuntimeError('ENV_ABORT: driver interrupted: '+str(signum))
        handlers={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        try:
            base.client.a.require_idle(OUT/'initial-gpu.json')
            self.launch();self.rejected_frontends();self.frontend(1);self.pressure()
            observed=json.loads((base.client.OUT/'saturated-capabilities-wire.bin').read_bytes())
            cpu.require(pkg.same_json(observed,pkg.read(self.package/'declarations/capabilities.json')),'declaration mismatch')
            self.current_client=0;self.tools('project-alpha');self.idle('alpha-idle')
            self.stop_front(1);self.frontend(2)
            self.current_client=1;self.tools('project-beta')
            metrics=self.idle('final-idle');pkg.save(OUT/'final-metrics.json',metrics)
            cpu.require(self.successes==metrics['q4t_requests_success_total']==4 and
                metrics['q4t_requests_error_total']==metrics['q4t_requests_aborted_total']==0 and
                metrics['q4t_structured_workers_started_total']==metrics['q4t_structured_workers_reaped_total']==4,'business accounting')
            self.stop_front(2);self.stop_backend()
            self.custody.check();base.client.a.require_idle(OUT/'between-generations-gpu.json')
            self.transport_probes();self.startup_stop()
            result.update(status='PASS_SUPERVISOR_DEADLINES_SHORT',successful_responses=4,real_tools=2,
                gateway_starts=2,rejected_gateways=2,backend_attempts=2,complete_models=1,
                startup_stops=1,transport_requests=3)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',error=repr(error));raise
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:
                self.cleanup()
                if self.processes:
                    time.sleep(10)
                    for name in ('managed-run','startup-run'):
                        if not (OUT/name/'controller.json').exists():continue
                        value=base.inspection.inspect(OUT/name)
                        pkg.save(OUT/(name+'-stopped-inspection.json'),value)
                        cpu.require(value['cleanup_observed'] and len(list((OUT/name).glob('attempt-*.json')))==1,'post-stop cleanup/reactivation')
                cpu.require(time.monotonic()-self.started<LIMITS['seconds'],'wall budget')
                check();result['cleanup_confirmed']=True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status']=='ENV_ABORT' or
                    'ENV_ABORT' in str(error) else 'FAILED',cleanup_error=repr(error));raise
            finally:
                for name in ('managed-run','startup-run'):
                    path=OUT/name/'lease/exit.json'
                    if path.exists() and pkg.read(path)['status'] in ('ENV_ABORT','LEASE_BUSY','RECOVERY_REQUIRED'):
                        result['status']='ENV_ABORT'
                for sig,handler in handlers.items():signal.signal(sig,handler)
                for stream in self.logs:stream.close()
                result.update(counts=self.counts,seconds=time.monotonic()-self.started)
                pkg.save(OUT/'result.json',result)
        return result


if __name__=='__main__':
    os.umask(0o077);configure();p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','check','run'));action=p.parse_args().action
    if action=='run':
        plan=check();OUT.mkdir(mode=0o700)
        with sup.processes.child_subreaper():value=Run(plan).execute()
    elif action=='check':check();value=dict(checked=True)
    else:value=prepare()
    print(json.dumps(value))
