"""Real nginx control saturation, bounded mixed load and backend restart."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--nginx', type=Path, required=True)
    ap.add_argument('--lifecycle', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--baseline', action='store_true')
    ap.add_argument('--config-template', type=Path,
                    default=ROOT / 'tools/deploy/nginx/q4t.conf.in')
    ap.add_argument('--seconds', type=int, default=600)
    args = ap.parse_args()
    assert 600 <= args.seconds <= 1800
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    save = lambda name, data: (out / name).write_text(json.dumps(data, indent=2) + '\n')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    prior = args.lifecycle.resolve()
    cmd = json.loads((prior / 'plan.json').read_text())['command']
    cmd[0] = str(args.binary.resolve())
    cmd[cmd.index('--max-seq') + 1] = '1'
    cmd += ['--host', '127.0.0.1']
    back = int(cmd[cmd.index('--port') + 1]); front = back + 80
    template = args.config_template.resolve()
    conf = template.read_text().replace('@PREFIX@', str(out)).replace('@BACK_PORT@', str(back)).replace('@FRONT_PORT@', str(front))
    (out / 'nginx.conf').write_text(conf)
    shutil.copy2(__file__, out)
    shutil.copy2(template, out)
    nginx = [str(args.nginx.resolve()), '-p', str(out), '-c', str(out / 'nginx.conf')]
    save('plan.json', dict(command=cmd, nginx=nginx, binary_sha256=sha(args.binary), nginx_sha256=sha(args.nginx), template_sha256=sha(template), baseline=args.baseline, minimum_seconds=args.seconds, minimum_cycles=12, maximum_seconds=args.seconds+120))
    (out / 'config-check.log').write_bytes(subprocess.check_output(nginx + ['-t'], stderr=subprocess.STDOUT))
    records = []; held = []; exits = []; resource = []
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    server = proxy = None
    logs = []
    failure = None

    def http(path, body=None):
        c = HTTPConnection('127.0.0.1', front, timeout=90)
        try:
            c.request('POST' if body is not None else 'GET', path, None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            r = c.getresponse()
            return {'status': r.status, 'body': r.read().decode()}
        finally:
            c.close()

    def wait(fn, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if fn(): return
            time.sleep(.05)
        raise AssertionError('condition timeout')

    def health():
        r = http('/healthz'); assert r['status'] == 200, r
        return json.loads(r['body'])

    def idle():
        wait(lambda: health()['seq_slots_free'] == 1 and health()['requests_running'] == 1)

    def start_server(label):
        log = (out / (label + '.log')).open('w'); logs.append(log)
        p = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        wait(lambda: 'serving on port' in (out / (label + '.log')).read_text() or p.poll() is not None, 180)
        assert p.poll() is None
        return p

    def payload(label, short=False):
        d = json.loads((prior / ('canary-request.json' if short else 'active-a-request.json')).read_text())
        d.update(request_id=label, cancel_token=secrets.token_hex(32))
        save(label + '-request.json', d)
        return d

    def cancel(d):
        return http('/v1/requests/cancel', {k:d[k] for k in ['request_id','cancel_token']})

    def stall(path, n):
        for _ in range(n):
            s = socket.create_connection(('127.0.0.1',front),timeout=5); held.append(s)
            method = 'POST' if path.endswith('cancel') else 'GET'
            s.sendall(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\n{{'.encode())
        time.sleep(.3)

    def release():
        for s in held: s.close()
        held.clear()
        time.sleep(.2)

    def normal(label, short=False):
        r = http('/v1/chat/completions', payload(label, short))
        save(label + '-response.json', r)
        assert r['status'] == 200 and r['body'].strip().endswith('data: [DONE]')
        events = [json.loads(x[6:]) for x in r['body'].splitlines() if x.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        assert ''.join(c.get('delta', {}).get('content','') for c in choices) == ('710003' if short else '711146')
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
        usage = next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens'] == (1024 if short else 45056) and usage['completion_tokens'] == 7

    def sample(cycle):
        idle()
        time.sleep(.05)
        status = Path(f'/proc/{server.pid}/status').read_text()
        fields = {line.split(':')[0]:line.split(':')[1].strip() for line in status.splitlines() if line.startswith(('VmRSS:', 'Threads:'))}
        resource.append({'cycle':cycle,'fd_count':len(list(Path(f'/proc/{server.pid}/fd').iterdir())),**fields})
        save('resources.json',resource)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            server = start_server('server-0')
            log = (out/'proxy.log').open('w'); logs.append(log)
            proxy = subprocess.Popen(nginx+['-g','daemon off;'],stdout=log,stderr=subprocess.STDOUT)
            def ready():
                assert proxy.poll() is None
                try: return http('/healthz')['status'] == 200
                except ConnectionRefusedError: return False
            wait(ready)
            fake = {'request_id':'missing','cancel_token':'a'*64}
            stall('/metrics', 8 if args.baseline else 4)
            r = cancel(fake); save('metrics-saturated-cancel.json',r)
            assert r['status'] == (429 if args.baseline else 404)
            if args.baseline:
                records.append('shared monitoring budget blocks cancel with 429')
            else:
                assert health()['gpu_healthy']
                d = payload('metrics-full-active'); f = pool.submit(http,'/v1/chat/completions',d)
                wait(lambda: health()['seq_slots_free'] == 0)
                assert cancel(d)['status'] == 202
                r = f.result(timeout=30);save('metrics-full-active-response.json',r);assert r['status']==409
                records.append('full metrics budget preserves health and active cancellation')
            release()
            if not args.baseline:
                stall('/v1/requests/cancel',8)
                r=cancel(fake);save('cancel-saturated-response.json',r);assert r['status']==429
                assert health()['gpu_healthy'] and http('/metrics')['status']==200
                # Idle request-body timeout must release the cancellation budget.
                start=time.monotonic()
                wait(lambda: cancel(fake)['status']==404,15)
                save('cancel-timeout-recovery.json',{'seconds':time.monotonic()-start})
                release();records.append('cancel saturation is bounded; monitoring survives and idle uploads expire')
                start=time.monotonic();cycle=0
                while time.monotonic()-start < args.seconds or cycle < 12:
                    assert time.monotonic()-start < args.seconds+120
                    normal(f'cycle-{cycle}-long')
                    d=payload(f'cycle-{cycle}-active');f=pool.submit(http,'/v1/chat/completions',d)
                    wait(lambda: health()['seq_slots_free']==0)
                    queued=payload(f'cycle-{cycle}-queued',True);q=pool.submit(http,'/v1/chat/completions',queued)
                    # Wait for registration by retrying a cancellation until accepted.
                    wait(lambda: cancel(queued)['status']==202)
                    assert q.result(timeout=10)['status']==409
                    assert cancel(d)['status']==202
                    r=f.result(timeout=30);save(f'cycle-{cycle}-cancel-response.json',r);assert r['status']==409
                    normal(f'cycle-{cycle}-short',True)
                    sample(cycle);cycle+=1
                    print('cycle',cycle,'elapsed',round(time.monotonic()-start),flush=True)
                save('soak.json',{'cycles':cycle,'seconds':time.monotonic()-start})
                assert max(x['fd_count'] for x in resource) - min(x['fd_count'] for x in resource) <= 1
                assert len({x['Threads'] for x in resource}) == 1
                records.append('bounded mixed load completed with exact outputs and reclaimed slots')
                d=payload('restart-active');f=pool.submit(http,'/v1/chat/completions',d)
                wait(lambda:health()['seq_slots_free']==0);time.sleep(.5)
                server.terminate();server.wait(timeout=60);exits.append(server.returncode);assert server.returncode==0
                r=f.result(timeout=30);save('restart-active-response.json',r);assert r['status'] in [409,502]
                r=http('/healthz');save('backend-down-response.json',r);assert r['status']==502
                server=start_server('server-1');assert health()['gpu_healthy']
                normal('restart-recovery',True);idle()
                records.append('active shutdown, observable backend outage and restart output recovery')
        except Exception as exc:
            failure=repr(exc)
            raise
        finally:
            release()
            if proxy and proxy.poll() is None:proxy.terminate();proxy.wait(timeout=20)
            if server and server.poll() is None:
                server.terminate()
                try:server.wait(timeout=60)
                except subprocess.TimeoutExpired:server.kill();server.wait()
                exits.append(server.returncode)
            for log in logs:log.close()
            save('exit.json',{'failure':failure,'server_exits':exits,'proxy':proxy.returncode if proxy else None,'records':records})
    assert all(x==0 for x in exits) and proxy.returncode==0
    print('control qualification completed',flush=True)


if __name__=='__main__':main()
