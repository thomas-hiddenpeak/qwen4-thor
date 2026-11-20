"""MCP recovery actor: owned connections, real peer reads and exact accounting."""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_realtime import managed, mcp_checks, mcp_recovery_checks as c
from tools.serve_realtime.conversation_managed import owner
from tools.serve_service.package import read, require, same_json, save

FORMAT = 'q4t-native-realtime-mcp-recovery-v1'


def phases(plan):
    fixed = plan['mcp_recovery']
    require(fixed['format'] == FORMAT and type(fixed['default_policy']) is bool,
            'unknown MCP recovery contract')
    if fixed['default_policy']:
        require(plan['scenario'] != 'tls', 'private TLS MCP scenario needs explicit policy')
        return {'tool_retained': [('primary', 'policy')],
                'tool_version': [('primary', 'hold_policy'), ('fallback', 'policy')]}[plan['scenario']]
    return {'tool_retained': [('primary', 'complete')],
            'tls': [('tls-before', 'hold_approval'), ('tls-after', 'complete'),
                    ('tls-controller', 'complete')],
            'tool_version': [('primary', 'hold_complete'), ('fallback', 'complete')]}[plan['scenario']]


def journal(out, prefix):
    path = Path(out)/(prefix+'.events.jsonl')
    require(0 < path.stat().st_size <= 8 << 20, 'MCP event journal byte bound')
    return [json.loads(line) for line in path.read_text().splitlines()]


def check(out, plan):
    out = Path(out)
    result, identifiers, history = {}, [], []
    source = plan['mcp_recovery']['source']
    transport = 'official-openai-wss' if plan['scenario'] == 'tls' else 'ws-loopback'
    previous = None
    for label, mode in phases(plan):
        prefix = label+'-mcp'
        receipt = read(out/(prefix+'-receipt.json'))
        raw = read(out/(prefix+'.sdk.json'))
        observed = c.check_session(raw, mode, transport, journal(out, prefix), source, history)
        require(receipt['mode'] == mode and managed.clean(receipt['before']) and
            receipt.get('observed') == observed and receipt.get('verification_failure') is None,
            'MCP original receipt differs or has a failure')
        if label == 'tls-after':
            require(same_json(receipt['before'], previous), 'gateway recovery changed model counters')
        replacement = mode in ('hold_complete', 'hold_policy')
        for suffix in ('before', 'after') + (() if mode in ('complete', 'policy') else ('held',)):
            if suffix == 'after' and replacement:
                require(receipt['after'] is None, 'cannot subtract counters across model replacement')
                continue
            samples = read(out/(prefix+'-'+suffix+'-metrics.json'))
            require(0 < len(samples) <= 128 and all(r['http_status'] == 200 for r in samples) and
                same_json(managed.state(samples[-1]['body']), receipt[suffix]),
                'actual MCP metrics differ')
        if mode not in ('complete', 'policy'):
            ready = read(out/(prefix+'-ready.json'))
            held = receipt['held']
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode and
                ready.get('transport') == transport and ready.get('connections') == 1 and
                ready.get('approval') == raw['approval'] and ready.get('response_creates') == c.work(mode) and
                ready.get('completed_ids') == [r['id'] for r in raw['responses']] and
                ready.get('tool_ids') == [t['id'] for t in raw['tools']] and
                held['sessions'] == 1 and held['host_bytes'] > 0 and
                held['active'] == held['reserved'] == 0,
                'fault did not own actual settled MCP work')
        after = receipt['held'] if replacement else receipt['after']
        if not replacement:
            require(managed.clean(after), 'MCP resources not drained')
        used = c.accounting(receipt['before'], after, observed)
        first = read(out/(prefix+'-core-before.json'))
        last = read(out/(prefix+'-core-'+('held' if replacement else 'after')+'.json'))
        require(first['business_ready'] is True and owner(first) == owner(last),
                'MCP work changed healthy model ownership')
        c.peer(read(out/(prefix+'-peer-before.json')),
               read(out/(prefix+'-peer-after.json')), observed, source)
        identifiers += observed['ids']
        if raw['tools']:
            history = [raw['tools'][-1]]
        previous = receipt['after']
        result[prefix] = dict(mode=mode, transport=transport, actual_tokens=used,
            core_owner=owner(first), **observed)
    require(len(identifiers) == len(set(identifiers)) and
        read(out/'counts.json')['counts']['websocket_sessions'] ==
        plan['limits']['websocket_sessions'] == len(result), 'connection replay/count differs')
    snapshot = read(out/'mcp-peer-final.json')
    total = sum(r['actual_reads'] for r in result.values())
    require(len(snapshot['calls']) == snapshot['methods'].count('tools/call') == total and
        all(r['source'] == source for r in snapshot['calls']), 'late/cleanup tool replay')
    return result


class Actor(managed.Actor):
    def __init__(self, stage):
        super().__init__(stage)
        self.history = []

    def start(self, label):
        expected = phases(self.stage.plan)
        require(self.current is None and len(self.attempted) < len(expected) and
            label == expected[len(self.attempted)][0], 'MCP phase order or replay')
        mode = expected[len(self.attempted)][1]
        self.attempted.append(label)
        prefix = label+'-mcp'
        self.current = dict(prefix=prefix, mode=mode)
        save(self.stage.output/(prefix+'-core-before.json'), self.stage.sample())
        save(self.stage.output/(prefix+'-peer-before.json'), self.stage.peer.snapshot())
        self.current['before'] = self.observe('before', managed.clean)
        self.stage.budget.reserve('websocket_sessions', prefix)
        for _ in range(c.work(mode)):
            self.stage.budget.reserve('chats', prefix)
        tls = self.stage.plan['scenario'] == 'tls'
        args = dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{self.stage.port}/v1",
            api_key=self.stage.keys['alpha'], mode=mode, history=self.history,
            mcp_url=self.stage.peer.url, mcp_token=self.stage.peer.secret, arguments=mcp_checks.ARGUMENTS,
            journal_file=str(self.stage.output/(prefix+'.events.jsonl')),
            ca_file=self.stage.plan['tls_inputs']['ca']['path'] if tls else None)
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','HTTP_PROXY','HTTPS_PROXY',
             'ALL_PROXY','http_proxy','https_proxy','all_proxy','NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR'] = self.stage.plan['sdk']['sdk_directory']
        self.stderr = (self.stage.output/(prefix+'.stderr')).open('xb')
        self.process = subprocess.Popen([self.stage.plan['sdk']['node']['path'],
            str(managed.ROOT/'tools/serve_realtime/mcp_recovery_sdk.mjs')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        self.buffer.clear()
        self.total_bytes = 0
        self.process.stdin.write(json.dumps(args).encode())
        self.process.stdin.close()
        self.process.stdin = None
        if mode in ('complete', 'policy'):
            self.finish()
        else:
            ready = self.line(120)
            save(self.stage.output/(prefix+'-ready.json'), ready)
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode,
                    'real MCP task not ready for fault')
            self.current['held'] = self.observe('held', lambda s:s['sessions'] == 1 and
                s['host_bytes'] > 0 and s['active'] == s['reserved'] == 0 and
                s['success'] == self.current['before']['success'] + c.work(mode))
            save(self.stage.output/(prefix+'-core-held.json'), self.stage.sample())

    def finish(self):
        require(self.current is not None, 'fault without owned MCP connection')
        prefix, mode = self.current['prefix'], self.current['mode']
        value = self.line(120 if mode in ('complete', 'policy') else 10)
        save(self.stage.output/(prefix+'.sdk.json'), value)
        self.process.wait(timeout=5)
        extra = self.process.stdout.read(1)
        failure = None
        try:
            require(self.process.returncode == 0 and not self.buffer and not extra,
                    'MCP SDK failed/repeated receipt')
            self.current['observed'] = c.check_session(value, mode,
                'official-openai-wss' if self.stage.plan['scenario'] == 'tls' else 'ws-loopback',
                journal(self.stage.output, prefix), self.stage.plan['mcp_recovery']['source'], self.history)
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            failure = error
            self.current['verification_failure'] = str(error)
        try:
            replacement = mode in ('hold_complete', 'hold_policy')
            self.current['after'] = None if replacement else self.observe('after', managed.clean)
            if not replacement:
                save(self.stage.output/(prefix+'-core-after.json'), self.stage.sample())
        finally:
            save(self.stage.output/(prefix+'-peer-after.json'), self.stage.peer.snapshot())
            save(self.stage.output/(prefix+'-receipt.json'), self.current)
            self.process.stdout.close()
            self.stderr.close()
            self.process = self.stderr = self.current = None
        if failure is not None:
            raise failure
        if value['tools']:
            self.history = [value['tools'][-1]]

    def finish_phases(self):
        require(self.current is None and self.attempted == [n for n,_ in phases(self.stage.plan)],
                'MCP recovery phases incomplete')
