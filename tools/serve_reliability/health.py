"""Six installed-program cache health requests; two heads, no extra sampling."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools/evalscope'))
from serve_reliability import Runner, capability, decoded, fields, save, turn
import nvml_process_query


class Health(Runner):
    def charge(self, kind, label):
        super().charge(kind, label)
        if self.http_count > 6 or self.starts > 2:
            raise RuntimeError('installed health budget')

    def free(self):
        self.wait(lambda: self.health()['seq_slots_free'] == 4, 60)
        if not self.health()['gpu_healthy']:
            raise AssertionError('unhealthy deployed model')

    def run(self):
        for mode in ['full', 'subset']:
            self.charge('startup', mode)
            folder = self.out/mode
            folder.mkdir()
            command = [self.plan['candidate'], 'serve', '--model-dir', self.plan['model'],
                '--host', '127.0.0.1', '--port', str(self.plan['port']),
                '--max-seq', '4', '--max-prefill', '4096', '--max-len', '208896',
                '--seq-capacities', '208896,65536,65536,65536',
                '--prefix-cache-checkpoints', '8192']
            for kind in ['session', 'prefix']:
                (folder/kind).mkdir(mode=0o700)
                command += ['--'+kind+'-cache-dir', str(folder/kind)]
            if mode == 'subset':
                command += ['--mtp-subset-head', self.plan['artifact']]
            env = {k:v for k,v in os.environ.items()
                   if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
            env['Q4T_SESSION_INCREMENTAL'] = '1'
            save(folder/'command.json', dict(argv=command,
                 environment={'Q4T_SESSION_INCREMENTAL': '1'}))
            server, passed = None, False
            try:
                with (folder/'server.log').open('x') as log:
                    server = subprocess.Popen(command, cwd=ROOT, env=env,
                        stdout=log, stderr=subprocess.STDOUT)
                    def ready():
                        if server.poll() is not None:
                            raise RuntimeError('health startup failed')
                        return 'serving on port' in (folder/'server.log').read_text()
                    self.wait(ready, 180)
                    self.free()
                    prompt = self.plan['cache_prompt']
                    scope = capability('installed-'+mode)
                    session = capability('installed-private-'+mode)
                    name = mode+'-seed'
                    source = decoded(self.http(folder, name, self.body(name, prompt,
                        maximum=16, session_id=session, prefix_cache_scope=scope,
                        prefix_cache_write_tokens=8192)))
                    self.free()
                    name = mode+'-shared'
                    child = decoded(self.http(folder, name, self.body(name,
                        prompt+source['text']+turn('What is the audit_code in the '
                          'authoritative shared record? Return only its six digits.'),
                        prefix_cache_scope=scope)))
                    self.free()
                    name = mode+'-private'
                    resume = decoded(self.http(folder, name, self.body(name,
                        prompt+source['text']+turn('Reply with exactly 44.'),
                        session_id=session, prefix_cache_scope=scope)))
                    self.free()
                    if (source['usage'].get('prompt_tokens_details', {}).get('cached_tokens', 0)
                            or child['text'].strip() != '831907'
                            or child['usage']['prompt_tokens_details']['cached_tokens'] != 8192
                            or resume['text'].strip() != '44'
                            or resume['usage']['prompt_tokens_details']['cached_tokens'] <= 8192):
                        raise AssertionError('installed cache/quality contract')
                    save(folder/'health.json', dict(source=source, child=child, resume=resume))
                    passed = True
            finally:
                if server is not None and server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                raw = (folder/'server.log').read_text()
                ends = [r for r in fields(raw, '[q4t][request] ') if r['event']=='end']
                finalized = [json.loads(line.split('] ',1)[1]) for line in raw.splitlines()
                             if line.startswith('[startup-reservation] ') and
                             '"event":"finalized"' in line]
                accepted = (passed and server.returncode == 0 and len(ends) == 3 and
                    all(r['released']=='1' and r['outcome']=='success' for r in ends) and
                    len(finalized) == 1 and finalized[0]['status'] == 'PASS' and
                    finalized[0]['handed_live'] == finalized[0]['unhanded_remaining'] == 0 and
                    'shutdown complete (0 in-flight remaining)' in raw)
                save(folder/'exit.json', dict(passed=accepted,
                    server_exit=server.returncode if server else None, endings=ends,
                    reservation_final=finalized))
                if not accepted:
                    raise RuntimeError('installed health/lifecycle failure')
            print('installed-'+mode+': PASS', flush=True)
        final = nvml_process_query.query(timeout=5)
        save(self.out/'final-gpu.json', final)
        if final['status'] != 'OK' or final['pids']:
            raise RuntimeError('health GPU contexts remain')
        save(self.out/'result.json', dict(passed=True, http=self.http_count,
                                         starts=self.starts, finished=time.time()))


if __name__ == '__main__':
    plan = json.loads(Path(sys.argv[1]).read_text())
    Health(plan).run()
