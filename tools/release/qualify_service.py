"""One release lifecycle/resource check followed by a fixed 30-minute trial."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2]
MANAGER=ROOT/'tools/release/release.py'


def save(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--package',type=Path,required=True)
    ap.add_argument('--previous',type=Path,required=True)
    ap.add_argument('--nginx',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--quality-run',type=Path,required=True)
    ap.add_argument('--performance-run',type=Path,required=True)
    args=ap.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    runtime=out/'runtime';records=[];failure=None
    backend,data,control=18080,18081,18082
    def run(action,package=None):
        cmd=['python3',str(MANAGER),action,'--root',str(runtime),'--nginx',str(args.nginx)]
        if package:cmd+=['--package',str(package.resolve())]
        with (out/'manager.log').open('ab') as log:
            subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
    def http(port,path,payload=None,timeout=300):
        c=HTTPConnection('127.0.0.1',port,timeout=timeout)
        start=time.monotonic()
        try:
            c.request('GET' if payload is None else 'POST',path,None if payload is None else json.dumps(payload),{'Content-Type':'application/json'})
            r=c.getresponse();body=r.read().decode()
            return {'status':r.status,'body':body,'seconds':time.monotonic()-start}
        finally:c.close()
    def metrics():
        r=http(backend,'/metrics',timeout=2);assert r['status']==200
        return {l.split()[0]:float(l.split()[1]) for l in r['body'].splitlines() if l and not l.startswith('#') and len(l.split())==2}
    def wait(fn,seconds=15):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if fn():return
            time.sleep(.05)
        raise AssertionError('condition timeout')
    def held(port,length):
        s=socket.create_connection(('127.0.0.1',port),timeout=2)
        s.sendall(f'POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\nContent-Length: {length}\r\n\r\n'.encode())
        return s
    quality=json.loads((args.quality_run/'results.json').read_text())
    lines=[json.loads(s) for s in (args.quality_run/'inputs/requests.jsonl').read_text().splitlines()]
    fixtures={}
    for length in [1024,4096,8192,45056,204800]:
        row=next(r for r in quality if r['actual_input']==length)
        source=next(r for r in lines if hashlib.sha256(r['prompt'].encode()).hexdigest()==row['prompt_sha256'])
        fixtures[length]=(source['prompt'],row)
    def generate(length,tag):
        prompt,ref=fixtures[length]
        req={'prompt':prompt,'stream':True,'stream_options':{'include_usage':True},'max_tokens':32,'temperature':0}
        c=HTTPConnection('127.0.0.1',data,timeout=300)
        events=[];first=None;start=time.monotonic()
        try:
            c.request('POST','/v1/chat/completions',json.dumps(req),{'Content-Type':'application/json'})
            r=c.getresponse();assert r.status==200,r.status
            done=False
            while line:=r.readline():
                if not line.startswith(b'data: '):continue
                payload=line[6:].strip()
                if payload==b'[DONE]':done=True;break
                event=json.loads(payload);events.append(event)
                if first is None and any(x.get('delta',{}).get('content') for x in event.get('choices',[])):first=time.monotonic()
            result={'tag':tag,'length':length,'events':events,'seconds':time.monotonic()-start,'ttft':None if first is None else first-start,'done':done}
            save(out/(tag+'.json'),result)
            choices=[x for e in events for x in e.get('choices',[])]
            assert done and not any('error' in e for e in events)
            assert ''.join(x.get('delta',{}).get('content','') for x in choices)==ref['text']
            assert [x['finish_reason'] for x in choices if x.get('finish_reason')]==['stop']
            usage=next(e['usage'] for e in events if e.get('usage'))
            assert usage['prompt_tokens']==length and usage['completion_tokens']==ref['actual_output']
            assert result['ttft'] is not None and result['ttft']< {1024:2,4096:5,8192:10,45056:50,204800:240}[length]
            return result
        finally:c.close()
    save(out/'plan.json',{'package':str(args.package.resolve()),'previous':str(args.previous.resolve()),'seconds':1800,'minimum_requests':60,'rss_growth_limit_bytes':512*1024*1024,'ports':[backend,data,control],'nginx_sha256':hashlib.sha256(args.nginx.read_bytes()).hexdigest(),'fixtures':{str(n):{'prompt_sha256':r['prompt_sha256'],'expected':r['text']} for n,(_,r) in fixtures.items()}})
    sockets=[]
    try:
        # Exercise a real old -> new -> old -> new switch. Never call this
        # rollback of quality evidence: each snapshot retains its own identity.
        run('activate',args.previous);generate(1024,'previous-smoke')
        bad=out/'invalid-package';shutil.copytree(args.package,bad)
        with (bad/'q4t').open('ab') as f:f.write(b'invalid digest')
        command=['python3',str(MANAGER),'activate','--root',str(runtime),'--package',str(bad)]
        with (out/'invalid-package.log').open('w') as log:
            code=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=15).returncode
        assert code!=0 and (runtime/'current').resolve()==args.previous.resolve()
        assert http(control,'/healthz',timeout=2)['status']==200
        # A valid manifest with an intentionally failing entry point tests
        # transactional rollback of a startup failure (not a model failure).
        (bad/'q4t').write_text('#!/bin/sh\nexit 1\n');(bad/'q4t').chmod(0o755)
        manifest=json.loads((bad/'manifest.json').read_text())
        manifest['files']['q4t']=hashlib.sha256((bad/'q4t').read_bytes()).hexdigest()
        save(bad/'manifest.json',manifest)
        with (out/'startup-failure.log').open('w') as log:
            code=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=300).returncode
        assert code!=0 and (runtime/'current').resolve()==args.previous.resolve()
        assert http(control,'/healthz',timeout=2)['status']==200
        records.append({'case':'invalid-package-and-startup-failure-rollback','passed':True})
        run('activate',args.package);generate(1024,'upgrade-smoke')
        run('rollback');assert (runtime/'current').resolve()==args.previous.resolve()
        generate(1024,'rollback-smoke')
        run('activate',args.package);assert (runtime/'current').resolve()==args.package.resolve()
        records.append({'case':'upgrade-and-rollback','passed':True})
        for value in [1200001,0,1.5]:
            result=http(backend,'/v1/chat/completions',{'prompt':'x','request_timeout_ms':value})
            assert result['status']==400
        records.append({'case':'generation-deadline-contract','passed':True})
        # Header-only reservations exercise the aggregate budget before upload.
        sockets=[held(backend,16*1024*1024) for _ in range(3)]
        wait(lambda:metrics()['q4t_request_body_bytes']==48*1024*1024)
        extra=held(backend,1)
        response=extra.recv(4096);extra.close();assert b' 503 ' in response,response
        assert http(control,'/healthz',timeout=2)['status']==200
        cancel=http(control,'/v1/requests/cancel',{'request_id':'none','cancel_token':'0'*64},timeout=2)
        assert cancel['status']==404 and cancel['seconds']<2
        for s in sockets:s.close()
        sockets=[];wait(lambda:metrics()['q4t_request_body_bytes']==0)
        records.append({'case':'body-budget-reservation-control-recovery','passed':True})
        # Continuous progress must not defeat a whole-upload timeout at proxy.
        s=held(data,1024);sockets=[s];start=time.monotonic();response=b''
        while time.monotonic()-start<36:
            try:s.sendall(b'x')
            except OSError:pass
            readable,_,_=select.select([s],[],[],2)
            if readable:
                response=s.recv(4096);break
        elapsed=time.monotonic()-start;s.close();sockets=[]
        assert b' 408 ' in response and 26<=elapsed<36,(response,elapsed)
        assert not any(p.is_file() for kind in ['data','control'] for p in (runtime/kind/'body').rglob('*'))
        wait(lambda:metrics()['q4t_request_body_bytes']==0)
        records.append({'case':'proxy-trickle-absolute-deadline','seconds':elapsed,'status':408})
        sockets=[held(data,64) for _ in range(4)]
        for s in sockets:s.sendall(b'x')
        wait(lambda:metrics()['q4t_request_body_bytes']==256)
        extra=held(data,64);response=extra.recv(4096);extra.close()
        assert b' 429 ' in response,response
        h=http(control,'/healthz',timeout=2);assert h['status']==200 and h['seconds']<2
        for s in sockets:s.close()
        sockets=[];wait(lambda:metrics()['q4t_request_body_bytes']==0)
        records.append({'case':'data-admission-control-isolation','passed':True})
        perf=json.loads((args.performance_run/'inputs/context-1024.jsonl').read_text().splitlines()[0])['prompt']
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures=[];keys=[]
            for i in range(8):
                key=os.urandom(32).hex();rid='release-queue-'+str(i);keys.append((rid,key))
                payload={'prompt':perf,'max_tokens':256,'stream':False,'request_id':rid,'cancel_token':key}
                futures.append(pool.submit(http,backend,'/v1/chat/completions',payload))
            wait(lambda:metrics()['q4t_active_chats']==8)
            overload=http(backend,'/v1/chat/completions',{'prompt':'x','max_tokens':1})
            assert overload['status']==503
            for rid,key in keys:
                assert http(control,'/v1/requests/cancel',{'request_id':rid,'cancel_token':key},timeout=2)['status']==202
            results=[f.result(timeout=30) for f in futures]
            assert all(r['status']==409 for r in results),results
        wait(lambda:metrics()['q4t_active_chats']==0 and metrics()['q4t_request_body_bytes']==0)
        records.append({'case':'eight-chat-cap-cancel-recovery','passed':True})
        def group_alive(group):
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit():continue
                try:
                    if int((proc/'stat').read_text().split(') ',1)[1].split()[2])==group:return True
                except (OSError,ValueError,IndexError):pass
            return False
        # Killing a proxy master must also reap its surviving workers.
        for name,port,path in [('data',data,'/'),('control',control,'/healthz')]:
            old=json.loads((runtime/'children.json').read_text())[name]['pid']
            assert Path(f'/proc/{old}/exe').resolve()==args.nginx.resolve()
            os.kill(old,signal.SIGKILL)
            def proxy_restarted():
                try:
                    new=json.loads((runtime/'children.json').read_text())[name]['pid']
                    return new!=old and not group_alive(old) and http(port,path,timeout=2)['status']==(404 if name=='data' else 200)
                except (OSError,ValueError,HTTPException):return False
            wait(proxy_restarted,30)
            records.append({'case':name+'-master-crash-worker-reaping','passed':True})
        # Kill only the backend owned by this supervisor, then require recovery.
        child=json.loads((runtime/'children.json').read_text())['backend'];pid=child['pid']
        assert Path(f'/proc/{pid}/exe').resolve()==(args.package/'q4t').resolve()
        os.kill(pid,signal.SIGKILL)
        def restarted():
            try:
                new=json.loads((runtime/'children.json').read_text())['backend']['pid']
                return new!=pid and http(control,'/healthz',timeout=2)['status']==200
            except (OSError,ValueError,HTTPException):return False
        wait(restarted,180);generate(1024,'restart-smoke')
        records.append({'case':'supervised-backend-crash-recovery','passed':True})
        save(out/'resource-results.json',records)
        # Warm all supported prompt shapes once before the RSS growth window.
        for length in [1024,4096,8192,45056,204800]:generate(length,'warm-'+str(length))
        pid=json.loads((runtime/'children.json').read_text())['backend']['pid']
        def rss():
            assert json.loads((runtime/'children.json').read_text())['backend']['pid']==pid,'backend restarted during trial'
            assert Path(f'/proc/{pid}/exe').resolve()==(args.package/'q4t').resolve()
            return next(int(line.split()[1])*1024 for line in Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        baseline=rss();peak=baseline;start=time.monotonic();trial=[]
        lengths=[1024,4096,8192,1024,45056,1024,8192]
        while time.monotonic()-start<1800:
            i=len(trial);length=204800 if i in [0,40,80] else lengths[i%len(lengths)]
            r=generate(length,f'trial-{i:04d}');r.pop('events')
            health=http(control,'/healthz',timeout=2)
            assert health['status']==200 and health['seconds']<2
            assert json.loads(health['body'])['seq_slots_free']==1
            current=rss();peak=max(peak,current);assert peak-baseline<=512*1024*1024,(baseline,peak)
            r.update(rss_bytes=current,health_seconds=health['seconds']);trial.append(r)
            save(out/'trial.json',{'elapsed_seconds':time.monotonic()-start,'baseline_rss':baseline,'peak_rss':peak,'requests':trial})
            time.sleep(5)
        assert len(trial)>=60
        records.append({'case':'1800-second-controlled-trial','requests':len(trial),'rss_growth_bytes':peak-baseline,'passed':True})
    except BaseException as error:
        failure=repr(error);raise
    finally:
        for s in sockets:s.close()
        run('stop')
        save(out/'exit.json',{'failure':failure,'records':records,'stopped':True})


if __name__=='__main__':main()
