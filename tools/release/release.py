"""Local, immutable text-runner packages and bounded process supervision.

All state stays under build/ or .q4t-work/. No public listener or system install.
"""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
MAX_LOG = 4 * 1024 * 1024


def save(path, obj):
    temp = path.with_suffix('.new')
    temp.write_text(json.dumps(obj, indent=2) + '\n')
    os.replace(temp, path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded_log(path, data):
    if path.exists() and path.stat().st_size + len(data) > MAX_LOG:
        os.replace(path, path.with_suffix('.previous'))
    with path.open('ab') as stream:
        stream.write(data)


def capture(pipe, path):
    try:
        while data := pipe.read(4096):
            bounded_log(path, data)
    finally:
        pipe.close()


def verify(package):
    manifest = json.loads((package / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        path = package / name
        if not path.resolve().is_relative_to(package.resolve()) or sha(path) != digest:
            raise RuntimeError('package digest mismatch: ' + name)
    for name, digest in manifest['model_files'].items():
        if sha(MODEL / name) != digest:
            raise RuntimeError('model identity mismatch: ' + name)
    return manifest


def package(args):
    import shutil
    target = args.output.resolve()
    if not any(target.is_relative_to(ROOT / p) for p in ['build','.q4t-work']):
        raise RuntimeError('package output must be under build/ or .q4t-work/')
    target.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.binary, target / 'q4t')
    shutil.copy2(args.cache, target / 'CMakeCache.txt')
    shutil.copy2(__file__, target / 'release.py')
    shutil.copy2(ROOT/'tools/release/q4t-text.service.in',target/'q4t-text.service.in')
    for kind in ['data', 'control']:
        source = ROOT / f'tools/deploy/nginx/q4t-{kind}.conf.in'
        text = source.read_text().replace('proxy_request_buffering on;', 'proxy_request_buffering off;')
        text = text.replace('error_log @PREFIX@/error.log notice;', 'error_log stderr warn;')
        text = text.replace('access_log @PREFIX@/access.log qualification;', 'access_log off;')
        # No chunked uploads: buffering and upstream transfer semantics must
        # not bypass the backend's whole-upload deadline or declared budget.
        text = text.replace('server_name q4t_local;', 'server_name q4t_local;\n    if ($http_transfer_encoding != "") { return 400; }')
        (target / f'{kind}.conf.in').write_text(text)
    if not args.compatibility_snapshot:
        names = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','--','src','include','CMakeLists.txt','tests','tools/release','tools/evalscope','tools/deploy'],cwd=ROOT,text=True).splitlines()
        for name in sorted(set(names)):
            source=ROOT/name
            if source.is_file():
                dest=target/'source'/name
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,dest)
    model_files = {p.name: sha(p) for p in MODEL.glob('*.json')}
    manifest = {'version': 1, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_dirty': bool(subprocess.check_output(['git', 'diff', 'HEAD', '--', 'src', 'include'], cwd=ROOT)),
                'files': {str(p.relative_to(target)): sha(p) for p in target.rglob('*') if p.is_file()},
                'compatibility_snapshot': args.compatibility_snapshot,
                'model': str(MODEL), 'model_files': model_files,
                'scope': 'single Thor; text greedy; MTP off; private loopback; max_seq=1; no public authentication',
                'limits': {'max_prefill': 8192, 'max_len': 208896, 'upload_seconds': 30,
                           'body_budget_bytes': 48*1024*1024, 'chat_admission': 8,
                           'logs_bytes_per_file': MAX_LOG, 'log_generations': 2}}
    if args.compatibility_snapshot:
        manifest['limits'] = None
        manifest['source_commit'] = 'external binary snapshot; see original evidence'
    save(target / 'manifest.json', manifest)
    verify(target)
    print(json.dumps({'package': str(target), 'binary_sha256': sha(target/'q4t')}))


def identity(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def running(root):
    try:
        state = json.loads((root / 'supervisor.json').read_text())
        return state if identity(state['pid']) == state['start'] else None
    except (OSError, ValueError):
        return None


def stop(root):
    state = running(root)
    if not state:
        return
    os.kill(state['pid'], signal.SIGTERM)
    for _ in range(400):
        if not running(root):
            return
        time.sleep(.1)
    raise RuntimeError('supervisor did not stop; retained state for diagnosis')


def request(port, route):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', route)
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


def start(root):
    if running(root):
        raise RuntimeError('already running')
    package_path = (root / 'current').resolve(strict=True)
    verify(package_path)
    config = json.loads((root/'config.json').read_text())
    for key in ['backend_port','data_port','control_port']:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',config[key]))
    command = [sys.executable, str(package_path/'release.py'), 'supervise', '--root', str(root)]
    supervisor = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 240
    config = json.loads((root/'config.json').read_text())
    while time.monotonic() < deadline:
        if supervisor.poll() is not None:
            raise RuntimeError('supervisor exited before readiness; inspect failure.json/events.log')
        try:
            if running(root) and request(config['control_port'], '/healthz')[0] == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(1)
    stop(root)
    raise RuntimeError('release readiness timeout')


def switch(root, target):
    verify(target)
    old = (root/'current').resolve() if (root/'current').exists() else None
    was_running = bool(running(root))
    stop(root)
    temp = root/'current.new'
    temp.unlink(missing_ok=True)
    temp.symlink_to(target)
    os.replace(temp, root/'current')
    try:
        start(root)
    except BaseException:
        stop(root)
        if old:
            temp.symlink_to(old)
            os.replace(temp, root/'current')
            if was_running:
                start(root)
        else:
            (root/'current').unlink(missing_ok=True)
        raise
    if old and old != target:
        temp = root/'previous.new'
        temp.unlink(missing_ok=True)
        temp.symlink_to(old)
        os.replace(temp, root/'previous')


def supervise(root):
    lock = (root/'supervisor.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    package_path = (root/'current').resolve(strict=True)
    verify(package_path)
    config = json.loads((root/'config.json').read_text())
    if sha(Path(config['nginx'])) != config['nginx_sha256']:
        raise RuntimeError('nginx executable identity changed')
    backend, data, control = (config[k] for k in ['backend_port', 'data_port', 'control_port'])
    env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    commands = {'backend': [str(package_path/'q4t'), 'serve', '--model-dir', str(MODEL),
                '--host', '127.0.0.1', '--port', str(backend), '--max-seq', '1',
                '--max-prefill', '8192', '--max-len', '208896', '--max-tokens', '256', '--no-mtp']}
    for kind, port in [('data',data),('control',control)]:
        directory = root/kind
        directory.mkdir(exist_ok=True)
        for name in ['body','proxy','fastcgi','uwsgi','scgi']:
            (directory/name).mkdir(exist_ok=True)
        text = (package_path/f'{kind}.conf.in').read_text().replace('@PREFIX@',str(directory)).replace('@FRONT_PORT@',str(port)).replace('@BACK_PORT@',str(backend))
        path = directory/'nginx.conf'
        path.write_text(text)
        subprocess.run([config['nginx'], '-t', '-p', str(directory)+'/', '-c', str(path)], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commands[kind] = [config['nginx'], '-p', str(directory)+'/', '-c', str(path), '-g', 'daemon off;']
    stopping = threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:
        signal.signal(sig, lambda *_: stopping.set())
    children = {}
    readers = {}
    restarts = []
    launch_times = {}
    def launch(name):
        child = subprocess.Popen(commands[name], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        children[name] = child
        reader = threading.Thread(target=capture,args=(child.stdout,root/(name+'.log')),daemon=True)
        reader.start(); readers[name]=reader
        launch_times[name] = time.monotonic()
        save(root/'children.json',{k:{'pid':p.pid,'start':identity(p.pid),'command':commands[k]} for k,p in children.items()})
        bounded_log(root/'events.log',f'{time.time()} start {name} {child.pid}\n'.encode())
    save(root/'supervisor.json',{'pid':os.getpid(),'start':identity(os.getpid()),'package':str(package_path)})
    try:
        for name in commands:
            launch(name)
        unhealthy_since = None
        last_completed = None
        last_progress = time.monotonic()
        while not stopping.wait(1):
            now = time.monotonic()
            try:
                status, _ = request(backend, '/healthz')
                if status == 503:
                    if unhealthy_since is None: unhealthy_since=now
                    if now-unhealthy_since > 30: children['backend'].kill()
                    else: children['backend'].terminate()
                else:
                    unhealthy_since=None
                if status == 200:
                    _, metrics = request(backend, '/metrics')
                    values = {line.split()[0]:float(line.split()[1]) for line in metrics.splitlines() if line and not line.startswith('#') and len(line.split())==2}
                    done = sum(values.get('q4t_requests_'+x+'_total',0) for x in ['success','error','aborted'])
                    outstanding = values.get('q4t_requests_total',0)-done
                    # Rejected/queued-cancelled requests must not hide a
                    # stalled single-sequence GPU. Only generated tokens
                    # constitute execution progress in this profile.
                    progress = values.get('q4t_generation_tokens_total',0)
                    if progress != last_completed or outstanding <= 0:
                        last_completed=progress; last_progress=now
                    if outstanding > 0 and now-last_progress > 1260:
                        children['backend'].kill()
                        bounded_log(root/'events.log',b'backend execution watchdog expired\n')
            except (OSError, http.client.HTTPException):
                if now-launch_times['backend'] > 240:
                    children['backend'].kill()
            for name,p in list(children.items()):
                if p.poll() is None:
                    continue
                # Reap surviving nginx workers before restarting their master.
                try: os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                readers[name].join(timeout=5)
                restarts = [t for t in restarts if now-t < 300]
                if len(restarts) >= 5:
                    raise RuntimeError('restart budget exhausted (5 per 300s)')
                restarts.append(now)
                if stopping.wait(2):
                    break
                launch(name)
                if name == 'backend':
                    last_completed=None; last_progress=time.monotonic(); unhealthy_since=None
    except BaseException as error:
        save(root/'failure.json',{'error':repr(error),'time':time.time()})
        raise
    finally:
        for p in children.values():
            try: os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError: pass
        deadline=time.monotonic()+30
        for p in children.values():
            try: p.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGKILL); p.wait()
        for reader in readers.values(): reader.join(timeout=5)
        bounded_log(root/'events.log',b'supervisor stopped\n')
        fcntl.flock(lock,fcntl.LOCK_UN)
        (root/'supervisor.json').unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['package','activate','start','stop','status','rollback','supervise','verify'])
    parser.add_argument('--root',type=Path)
    parser.add_argument('--package',type=Path)
    parser.add_argument('--compatibility-snapshot',action='store_true')
    parser.add_argument('--binary',type=Path)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--nginx',default='/usr/sbin/nginx')
    parser.add_argument('--backend-port',type=int,default=18080)
    parser.add_argument('--data-port',type=int,default=18081)
    parser.add_argument('--control-port',type=int,default=18082)
    args=parser.parse_args()
    if args.action=='package':
        if not all([args.binary,args.cache,args.output]): parser.error('package requires binary/cache/output')
        package(args); return
    if args.action=='verify': verify(args.package.resolve()); return
    root=args.root.resolve()
    # ROOT differs for packaged copies; use project-independent ancestor names.
    if not any(p.name in ['build','.q4t-work'] for p in root.parents):
        parser.error('state must be inside build/ or .q4t-work/')
    root.mkdir(parents=True,exist_ok=True)
    if args.action=='supervise': supervise(root); return
    with (root/'operation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='activate':
            if not (root/'config.json').exists():
                ports=[args.backend_port,args.data_port,args.control_port]
                if len(set(ports))!=3 or not all(1024<=p<=65535 for p in ports): parser.error('three distinct unprivileged ports required')
                save(root/'config.json',{'backend_port':ports[0],'data_port':ports[1],'control_port':ports[2],'nginx':str(Path(args.nginx).resolve()),'nginx_sha256':sha(Path(args.nginx).resolve())})
            switch(root,args.package.resolve())
        elif args.action=='rollback': switch(root,(root/'previous').resolve(strict=True))
        elif args.action=='start': start(root)
        elif args.action=='stop': stop(root)
        elif args.action=='status': print(json.dumps({'running':running(root),'current':str((root/'current').resolve())}))


if __name__=='__main__':
    main()
