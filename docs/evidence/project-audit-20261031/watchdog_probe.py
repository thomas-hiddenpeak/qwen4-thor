"""Run the actual supervisor loop with process/clock doubles; no GPU/HTTP."""
import importlib.util
import json
from pathlib import Path
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release', ROOT / 'tools/release/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
root = Path(__file__).with_name('watchdog-fixture')
root.mkdir()
package = root / 'package'
package.mkdir()
(root / 'current').symlink_to(package.resolve())
for name in ['data', 'control']:
    (package / (name + '.conf.in')).write_text('synthetic configuration')
(root / 'config.json').write_text(json.dumps(dict(
    backend_port=1, data_port=2, control_port=3,
    nginx='synthetic-nginx', nginx_sha256='synthetic')))

clock = types.SimpleNamespace(now=0, waits=0)
kills = []

class Event:
    def set(self):
        pass
    def wait(self, seconds):
        clock.waits += 1
        if clock.waits > 2:
            return True
        clock.now = 0 if clock.waits == 1 else 1261
        return False

class Child:
    def __init__(self, command, **kwargs):
        self.name = 'backend' if command[0].endswith('/q4t') else 'proxy'
        self.pid = 10000000
        self.stdout = None
    def poll(self):
        return None
    def kill(self):
        kills.append((self.name, clock.now))
    def terminate(self):
        pass
    def wait(self, **kwargs):
        return 0

class Thread:
    def __init__(self, **kwargs):
        pass
    def start(self):
        pass
    def join(self, **kwargs):
        pass

def response(port, route):
    if route == '/healthz':
        return 200, '{}'
    # The C++ producer only increments generation_tokens_total when a request
    # leaves generation, so active streaming produces these same snapshots.
    return 200, 'q4t_requests_total 1\nq4t_requests_success_total 0\nq4t_generation_tokens_total 0\n'

with patch.object(release, 'verify', return_value={}), \
     patch.object(release, 'sha', return_value='synthetic'), \
     patch.object(release, 'identity', return_value='synthetic'), \
     patch.object(release, 'request', side_effect=response), \
     patch.object(release.subprocess, 'run'), \
     patch.object(release.subprocess, 'Popen', Child), \
     patch.object(release.threading, 'Event', Event), \
     patch.object(release.threading, 'Thread', Thread), \
     patch.object(release.time, 'monotonic', lambda: clock.now), \
     patch.object(release.signal, 'signal'), \
     patch.object(release.os, 'killpg'):
    release.supervise(root)
print(json.dumps({'health_status': 200, 'active_request_timeout_ms': 1800000,
                  'producer_counter_updates': 'only after generation ends',
                  'supervisor_kills': kills, 'actual_processes_started': 0}))
