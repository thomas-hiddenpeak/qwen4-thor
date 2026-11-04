"""Safety/evidence contracts and small real CPU-only ownership checks."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_pressure import stage, pressure, verify, observe
from isolated_scope import IsolatedScope

CPU = stage.BASE / 'cpu'


class Driver(unittest.TestCase):
    def test_observation_rejects_unknown_contradictory_duplicate_and_partial(self):
        good = 'MemTotal: 100 kB\nMemFree: 10 kB\nMemAvailable: 20 kB\n'
        self.assertEqual(pressure.parse_memory(good)['MemAvailable'], 20480)
        for raw in (good[:-1], good+'MemTotal: 100 kB\n', good.replace('100', '0'),
                    good.replace('10 kB', '101 kB'), good.replace('20 kB', '-1 kB'),
                    good.replace('MemAvailable: 20 kB\n', ''), good.replace('kB', 'MB')):
            with self.assertRaises(RuntimeError):
                pressure.parse_memory(raw)

    def test_reserves_and_allocation_cap(self):
        sample = dict(MemAvailable=pressure.MIN_AVAILABLE+pressure.CHUNK,
                      MemFree=pressure.MIN_FREE+pressure.CHUNK)
        pressure.safe(sample, pressure.MAX_BYTES-pressure.CHUNK, pressure.CHUNK)
        for held, extra in ((pressure.MAX_BYTES, pressure.CHUNK), (0, 1), (-1, 0)):
            with self.assertRaises(RuntimeError):
                pressure.safe(sample, held, extra)
        for key in sample:
            low = dict(sample)
            low[key] -= 1
            with self.assertRaises(RuntimeError):
                pressure.safe(low, 0, pressure.CHUNK)

    def test_only_complete_content_deltas_count_as_progress(self):
        with tempfile.TemporaryDirectory(dir=CPU) as tmp:
            path = Path(tmp)/'wire'
            path.write_bytes(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"42"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"unfinished')
            self.assertEqual(verify.content_bytes(path), 2)

    def test_resource_envelope_does_not_allow_missing_cycles_or_growth(self):
        row = dict(parent=dict(VmRSS=1024, fds=20, Threads=5), resources=dict(cgroup=dict(
            observations={'memory.stat':dict(value=dict(anon=2048))})))
        rows = [copy.deepcopy(row) for _ in range(64)]
        self.assertFalse(observe.plateau(rows)['physical_ram_peak_proved'])
        with self.assertRaises(RuntimeError):
            observe.plateau(rows[:-1])
        for key, delta in (('VmRSS', 64*1024**2+1), ('fds', 3), ('Threads', 3)):
            altered = copy.deepcopy(rows)
            altered[-1]['parent'][key] += delta
            with self.assertRaises(RuntimeError):
                observe.plateau(altered)
        rows[-1]['resources']['cgroup']['observations']['memory.stat']['value']['anon'] += 128*1024**2+1
        with self.assertRaises(RuntimeError):
            observe.plateau(rows)

    def test_original_time_and_attempt_budget(self):
        run = verify.Run.__new__(verify.Run)
        run.deadline, run.monitor, run.out = 100, None, CPU
        run.counts = dict(stage.LIMITS)
        with patch.object(verify.time, 'time', return_value=101):
            with self.assertRaises(RuntimeError):
                run.budget()
        with patch.object(verify.time, 'time', return_value=99):
            for kind in ('generations', 'negatives', 'starts'):
                with self.assertRaises(RuntimeError):
                    run.budget(kind)

    def test_full_wire_retains_error_done_before_client_raises(self):
        raw = b'data: {"error":{"code":"memory_pressure","message":"low","type":"server_error","param":null}}\n\ndata: [DONE]\n\n'
        response = io.BytesIO(raw)
        response.status = 200
        response.getheaders = lambda: [('Content-Type', 'text/event-stream')]
        response.headers = type('Headers', (), {'get_content_type':lambda self:'text/event-stream'})()
        conn = type('Connection', (), dict(request=lambda *a:None, getresponse=lambda *a:response,
                                          close=lambda *a:None))()
        with tempfile.TemporaryDirectory(dir=CPU) as tmp:
            captured = bytearray()
            with patch.object(verify.http.client, 'HTTPConnection', return_value=conn):
                with self.assertRaises(verify.RemoteError):
                    verify.FullWireClient(1, Path(tmp), 'queued').complete(dict(stream=True), sink=captured.extend)
            self.assertEqual(captured, raw)
            self.assertEqual(json.loads((Path(tmp)/'queued-headers.json').read_text())['status'], 200)

    def test_small_real_pressure_released_on_eof(self):
        folder = CPU/'pressure-eof'
        folder.mkdir()
        client = pressure.Pressure(folder)
        try:
            row = client.command('add')
            self.assertEqual(row['allocated'], pressure.CHUNK)
            status = (Path('/proc')/str(client.process.pid)/'status').read_text()
            self.assertGreaterEqual(observe.status_fields(status)['VmRSS'], pressure.CHUNK)
        finally:
            client.close()
        self.assertEqual(client.process.returncode, 0)
        rows = [json.loads(line) for line in (folder/'pressure.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['allocated'], 0)
        self.assertTrue(rows[-1]['final'])

    def test_real_parent_death_releases_pressure(self):
        folder = CPU/'pressure-parent-death'
        folder.mkdir()
        script = (
            'import os,sys; from pathlib import Path; '
            'from tools.serve_pressure.pressure import Pressure; '
            'p=Pressure(Path(sys.argv[1])); p.command("add"); '
            'print(p.process.pid,flush=True); os._exit(0)')
        result = subprocess.run(['/usr/bin/python3','-B','-c',script,str(folder)],
            cwd=ROOT, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = int(result.stdout)
        end = time.monotonic()+5
        while Path(f'/proc/{pid}/stat').exists() and time.monotonic() < end:
            if Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0] == 'Z':
                break
            time.sleep(.05)
        if Path(f'/proc/{pid}/status').exists():
            self.assertNotIn('VmRSS', observe.status_fields(Path(f'/proc/{pid}/status').read_text()))
        stage.h.save(folder/'result.json', dict(parent_returncode=result.returncode,
            helper_pid=pid, address_space_released=True))

    def test_real_scope_ownership_ancestry_and_cleanup(self):
        folder = CPU/'scope-smoke'
        folder.mkdir()
        scope = IsolatedScope(['/usr/bin/python3','-c','import time; time.sleep(3)'],
            cwd=ROOT, env=dict(PATH=os.environ['PATH']), log_path=folder/'payload.log',
            unit=f'q4t-pressure-cpu-{os.getpid()}.scope', memory_max=None, evidence=folder/'scope')
        try:
            identity = stage.h.read(folder/'scope/identity.json')
            sample = observe.sample(scope, identity['start_ticks'])
            self.assertEqual(sample['resources']['cgroup']['observations']['memory.max']['value'], 'max')
            pid, ancestors = scope.pid, []
            while pid > 1 and len(ancestors) < 64:
                ancestors.append(pid)
                pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
            self.assertIn(os.getpid(), ancestors)
            stage.h.save(folder/'ancestry.json', dict(controller=os.getpid(), ancestors=ancestors))
            self.assertEqual(scope.wait(timeout=5), 0)
            scope.snapshot('exited')
        finally:
            scope.close()
        self.assertTrue(scope.closed)


if __name__ == '__main__':
    CPU.mkdir(parents=True, exist_ok=False)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(Driver)
    suite.addTests(loader.discover(str(ROOT/'tools/evalscope'), pattern='test_isolated_scope.py'))
    with (CPU/'tests.log').open('x') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    stage.h.save(CPU/'result.json', dict(passed=result.wasSuccessful(), tests=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), model_or_http_started=False))
    print((CPU/'tests.log').read_text())
    sys.exit(0 if result.wasSuccessful() else 1)
