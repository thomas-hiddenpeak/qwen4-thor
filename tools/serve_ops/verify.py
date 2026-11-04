"""Bounded real diagnostics, project tools and temporary Prometheus acceptance."""
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time
from urllib.parse import urlencode

from tools.serve_ops import bundle, identity, stage
from tools.serve_admission.verify import Run as BaseRun, ordinary

h,ROOT=stage.h,stage.ROOT


def check_diagnostics(before,after,down,raw_bundles,canaries):
    h.require(before['state']==after['state']=='HEALTHY','healthy diagnostic state')
    h.require(before['identity']['state']==after['identity']['state']=='verified' and
              before['identity']['process']==after['identity']['process'],'same actual instance')
    delta=after['comparison']
    h.require(delta['state']=='available','counter comparison unavailable')
    counts=delta['counters']
    h.require(counts['q4t_requests_total']==counts['q4t_requests_success_total']==3 and
        counts['q4t_requests_error_total']==counts['q4t_requests_aborted_total']==0 and
        counts['q4t_structured_workers_started_total']==counts['q4t_structured_workers_reaped_total']==2,
        'real business counter accounting')
    h.require(down['state']=='UNKNOWN' and down['metrics'] is None and
              down['comparison']['state']=='unavailable','stopped service incorrectly healthy/zero')
    h.require(all(c.encode() not in raw_bundles for c in canaries),'diagnostic private content escaped')
    h.require(after['log']['state']=='available' and after['log']['events'] and
              not after['log']['raw_log_included'],'no projected log evidence')


class Run(BaseRun):
    def __init__(self,plan,output):
        super().__init__(plan,output)
        self.deadline=plan['started']+stage.LIMITS['seconds']-60
        self.prom=self.prom_fd=None
        self.diag_root=output/'diagnostics'
        self.diag_root.mkdir(mode=0o700)
        self.prom_root=output/'prometheus'
        self.prom_root.mkdir(mode=0o700)

    def budget(self,kind=None):
        h.require(time.time()<self.deadline,'frozen operations deadline')
        if kind:
            h.require(self.counts[kind]<stage.LIMITS[kind],'frozen attempts limit')
            self.counts[kind]+=1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind,counts=self.counts.copy(),time=time.time()))

    def collect(self,label,previous=None):
        self.budget()
        argv=['/usr/bin/python3','-B',str(ROOT/'tools/serve_ops/diagnose.py'),
            '--base-url',self.base,'--pid',str(self.server.pid),
            '--release',self.plan['sidecar']['path'],'--log',str(self.folder/'server.log'),
            '--output',str(self.diag_root/label)]
        if previous:argv+=['--previous',str(self.diag_root/previous)]
        with (self.out/(label+'-diagnose.stdout')).open('xb') as stdout, \
             (self.out/(label+'-diagnose.stderr')).open('xb') as stderr:
            process=subprocess.run(argv,cwd=ROOT,stdout=stdout,stderr=stderr,timeout=25)
        h.save(self.out/(label+'-diagnose-command.json'),dict(argv=argv,returncode=process.returncode))
        h.require(process.returncode==(3 if label=='down' else 0),'diagnostic CLI result')
        value=bundle.read(self.diag_root/label)
        h.save(self.out/(label+'-permissions.json'),dict(
            directory=oct((self.diag_root/label).stat().st_mode & 0o777),
            files={p.name:oct(p.stat().st_mode & 0o777) for p in (self.diag_root/label).iterdir()}))
        return value

    def start_model(self):
        self.budget('starts')
        self.folder=self.out/'model';self.folder.mkdir(mode=0o700)
        argv=[self.plan['binary']['path'],'serve','--model-dir',self.plan['model_dir'],
            '--host','127.0.0.1','--port',str(self.port),'--max-prefill','2048',
            '--max-len','8192','--max-seq','1','--max-inflight-requests','8',
            '--max-queued-requests','2']
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
        h.save(self.folder/'command.json',dict(argv=argv,binary=self.plan['binary'],validation_env={}))
        with (self.folder/'server.log').open('x') as log:
            self.server=subprocess.Popen(argv,cwd=ROOT,env=env,stdout=log,stderr=log)
            self.server_fd=os.pidfd_open(self.server.pid)
        end=time.monotonic()+240
        while 'serving on port' not in (self.folder/'server.log').read_text():
            self.budget()
            h.require(self.server.poll() is None and time.monotonic()<end,'model startup failed')
            time.sleep(.25)
        reply=self.http('GET','/v1/config');h.save(self.folder/'configuration.json',reply)
        value=json.loads(reply['raw']);self.config_schema.validate(value)
        h.require(reply['status']==200 and value['protocol_source_sha256']==self.plan['protocol_sha256']
            and value['runtime']['mtp_enabled'] and
            value['memory_admission']['min_available_mib']['effective']==2048,'default runtime identity')
        self.drain('before')

    def start_prometheus(self):
        config={'global':dict(scrape_interval='15s',scrape_timeout='2s',evaluation_interval='15s'),
            'rule_files':[str(ROOT/'deploy/serve-ops/q4t.rules.yml')],
            'scrape_configs':[dict(job_name='q4t',static_configs=[dict(targets=[f'127.0.0.1:{self.port}'])])],
            'alerting':dict(alertmanagers=[])}
        h.save(self.prom_root/'config.json',config)
        argv=[self.plan['prometheus']['path'],'--config.file='+str(self.prom_root/'config.json'),
            '--storage.tsdb.path='+str(self.prom_root/'tsdb'),
            '--web.listen-address=127.0.0.1:'+str(self.plan['prometheus_port']),
            '--storage.tsdb.retention.time=1h','--storage.tsdb.retention.size=64MB']
        h.save(self.prom_root/'command.json',dict(argv=argv,notification_receivers=[]))
        with (self.prom_root/'server.log').open('x') as log:
            self.prom=subprocess.Popen(argv,cwd=ROOT,stdout=log,stderr=log)
            self.prom_fd=os.pidfd_open(self.prom.pid)
        end=time.monotonic()+30
        while 'Server is ready to receive web requests.' not in (self.prom_root/'server.log').read_text():
            self.budget()
            h.require(self.prom.poll() is None and time.monotonic()<end,'Prometheus startup failed')
            time.sleep(.1)
        owned=set()
        for descriptor in Path(f'/proc/{self.prom.pid}/fd').iterdir():
            try:target=os.readlink(descriptor)
            except FileNotFoundError:continue
            if target.startswith('socket:['):owned.add(target[8:-1])
        selected=identity.listener(Path('/proc/net/tcp').read_text(),self.plan['prometheus_port'],owned)
        h.require(selected['listener_scope']=='loopback','Prometheus listener is not isolated')
        h.save(self.prom_root/'listener.json',dict(pid=self.prom.pid,**selected))

    def prom_get(self,path):
        self.budget();h.require(self.prom.poll() is None,'owned Prometheus exited')
        conn=http.client.HTTPConnection('127.0.0.1',self.plan['prometheus_port'],timeout=3)
        try:
            conn.request('GET',path)
            response=conn.getresponse();raw=response.read(4*1024**2+1)
            h.require(response.status==200 and len(raw)<=4*1024**2,'Prometheus API unavailable')
            value=json.loads(raw)
            h.require(value['status']=='success','Prometheus query failed')
            return value
        finally:conn.close()

    def monitor_state(self,*,down=False):
        end=time.monotonic()+(105 if down else 45);samples=[]
        try:
            while True:
                up=self.prom_get('/api/v1/query?'+urlencode(dict(query='up{job="q4t"}')))
                value=self.prom_get('/api/v1/rules')
                groups=value['data']['groups'];rules=[r for g in groups for r in g['rules']]
                firing=[r['name'] for r in rules if r.get('state')=='firing']
                samples.append(dict(unix_seconds=time.time(),up=up,rules=value))
                observed=up['data']['result']
                ready=(len(observed)==1 and observed[0]['metric']['instance']==f'127.0.0.1:{self.port}' and
                    observed[0]['value'][1]==('0' if down else '1') and len(rules)==19 and
                    all(r['health']=='ok' for r in rules))
                if ready and (firing==['Q4TTargetDown'] if down else not firing):
                    return dict(passed=True,firing=firing,samples=len(samples))
                h.require(time.monotonic()<end,'actual Prometheus state deadline')
                time.sleep(3)
        finally:h.save(self.prom_root/('down-observations.json' if down else 'healthy-observations.json'),samples)

    def stop_model(self):
        if self.server is None or self.server_fd is None:return
        try:
            if self.server.poll() is None:
                signal.pidfd_send_signal(self.server_fd,signal.SIGTERM)
                try:self.server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    signal.pidfd_send_signal(self.server_fd,signal.SIGKILL);self.server.wait(timeout=10)
            for call in self.calls:call.thread.join(timeout=5)
            clean=self.server.returncode==0 and 'shutdown complete (0 in-flight remaining)' in (self.folder/'server.log').read_text()
            h.save(self.folder/'shutdown.json',dict(clean=clean,returncode=self.server.returncode,
                clients_joined=all(not c.thread.is_alive() for c in self.calls)))
            h.require(clean and all(not c.thread.is_alive() for c in self.calls),'owned model shutdown')
        finally:os.close(self.server_fd);self.server_fd=None

    def stop_prometheus(self):
        if self.prom is None or self.prom_fd is None:return
        try:
            if self.prom.poll() is None:
                signal.pidfd_send_signal(self.prom_fd,signal.SIGTERM)
                try:self.prom.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    signal.pidfd_send_signal(self.prom_fd,signal.SIGKILL);self.prom.wait(timeout=5)
            h.save(self.prom_root/'shutdown.json',dict(returncode=self.prom.returncode,owned_only=True))
            h.require(self.prom.returncode==0,'owned Prometheus shutdown')
        finally:os.close(self.prom_fd);self.prom_fd=None

    def run(self):
        result=dict(status='FAILED')
        try:
            self.start_model();self.start_prometheus()
            healthy=self.monitor_state()
            before=self.collect('before')
            body=ordinary();canary='SRV12_PRIVATE_'+secrets.token_hex(12)
            body['request_id']=canary+'_ID'
            body['messages'][0]['content']+=' 不要复述此诊断哨兵：'+canary+'_BODY'
            self.normal(self.submit('ordinary',body))
            self.project_tool();self.drain('after-business')
            after=self.collect('after','before')
            h.require(body['request_id'] in (self.folder/'server.log').read_text(),'canary absent from raw log')
            self.stop_model()
            down=self.collect('down','after')
            alert=self.monitor_state(down=True)
            raw=b''.join(p.read_bytes() for p in self.diag_root.glob('*/*.json'))
            canaries=[body['request_id'],canary+'_BODY',self.plan['model_dir']]
            canaries += [call.body['cancel_token'] for call in self.calls]
            check_diagnostics(before,after,down,raw,canaries)
            h.require(self.counts==stage.PLANNED,'frozen real case count')
            h.save(self.out/'wire-schema-result.json',dict(passed=True,records=self.wires))
            h.save(self.out/'diagnostic-result.json',dict(passed=True,states=[before['state'],after['state'],down['state']],
                canary_present_in_raw_log=True,canaries_absent_from_all_bundles=True,
                same_instance_counter_delta=after['comparison'],bundle_count=3,
                raw_logs_are_not_redacted=True))
            result.update(status='PASS_BOUNDED_LOCAL_OPERATIONS',healthy_scrape=healthy,
                stopped_target_alert=alert,notification_receivers_deployed=False,
                deployment_changed=False,automatic_retries=0,performance_acceptance=False)
        except BaseException as error:
            result['error']=repr(error)
            if isinstance(error,stage.a.EnvironmentAbort):result['status']='ENV_ABORT'
            raise
        finally:
            try:
                try:self.stop_model()
                finally:self.stop_prometheus()
                stage.a.require_idle_gpu(self.out/'gpu-after.json')
            except BaseException as error:
                result.update(status='ENV_ABORT' if isinstance(error,stage.a.EnvironmentAbort) else 'FAILED',
                              cleanup_error=repr(error))
                raise
            finally:
                result.update(counts=self.counts,elapsed_seconds=time.monotonic()-self.started)
                h.save(self.out/'result.json',result)
