"""CPU end-to-end actor for the real Controller; never a packaged dependency."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import controller, jobs, monitor
from tools.serve_recovery.test_gateway import config
from tools.serve_recovery.test_gateway_nginx import native_config
from tools.serve_service import package as engine


def cpu_backend(args):
    value = config(args.directory, ports=(1, args.port, args.operations_port))
    value['backend_policy'] = json.loads(args.client_policy_file.read_text())
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_args):
            pass
        def reply(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
        def do_GET(self):
            if self.path in ('/livez', '/readyz'):
                self.reply(200, dict(status='ready', live=True, ready=True))
            elif self.path == '/healthz':
                self.reply(200, dict(status='ok', gpu_healthy=True))
            elif self.path == '/v1/config':
                self.reply(200, native_config(value, value['backend_policy']['gateway_token']))
            else:
                self.reply(404, dict(error='absent'))
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.headers.get('X-Q4T-Gateway-Token') != value['backend_policy']['gateway_token']:
                self.reply(403, dict(error='bad internal credential'))
                return
            self.reply(200, dict(id='cpu-self-check', usage=dict(prompt_tokens=20, completion_tokens=5),
                choices=[dict(finish_reason='stop', message=dict(role='assistant', content='{"ok":true}'))]))
    servers = [ThreadingHTTPServer(('127.0.0.1', port), Handler) for port in (args.port, args.operations_port)]
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    (args.client_policy_file.parent/'cpu-started.json').write_text(json.dumps(dict(pid=os.getpid())))
    while True:
        if (args.directory/'log.flood').exists() and not (args.directory/'log.drained').exists():
            for _ in range(512):
                os.write(1, b'CPU ordinary log\n'.ljust(4096, b'x'))
            (args.directory/'log.drained').touch()
        time.sleep(0.05)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run_controller(directory):
    # Real lifecycle/HTTP/exec code. Only release fixtures, NVIDIA observations
    # and GPU work are replaced; these seams cannot be selected by service CLI.
    package = directory/'cpu-package'
    package.mkdir(mode=0o700, exist_ok=True)
    (package/'engine').mkdir(mode=0o700, exist_ok=True)
    (package/'bin').mkdir(mode=0o700, exist_ok=True)
    ports = free_port(), free_port(), free_port()
    base_policy = config(directory, ports)['backend_policy']
    path = package/'engine/client-policy.json'
    path.write_text(json.dumps(base_policy))
    path.chmod(0o600)
    probe = package/'bin/q4t-cuda-probe'
    if not probe.exists():
        probe.write_text('#!'+sys.executable+'\nprint(\'{"version":1,"ok":true,"cuda_code":0,"stage":"complete"}\')\n')
        probe.chmod(0o500)
    base = dict(settings=engine.settings(ports[1], 0, operations_port=ports[2], nvml_policy='degrade'),
        model_dir=str(directory), protocol_sha256='c'*64,
        files=dict(q4t=hashlib.sha256(Path('/proc/self/exe').read_bytes()).hexdigest()),
        client_policy=dict(enabled=True))
    controller.bundle.verify = lambda *_args: dict(version=1, engine_manifest_sha256='a'*64, model_assets={})
    controller.bundle.check_assets = lambda *_args, **_kwargs: None
    controller.engine.verify = lambda *_args: base
    controller.engine.command = lambda *_args, **_kwargs: [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
        '--backend', '--directory', str(directory), '--port', str(ports[1]),
        '--operations-port', str(ports[2]), '--client-policy-file', str(path)]
    controller.ownership.observe = lambda *_args: dict(conflicts=[], unconfirmed_compute=[])
    original_monitor = monitor.Monitor
    def factory(command, **kwargs):
        request = command[-1]
        failed = (directory/'monitor.unavailable').exists()
        result = dict(event='result', request_id=request, device_count=1, pids=[] if not failed else None,
                      status='ERROR' if failed else 'OK', error=dict(type='NVMLError',
                      message='CPU injected timeout', nvml_code=10) if failed else None)
        code = 'import sys; print('+repr(json.dumps(result))+',flush=True); sys.exit('+('1' if failed else '0')+')'
        return jobs.Job([sys.executable, '-B', '-c', code], **kwargs)
    controller.monitor.Monitor = lambda fd: original_monitor(fd, factory=factory)
    cfg = dict(version=1, package=str(package), manifest_sha256='a'*64, directory=str(directory))
    config_path = directory/'production-controller.json'
    config_path.write_text(json.dumps(cfg))
    config_path.chmod(0o600)
    sys.argv = [sys.argv[0], '--config', str(config_path)]
    controller.main()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--backend', action='store_true')
    parser.add_argument('--port', type=int)
    parser.add_argument('--operations-port', type=int)
    parser.add_argument('--client-policy-file', type=Path)
    args = parser.parse_args()
    cpu_backend(args) if args.backend else run_controller(args.directory)
