"""Six installed chat requests, two heads, under one outer GPU lease."""
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import verify as wire


class Health(wire.Run):
    def __init__(self, plan):
        self.plan = plan
        super().__init__(SimpleNamespace(output=Path(plan['output']),
            port=plan['port'], protocol_sha256=plan['protocol_sha256']))
        self.out.mkdir(mode=0o700)

    def budget(self):
        wire.require(time.time() < self.plan['deadline_unix'] - 60,
                     'installed health deadline')
        wire.require(self.requests <= 6 and self.starts <= 2 and
                     self.capability_gets <= 2 and self.agent_runs <= 2 and
                     self.negative == 0, 'installed health budget')

    def wait_slot(self, name):
        deadline = time.monotonic() + 10
        while True:
            self.budget()
            status, raw = self.request('GET', '/healthz')
            state = json.loads(raw)
            wire.require(status == 200 and state['gpu_healthy'], name)
            if state['seq_slots_free'] == 4:
                return
            wire.require(time.monotonic() < deadline, 'slots not released')
            time.sleep(0.05)

    def one_head(self, mode):
        self.starts += 1
        self.budget()
        folder = self.out / mode
        folder.mkdir(mode=0o700)
        argv = [self.plan['candidate'], 'serve', '--model-dir',
            self.plan['model_dir'], '--host', '127.0.0.1', '--port',
            str(self.args.port), '--max-seq', '4', '--max-prefill', '4096',
            '--max-len', '208896', '--seq-capacities',
            '208896,65536,65536,65536']
        if mode == 'subset':
            argv += ['--mtp-subset-head', self.plan['artifact']]
        wire.save(folder / 'command.json', dict(argv=argv,
            binary_sha256=wire.digest(Path(self.plan['candidate']))))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k not in
               ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
        server, passed = None, False
        try:
            with (folder / 'server.log').open('x') as log:
                server = subprocess.Popen(argv, cwd=wire.ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 240
                while 'serving on port' not in (folder / 'server.log').read_text():
                    self.budget()
                    wire.require(server.poll() is None, 'startup failed')
                    wire.require(time.monotonic() < deadline, 'startup timeout')
                    time.sleep(1)
                self.wait_slot(mode)
                # The unchanged demo performs one capabilities GET and exactly
                # two real requests; its journal retains the complete wire.
                self.agent_roundtrip(folder, stream=mode == 'subset', mtp=True)
                thought = self.chat(folder, 'reasoning-sse',
                                    wire.body('thinking', True, True))
                wire.require(thought['finish'] == 'stop' and
                    thought['message']['content'].strip() == '323' and
                    thought['message'].get('reasoning_content'),
                    'installed reasoning quality')
                passed = True
        finally:
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            raw = (folder / 'server.log').read_text()
            ends = [line for line in raw.splitlines()
                    if line.startswith('[q4t][request] ') and 'event=end' in line]
            finalized = [json.loads(line.split('] ', 1)[1])
                for line in raw.splitlines()
                if line.startswith('[startup-reservation] ') and
                '"event":"finalized"' in line]
            clean = (server is not None and server.returncode == 0 and
                len(ends) == 3 and all('released=1' in line and
                'outcome=success' in line for line in ends) and
                len(finalized) == 1 and finalized[0]['status'] == 'PASS' and
                finalized[0]['handed_live'] == 0 and
                finalized[0]['unhanded_remaining'] == 0 and
                finalized[0]['release_unknown'] == 0 and
                'shutdown complete (0 in-flight remaining)' in raw)
            wire.save(folder / 'exit.json', dict(passed=passed and clean,
                server_exit=server.returncode if server else None,
                endings=ends, reservation_final=finalized))
            wire.require(clean, 'installed health cleanup failed')
        print('installed-' + mode + ': PASS', flush=True)

    def run(self):
        status, error = 'FAIL', None
        try:
            for mode in ['full', 'subset']:
                self.one_head(mode)
            wire.require(self.requests == 6 and self.starts == 2 and
                self.capability_gets == 2 and self.agent_runs == 2,
                'installed health coverage')
            self.budget()
            status = 'PASS'
        except BaseException as exc:
            error = repr(exc)
            raise
        finally:
            wire.save(self.out / 'result.json', dict(status=status, error=error,
                requests=self.requests, starts=self.starts,
                capability_gets=self.capability_gets, agent_runs=self.agent_runs,
                elapsed_seconds=time.monotonic() - self.started))
