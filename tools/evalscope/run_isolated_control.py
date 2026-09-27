"""Verify separate nginx process resources with real connection exhaustion."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--nginx', type=Path, required=True)
    ap.add_argument('--lifecycle', type=Path, required=True)
    ap.add_argument('--quality-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--shared', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build','.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    save = lambda name,d: (out/name).write_text(json.dumps(d,indent=2)+'\n')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    cmd=json.loads((args.lifecycle/'plan.json').read_text())['command']
    cmd[0]=str(args.binary.resolve());cmd += ['--host','127.0.0.1']
    for flag,value in [('--max-seq','1'),('--max-len','208896')]:cmd[cmd.index(flag)+1]=value
    back=int(cmd[cmd.index('--port')+1]);data_port=back+80;control_port=data_port if args.shared else back+81
    nginx=str(args.nginx.resolve());profiles={};processes={};logs=[];held=[];records=[];exits=[]
    shutil.copy2(__file__,out)
    for role,port in [('data',data_port)]+([] if args.shared else [('control',control_port)]):
        directory=out/role;directory.mkdir()
        template=ROOT/'tools/deploy/nginx'/('q4t.conf.in' if args.shared else 'q4t-'+role+'.conf.in')
        shutil.copy2(template,directory)
        content=template.read_text().replace('worker_connections 512','worker_connections 128').replace('@PREFIX@',str(directory)).replace('@BACK_PORT@',str(back)).replace('@FRONT_PORT@',str(port))
        (directory/'nginx.conf').write_text(content)
        profiles[role]=[nginx,'-p',str(directory),'-c',str(directory/'nginx.conf')]
        (directory/'config-check.log').write_bytes(subprocess.check_output(profiles[role]+['-t'],stderr=subprocess.STDOUT))
    save('plan.json',dict(command=cmd,profiles=profiles,binary_sha256=sha(args.binary),nginx_sha256=sha(args.nginx),shared=args.shared,worker_connections_per_process=128,rounds=1 if args.shared else 3,health_and_cancel_call_timeout_seconds=2))
    env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
    server=None;failure=None

    def http(port,path,body=None,timeout=300):
        c=HTTPConnection('127.0.0.1',port,timeout=timeout)
        try:
            start=time.monotonic();c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),{'Content-Type':'application/json'})
            r=c.getresponse();result={'status':r.status,'body':r.read().decode(),'seconds':time.monotonic()-start};return result
        finally:c.close()

    def wait(fn,seconds=20):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if fn():return
            time.sleep(.05)
        raise AssertionError('condition timeout')

    def health():
        r=http(control_port,'/healthz',timeout=2);assert r['status']==200,r;return json.loads(r['body'])

    def start_proxy(role,index):
        log=(out/role/f'process-{index}.log').open('w');logs.append(log)
        p=subprocess.Popen(profiles[role]+['-g','daemon off;'],stdout=log,stderr=subprocess.STDOUT);processes[role]=p
        def ready():
            assert p.poll() is None
            try:
                r=http(data_port if role=='data' else control_port,'/healthz',timeout=2)
                return r['status']==(200 if args.shared or role=='control' else 404)
            except ConnectionRefusedError:return False
        wait(ready)

    def payload(label,short=False):
        d=json.loads((args.lifecycle/('canary-request.json' if short else 'active-a-request.json')).read_text())
        d.update(request_id=label,cancel_token=secrets.token_hex(32));save(label+'-request.json',d);return d

    def cancel(d):return http(control_port,'/v1/requests/cancel',{k:d[k] for k in ['request_id','cancel_token']},timeout=2)

    def release():
        for s in held:s.close()
        held.clear();time.sleep(.1)

    def normal(label,r,expected,tokens):
        save(label+'-response.json',r)
        assert r['status']==200 and r['body'].strip().endswith('data: [DONE]')
        events=[json.loads(x[6:]) for x in r['body'].splitlines() if x.startswith('data: {')]
        choices=[c for e in events for c in e.get('choices',[])]
        assert ''.join(c.get('delta',{}).get('content','') for c in choices)==expected
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')]==['stop']
        usage=next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens']==tokens and usage['completion_tokens']==7

    def worker_ids(role):
        pid=processes[role].pid
        # Thor's kernel does not expose /proc/PID/task/PID/children.
        result = subprocess.run(['ps', '--ppid', str(pid), '-o', 'pid='],
                                stdout=subprocess.PIPE, check=False)
        assert result.returncode in (0, 1)
        return result.stdout.decode().split()

    def exhaust(label):
        log=out/'data/error.log';before=log.read_text().count('worker_connections are not enough')
        for i in range(160):
            try:
                s=socket.create_connection(('127.0.0.1',data_port),timeout=.05);held.append(s)
                s.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nX-Slow: ')
            except OSError:pass
            if log.read_text().count('worker_connections are not enough')>before:break
        wait(lambda:log.read_text().count('worker_connections are not enough')>before,2)
        save(label+'-exhaustion.json',{'held_clients':len(held),'data_master':processes['data'].pid,'data_workers':worker_ids('data'),'control_master':processes.get('control',processes['data']).pid,'control_workers':worker_ids('control') if not args.shared else worker_ids('data')})

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            log=(out/'server.log').open('w');logs.append(log)
            server=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
            wait(lambda:'serving on port' in (out/'server.log').read_text() or server.poll() is not None,180);assert server.poll() is None
            start_proxy('data',0)
            if not args.shared:start_proxy('control',0)
            if not args.shared:
                assert http(data_port,'/healthz')['status']==404
                assert http(control_port,'/v1/chat/completions',{})['status']==404
            for i in range(1 if args.shared else 3):
                label=f'flood-{i}';d=payload(label);f=pool.submit(http,data_port,'/v1/chat/completions',d)
                wait(lambda:health()['seq_slots_free']==0)
                exhaust(label)
                if args.shared:
                    try:r=http(control_port,'/healthz',timeout=2)
                    except (OSError,HTTPException) as exc:r={'transport_error':repr(exc)}
                    save('shared-health.json',r);assert r.get('status')!=200,r
                    release();wait(lambda:health()['gpu_healthy'])
                else:
                    assert set(worker_ids('data')).isdisjoint(worker_ids('control'))
                    for j in range(5):
                        r=http(control_port,'/healthz',timeout=2);save(f'{label}-health-{j}.json',r);assert r['status']==200 and r['seconds']<2
                r=cancel(d);save(label+'-cancel.json',r);assert r['status']==202 and r['seconds']<2
                r=f.result(timeout=20);save(label+'-response.json',r);assert r['status']==409
                release();wait(lambda:health()['seq_slots_free']==1)
                normal(label+'-recovery',http(data_port,'/v1/chat/completions',payload(label+'-recovery',True)),'710003',1024)
            records.append('shared failure reproduced' if args.shared else 'three data connection exhaustion rounds preserve control calls and cancellation')
            if not args.shared:
                # Reload data workers with a generation already in progress.
                d=payload('reload-active');f=pool.submit(http,data_port,'/v1/chat/completions',d);wait(lambda:health()['seq_slots_free']==0)
                old=set(worker_ids('data'));processes['data'].send_signal(signal.SIGHUP)
                wait(lambda:bool(set(worker_ids('data'))-old));assert health()['gpu_healthy']
                normal('reload-active',f.result(timeout=90),'711146',45056)
                wait(lambda:not(set(worker_ids('data'))&old))
                records.append('data reload preserves in-flight output')
                # Stopping data must not stop control; in-flight generation aborts.
                d=payload('data-stop-active');f=pool.submit(http,data_port,'/v1/chat/completions',d);wait(lambda:health()['seq_slots_free']==0)
                p=processes['data'];p.terminate();p.wait(timeout=20);exits.append({'role':'data-0','exit':p.returncode});assert p.returncode==0
                assert health()['gpu_healthy']
                try:r=f.result(timeout=20)
                except (OSError,HTTPException) as exc:r={'transport_error':repr(exc)}
                save('data-stop-active-response.json',r);assert r.get('status')!=200
                wait(lambda:health()['seq_slots_free']==1)
                start_proxy('data',1)
                normal('data-restart-recovery',http(data_port,'/v1/chat/completions',payload('data-restart-recovery',True)),'710003',1024)
                records.append('data stop leaves control alive and restart recovers output')
                # Stopping control must not interrupt data computation.
                d=payload('control-stop-active');f=pool.submit(http,data_port,'/v1/chat/completions',d);wait(lambda:health()['seq_slots_free']==0)
                p=processes['control'];p.terminate();p.wait(timeout=20);exits.append({'role':'control-0','exit':p.returncode});assert p.returncode==0
                start_proxy('control',1);assert health()['gpu_healthy']
                normal('control-stop-active',f.result(timeout=90),'711146',45056)
                records.append('control restart does not interrupt data output')
                row=next(x for x in json.loads((args.quality_run/'results.json').read_text()) if x['actual_input']==204800)
                prompts=[json.loads(x)['prompt'] for x in (args.quality_run/'inputs/requests.jsonl').read_text().splitlines()]
                d=payload('context-200k');d['prompt']=next(x for x in prompts if hashlib.sha256(x.encode()).hexdigest()==row['prompt_sha256']);save('context-200k-request.json',d)
                normal('context-200k',http(data_port,'/v1/chat/completions',d),row['text'],204800)
                records.append('200K output survives separate data/control routing')
        except Exception as exc:failure=repr(exc);raise
        finally:
            release()
            for role,p in processes.items():
                if p.poll() is None:p.terminate();p.wait(timeout=20);exits.append({'role':role,'exit':p.returncode})
            if server and server.poll() is None:
                server.terminate()
                try:server.wait(timeout=60)
                except subprocess.TimeoutExpired:server.kill();server.wait()
            for log in logs:log.close()
            save('exit.json',{'failure':failure,'server':server.returncode if server else None,'proxy_exits':exits,'records':records})
    assert server.returncode==0 and all(x['exit']==0 for x in exits)
    print('isolated control qualification passed',flush=True)


if __name__=='__main__':main()
