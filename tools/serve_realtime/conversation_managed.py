"""Real native Chat tools and an interrupted response across Guardian recovery.

This actor never launches/restarts a model, reconnects, or replays a request.
Each post-recovery connection is an explicit separate business operation.
"""
import json
import os
from pathlib import Path
import re
import subprocess

from tools.serve_realtime import managed
from tools.serve_realtime import conversation_observation
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_service.package import read, require, same_json, save, sha

OWNER = ('model_pid', 'model_start_ticks', 'model_generation',
         'controller_pid', 'controller_start_ticks')
QUALITY_POLICY = 'record_exact_source_answer_separately'


def phases(plan):
    if 'prompt_recovery' in plan:
        require(plan['prompt_recovery']['format'] == 'q4t-prompt-recovery-v1',
                'unknown Prompt recovery contract')
        if plan['scenario'] == 'tls_controller':
            return [('controller-before','complete'), ('controller-after','complete')]
    result = managed.recovery_phases(plan)
    if 'prompt_recovery' in plan:
        if plan['scenario'] == 'tls':
            result = [*result, ('tls-controller', 'complete')]
    return result


def owner(value):
    require(all(type(value.get(k)) is int and value[k] > 0 for k in OWNER),
            'conversation managed core identity missing')
    return {k:value[k] for k in OWNER}


def prepare(path, manifest, qualified, package):
    require(path is not None, 'native conversation needs managed verification')
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(managed.ROOT/'.q4t-work'),
            'private qualified conversation plan required')
    registry_path = path.parent/'qualified/registry.json'
    accepted = path.parent/'acceptance.json'
    prior = managed.accepted_source(path, manifest, qualified, package)
    require(evidence.conversation_enabled(qualified['capabilities']),
            'managed conversation requires accepted conversation capability')
    evidence.conversation_business(qualified['business']['conversation'])
    record = managed.ROOT/'docs/STATUS.md'
    require(record.stat().st_size <= 1 << 20, 'bounded actual project record')
    version = re.search(r'默认\s*rev([0-9]+)', record.read_text())
    require(version is not None, 'actual current release record missing')
    files = {str(p):sha(p) for p in (path, registry_path, accepted, record,
        path.parent/'qualified/primary.json', path.parent/'qualified/light.json')}
    return dict(format='q4t-managed-conversation-v2', files=files,
        quality_policy=QUALITY_POLICY,
        source=dict(path='docs/STATUS.md', sha256=sha(record), version='rev'+version[1])), prior['sdk']


def check_session(value, mode, transport, source, quality_policy=None):
    require(quality_policy in (None, QUALITY_POLICY), 'unknown conversation quality policy')
    require(mode in ('complete','hold_stream','hold_complete') and
        transport in ('official-openai-wss','ws-loopback') and
        type(value) is dict and value.get('phase') == 'result' and
        value.get('status') == ('PROTOCOL_PASS' if quality_policy else 'PASS') and
        value.get('mode') == mode and
        value.get('transport') == transport and value.get('automatic_replay') is False and
        type(value.get('connections')) is int and value['connections'] == 1 and
        type(value.get('close_code')) is int and value['close_code'] in
            ([1000] if mode == 'complete' else [1001, 1006, 1011, 1012]) and
        type(value.get('response_creates')) is int and
        value['response_creates'] == (3 if mode == 'hold_stream' else 2),
        'actual managed conversation connection/terminal differs')
    partial = value['partial']
    if mode == 'hold_stream':
        require(type(partial) is dict and partial.keys() ==
            {'response_id','item_id','delta'} and
            all(type(v) is str and v for v in partial.values()),
            'actual interrupted model delta missing')
        matches = [e for e in value['events'] if e.get('type') == 'response.output_text.delta'
                   and e.get('response_id') == partial['response_id']]
        require(matches and all(e.get('item_id') == partial['item_id'] for e in matches) and
            matches[0].get('delta') == partial['delta'] and
            not any(e.get('type') == 'response.done' and
                    e.get('response',{}).get('id') == partial['response_id']
                    for e in value['events']),
            'interrupted model was completed or changed identity')
        pending = partial['response_id']
        created = [e.get('response') for e in value['events'] if
                   e.get('type') == 'response.created' and
                   e.get('response',{}).get('id') == pending]
        require(len(created) == 1 and not created[0].get('conversation_id'),
                'interrupted independent response entered default history')
    else:
        require(partial is None, 'unexpected incomplete managed response')
        pending = None
    if quality_policy:
        observed = conversation_observation.observe(source, value, pending)
        require(value.get('quality_policy') == quality_policy and
            value.get('model_quality') == observed['model_quality'],
            'model answer quality receipt differs from actual wire')
        total = observed['total_tokens']
    else:
        total = evidence.conversation_output(source, value, pending)
    return total


def quality_policy(plan):
    fixed = plan['conversation']
    if fixed['format'] == 'q4t-managed-conversation-v1':
        require('quality_policy' not in fixed, 'old strict plan cannot change policy')
        return None
    require(fixed['format'] == 'q4t-managed-conversation-v2' and
        fixed.get('quality_policy') == QUALITY_POLICY, 'managed conversation source contract')
    return QUALITY_POLICY


def accounting(before, after, total, interrupted=False):
    managed.state_shape(before); managed.state_shape(after)
    count = 3 if interrupted else 2
    require(after['admitted'] == before['admitted']+count and
        after['total'] == before['total']+count and
        after['success'] == before['success']+2 and
        after['aborted'] == before['aborted']+int(interrupted) and
        after['errors'] == before['errors'] and
        (after['settled'] > before['settled']+total if interrupted else
         after['settled'] == before['settled']+total),
        'actual conversation admission/settlement or replay differs')
    return after['settled']-before['settled']


def check(out, plan, identity, capabilities):
    del identity  # Native text Chat deliberately has no ASR dependency.
    enabled = evidence.conversation_enabled(capabilities)
    require(('conversation' in plan) is enabled,
            'declared managed conversation capability omitted')
    if not enabled: return {}
    policy = quality_policy(plan)
    out = Path(out); result = {}; identifiers = []
    transport = 'official-openai-wss' if plan['scenario'].startswith('tls') else 'ws-loopback'
    for label,mode in phases(plan):
        prefix = label+'-conversation'
        row = read(out/(prefix+'-receipt.json'))
        raw = read(out/(prefix+'.sdk.json'))
        total = check_session(raw,mode,transport,plan['conversation']['source'],policy)
        require(row['mode'] == mode and managed.clean(row['before']),
                'initial managed conversation resources leaked')
        for suffix in ('before','after') + (() if mode == 'complete' else ('held',)):
            if suffix == 'after' and mode == 'hold_complete':
                require(row['after'] is None, 'no counter subtraction across restart')
                continue
            samples = read(out/(prefix+'-'+suffix+'-metrics.json'))
            require(type(samples) is list and 0 < len(samples) <= 128 and
                all(v['http_status'] == 200 for v in samples) and
                same_json(managed.state(samples[-1]['body']),row[suffix]),
                'raw managed conversation metrics differ')
        if mode != 'complete':
            ready = read(out/(prefix+'-ready.json')); held = row['held']
            managed.state_shape(held)
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode and
                ready.get('transport') == transport and ready.get('connections') == 1 and
                ready.get('response_creates') == raw['response_creates'] and
                ready.get('partial') == raw['partial'] and
                ready.get('completed_ids') == [v['id'] for v in raw['responses']] and
                held['sessions'] == 1 and held['host_bytes'] > 0 and
                held['success'] == row['before']['success']+2 and
                held['aborted'] == row['before']['aborted'],
                'fault did not interrupt actual owned conversation')
            require(held['active'] == 1 and held['reserved'] > 0 if mode == 'hold_stream'
                else held['active'] == held['reserved'] == 0,
                'fault timing or active conversation reservation differs')
        after = row['held'] if mode == 'hold_complete' else row['after']
        if mode != 'hold_complete':
            require(managed.clean(after), 'interrupted conversation did not drain')
        used = accounting(row['before'],after,total,mode == 'hold_stream')
        first = read(out/(prefix+'-core-before.json'))
        last = read(out/(prefix+'-core-'+('held' if mode == 'hold_complete' else 'after')+'.json'))
        require(first.get('business_ready') is True and owner(first) == owner(last),
                'conversation changed healthy model ownership')
        identifiers += [v['id'] for v in raw['responses']]
        result[prefix] = dict(mode=mode,transport=transport,completed_tokens=total,
            actual_tokens=used,interrupted_usage_from_server=mode == 'hold_stream',
            automatic_replay=False,core_owner=owner(first))
        if policy:
            result[prefix].update(quality_policy=policy,model_quality=raw['model_quality'])
    require(len(identifiers) == len(set(identifiers)), 'managed response identity replay')
    count = len(result)*(1+int('realtime' in plan)+int(plan.get('semantic_vad',False)))
    require(read(out/'counts.json')['counts']['websocket_sessions'] ==
            plan['limits']['websocket_sessions'] == count,
            'managed conversation session budget incomplete')
    return result


class Actor(managed.Actor):
    def client_arguments(self, args):
        return args

    def verify_result(self, value, mode):
        return check_session(value,mode,'official-openai-wss' if
            self.stage.plan['scenario'].startswith('tls') else 'ws-loopback',
            self.stage.plan['conversation']['source'],quality_policy(self.stage.plan))

    def start(self,label):
        expected = phases(self.stage.plan)
        require(self.current is None and len(self.attempted) < len(expected) and
            label == expected[len(self.attempted)][0], 'conversation replay or phase order')
        mode = expected[len(self.attempted)][1]; self.attempted.append(label)
        prefix = label+'-conversation'; self.current = dict(prefix=prefix,mode=mode)
        save(self.stage.output/(prefix+'-core-before.json'),self.stage.sample())
        self.current['before'] = self.observe('before',managed.clean)
        self.stage.budget.reserve('websocket_sessions',prefix)
        for _ in range(3 if mode == 'hold_stream' else 2):
            self.stage.budget.reserve('chats',prefix)
        fixed = self.stage.plan['conversation']; source = fixed['source']
        require(sha(managed.ROOT/source['path']) == source['sha256'],
                'actual managed conversation source changed')
        tls = self.stage.plan['scenario'].startswith('tls')
        args = dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{self.stage.port}/v1",
            api_key=self.stage.keys['alpha'], mode=mode,
            record_file=str(managed.ROOT/source['path']),record_sha256=source['sha256'],
            expected_version=source['version'],
            quality_policy=quality_policy(self.stage.plan),
            ca_file=self.stage.plan['tls_inputs']['ca']['path'] if tls else None)
        env = {k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','HTTP_PROXY','HTTPS_PROXY',
             'ALL_PROXY','http_proxy','https_proxy','all_proxy','NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR'] = self.stage.plan['sdk']['sdk_directory']
        self.stderr = (self.stage.output/(prefix+'.stderr')).open('xb')
        self.process = subprocess.Popen([self.stage.plan['sdk']['node']['path'],
            str(managed.ROOT/'tools/serve_realtime/conversation_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,env=env)
        self.buffer.clear(); self.total_bytes = 0
        self.process.stdin.write(json.dumps(self.client_arguments(args)).encode())
        self.process.stdin.close()
        self.process.stdin = None
        if mode == 'complete':
            self.finish()
        else:
            ready = self.line(120); save(self.stage.output/(prefix+'-ready.json'),ready)
            require(ready.get('phase') == 'ready' and ready.get('mode') == mode,
                    'actual conversation not ready for fault')
            self.current['held'] = self.observe('held',lambda s:
                s['sessions'] == 1 and s['host_bytes'] > 0 and
                (s['active'] == 1 and s['reserved'] > 0 if mode == 'hold_stream' else
                 s['active'] == s['reserved'] == 0 and
                 s['success'] == self.current['before']['success']+2))
            save(self.stage.output/(prefix+'-core-held.json'),self.stage.sample())

    def finish(self):
        require(self.current is not None, 'conversation fault without owned connection')
        prefix = self.current['prefix']; mode = self.current['mode']
        value = self.line(120 if mode == 'complete' else 10)
        save(self.stage.output/(prefix+'.sdk.json'),value)
        self.process.wait(timeout=5); extra = self.process.stdout.read(1)
        require(self.process.returncode == 0 and not self.buffer and not extra,
                'managed conversation failed or repeated receipt')
        self.verify_result(value,mode)
        self.current['after'] = None if mode == 'hold_complete' else self.observe('after',managed.clean)
        if mode != 'hold_complete':
            save(self.stage.output/(prefix+'-core-after.json'),self.stage.sample())
        save(self.stage.output/(prefix+'-receipt.json'),self.current)
        self.process.stdout.close(); self.stderr.close()
        self.process = self.stderr = self.current = None

    def finish_phases(self):
        require(self.current is None and
            self.attempted == [name for name,_ in phases(self.stage.plan)],
            'managed conversation phases incomplete')
