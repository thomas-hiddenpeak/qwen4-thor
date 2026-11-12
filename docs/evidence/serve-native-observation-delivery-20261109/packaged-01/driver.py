"""Actual sealed CLI and exporter; only synthetic CPU run records are retired."""
import fcntl,json,os,signal,socket,subprocess,sys,time,traceback,uuid
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,journal,ledger,service
from tools.serve_service.package import read,save,sha,require
BASE=ROOT/'.q4t-work/serve-native-observation-20261109';RELEASE=BASE/'release-02';OUT=BASE/'packaged-01'
STATE=OUT/'state';PKG=RELEASE/'bundle';CLI=PKG/'bin/q4t'

def call(name,operation,*args,wanted=0):
 cmd=[str(CLI),'service',operation,'--state',str(STATE),*map(str,args)]
 p=subprocess.run(cmd,capture_output=True,timeout=15)
 (OUT/(name+'.stdout')).write_bytes(p.stdout);(OUT/(name+'.stderr')).write_bytes(p.stderr)
 save(OUT/(name+'.json'),dict(argv=cmd,exit_code=p.returncode))
 require(p.returncode==wanted,'CLI exit '+name)
 return json.loads(p.stdout) if operation not in ('metrics',) else p.stdout.decode()

def get(port,path='/status',method='GET'):
 with socket.create_connection(('127.0.0.1',port),timeout=1) as s:
  s.sendall(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode());raw=b''
  while chunk:=s.recv(65536):
   raw+=chunk;require(len(raw)<256*1024,'HTTP bound')
 head,_,body=raw.partition(b'\r\n\r\n');require(head.startswith(b'HTTP/1.1 200'),'HTTP status')
 return raw,body

def main():
 OUT.mkdir(mode=0o700)
 review=read(RELEASE/'control-review.json');default=review['default']
 require(all(sha(ROOT/'build'/n)==h for n,h in default.items()),'default drift')
 receipt=read(RELEASE/'receipt.json');bundle.verify(PKG,receipt['manifest_sha256'])
 initialized=call('init','init');require(initialized['desired']=='STOPPED','init starts')
 # This never-started synthetic state has no Guardian-created lock yet.
 (STATE/'guardian.lock').touch(mode=0o600)
 original_ledger=sha(STATE/'ledger.json')
 observed=call('observe','observe');require(observed['state']=='STOPPED' and observed['business_ready'] is False,'stopped projection')
 metrics=call('metrics','metrics');require('q4t_native_business_ready 0' in metrics,'stopped metrics')
 empty=call('plan-empty','retention-plan');require(not empty['remove'] and not empty['protected'],'empty retention')
 with socket.socket() as chooser:
  chooser.bind(('127.0.0.1',0));port=chooser.getsockname()[1]
 cmd=[str(CLI),'service','export','--state',str(STATE),'--port',str(port)]
 children=set();exporter=None
 with (OUT/'export.stdout').open('wb') as stdout,(OUT/'export.stderr').open('wb') as stderr:
  try:
   exporter=subprocess.Popen(cmd,stdout=stdout,stderr=stderr)
   until=time.monotonic()+10
   while True:
    require(exporter.poll() is None,'exporter exited')
    try:
     raw,body=get(port);value=json.loads(body)
     if value['state']=='STOPPED':break
    except (ConnectionError,OSError,ValueError):pass
    require(time.monotonic()<until,'exporter readiness deadline');time.sleep(.1)
   (OUT/'export-status.http').write_bytes(raw)
   for task in Path(f'/proc/{exporter.pid}/task').iterdir():
    children.update(int(n) for n in (task/'children').read_text().split())
   require(len(children)==1,'exactly one packaged collector')
   worker=next(iter(children));argv=(Path('/proc')/str(worker)/'cmdline').read_bytes().split(b'\0')
   require(os.fsencode(PKG/'source/tools/serve_host/native_exporter.py') in argv,'collector source binding')
   raw,body=get(port,'/metrics');require(b'q4t_native_business_ready 0' in body,'export metric')
   (OUT/'export-metrics.http').write_bytes(raw)
   raw,body=get(port,'/status','HEAD');require(not body,'HEAD body');(OUT/'export-head.http').write_bytes(raw)
  finally:
   if exporter is not None and exporter.poll() is None:exporter.terminate()
   if exporter is not None:code=exporter.wait(timeout=5)
 require(code==0 and all(not Path('/proc',str(pid)).exists() for pid in children),'export cleanup')
 with socket.socket() as s:require(s.connect_ex(('127.0.0.1',port))!=0,'export listener leaked')
 fd=os.open(STATE/'native-observer.lock',os.O_RDWR)
 try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 finally:os.close(fd)
 save(OUT/'export.json',dict(argv=cmd,exit_code=code,collector_count=len(children),collector_source_verified=True,children_reaped=True,listener_closed=True,claim_released=True))
 run_id=str(uuid.uuid4());run=STATE/'runs'/run_id;run.mkdir(mode=0o700)
 config=service.controller_configuration(PKG,receipt['manifest_sha256'],STATE,run)
 config['version']=2;config.pop('retention_context')
 save(run/'controller.json',config)
 save(run/'guardian.json',dict(directory=str(STATE),service_id=initialized['service_id'],launch=dict(parent_pid=os.getpid(),parent_start_ticks=journal.process_start(os.getpid())),controller=['python','--config',str(run/'controller.json')],gateway=['python','--config',str(run/'gateway.json')]))
 save(run/'gateway.json',dict(cpu_fixture=True));(run/'model-1').mkdir(mode=0o700)
 (run/'model-1/model.log').write_bytes(b'synthetic CPU-only fault evidence\n');(run/'model-1/model.log').chmod(0o600)
 migration=call('migrate','retention-migrate','--run',run_id)
 require(migration['migrated'] and not migration['model_signalled'] and not migration['gpu_probe_started'],'migration')
 archived=call('archive','retention-archive','--run',run_id,'--archive',OUT/'archive')
 require((Path(archived['archive'])/'files/model-1/model.log').read_bytes()==(run/'model-1/model.log').read_bytes(),'archive bytes')
 planned=call('plan','retention-plan','--max-bytes',1,'--plan-output',OUT/'plan.private.json')
 require([r['run_id'] for r in planned['remove']]==[run_id],'fixture eligible')
 applied=call('apply','retention-apply','--plan',OUT/'plan.private.json')
 require(applied['applied'] and not applied['model_signalled'] and not run.exists(),'fixture retired')
 resumed=call('resume','retention-resume','--run',run_id);require(resumed['state']=='RETIRED' and resumed['resumed'],'resume idempotence')
 require(sha(STATE/'ledger.json')==original_ledger,'fixture ledger changed')
 require(all(sha(ROOT/'build'/n)==h for n,h in default.items()),'default changed')
 return dict(status='PASS_ACTUAL_V6_PACKAGED_CLI_EXPORT_RETENTION',manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=default['q4t'],actual_packaged_child_source=True,read_only_export=True,synthetic_legacy_run_retired=1,production_runs_removed=0,ledger_preserved=True,default_preserved=True,model_starts=0,chat_requests=0,gpu_probes=0,ownership_lock_proof='nonblocking shared lease and own fixture Guardian/writer; no device operation',remaining_master_tasks_open=True)

if __name__=='__main__':
 start=time.monotonic()
 try:
  result=main()
 except Exception:
  if OUT.exists():(OUT/'failure.txt').write_text(traceback.format_exc())
  raise
 result['seconds']=time.monotonic()-start;save(OUT/'result.json',result);print(json.dumps(result))
