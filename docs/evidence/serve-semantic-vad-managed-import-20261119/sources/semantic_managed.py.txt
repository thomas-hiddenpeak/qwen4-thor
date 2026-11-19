"""Actual semantic turns before/after Guardian recovery, without model launch.

Each phase uses one explicit fresh connection and real retained audio. Manual
Realtime separately holds an unfinished connection across the injected fault.
"""
import json
import os
from pathlib import Path
import subprocess

from tools.serve_ops import transport
from tools.serve_realtime import managed
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_realtime import semantic_evidence as semantic
from tools.serve_service.package import read, require, same_json, save, sha

OWNER = ('model_pid', 'model_start_ticks', 'model_generation',
         'controller_pid', 'controller_start_ticks')


def owner(value):
    require(value.get('business_ready') is True and all(
        type(value.get(k)) is int and value[k] > 0 for k in OWNER),
        'semantic managed core identity is not ready')
    return {k:value[k] for k in OWNER}


def health(value, identity):
    projected = semantic.configuration(value)
    for name,fixed in (('asr',identity['native_asr']),('vad',identity['native_asr']['vad'])):
        observed = projected[name+'_health']
        require(observed.get('state') == 'ready' and observed.get('reason') == 'none' and
            observed.get('model') == fixed['model'] and
            observed.get('core_readiness_dependency') is False and
            type(observed.get('compute_failures')) is int and observed['compute_failures'] == 0 and
            same_json(projected[name+'_limits'],fixed['limits']),
            'semantic managed backend health or limits differ')
    return projected


def check(out, plan, identity, capabilities):
    enabled = evidence.semantic_vad_enabled(identity,capabilities)
    require(plan.get('semantic_vad',False) is enabled,
            'declared managed semantic capability omitted')
    if not enabled:return {}
    require('realtime' in plan, 'semantic managed real input missing')
    out=Path(out);results={};identifiers=[]
    expected_transport='openai-realtime-wss' if plan['scenario']=='tls' else 'ws-loopback'
    for label,_ in managed.phases(plan):
        prefix=label+'-semantic'
        row=read(out/(prefix+'-receipt.json'));raw=read(out/(prefix+'.sdk.json'))
        turn=semantic.compact(raw,False)
        require(same_json(row['turn'],turn) and raw.get('transport')==expected_transport and
            row['input_sha256']==plan['realtime']['clips']['zh']['sha256']==evidence.REALTIME_INPUT_SHA and
            same_json(row['settings'],evidence.semantic_settings()),
            'semantic managed raw SDK, transport or input differs')
        # Require actual unhinted effective settings, not only the request copy.
        created=[e for e in raw['events'] if e['type']=='session.created']
        updated=[e for e in raw['events'] if e['type']=='session.updated']
        expected=evidence.semantic_settings()['audio']['input']
        expected['transcription'].update(language='',prompt='',languages=[],keywords=[])
        require(len(created)==len(updated)==1 and
            same_json(updated[0]['session']['audio']['input'],expected),
            'semantic managed session restarted or added transcription hints')
        for suffix in ('before','after'):
            samples=read(out/(prefix+'-'+suffix+'-metrics.json'))
            require(type(samples) is list and 0 < len(samples) <= 128 and
                all(s['http_status']==200 for s in samples) and
                same_json(managed.state(samples[-1]['body']),row[suffix]) and
                managed.clean(row[suffix]), 'semantic raw metrics/resource cleanup differs')
        used=managed.accounting(row['before'],row['after'],tokens=evidence.semantic_turn(turn,False))
        http=read(out/(prefix+'-health.json'))
        require(http['http_status']==200 and
            same_json(health(json.loads(http['body']),identity),row['health']),
            'semantic actual operations health differs')
        first=read(out/(prefix+'-core-before.json'));last=read(out/(prefix+'-core-after.json'))
        require(owner(first)==owner(last)==row['core_owner'],
                'semantic work restarted the healthy core')
        identifiers.append(turn['terminal']['item_id'])
        results[prefix]=dict(transport=expected_transport,actual_tokens=used,
            actual_classifier=True,core_owner=row['core_owner'],resources_released=True,
            automatic_replay=False)
    require(len(set(identifiers))==len(identifiers), 'semantic new connection reused item identity')
    counts=read(out/'counts.json')['counts']
    require(counts['websocket_sessions']==plan['limits']['websocket_sessions']==2*len(results),
            'semantic managed session budget incomplete')
    return results


class Actor:
    def __init__(self,stage):
        self.stage=stage;self.attempted=[]

    def observe(self,label):
        stage=self.stage;expected=[name for name,_ in managed.phases(stage.plan)]
        require(stage.plan.get('semantic_vad') is True and
            len(self.attempted)<len(expected) and label==expected[len(self.attempted)],
            'semantic managed phase replay or order')
        self.attempted.append(label);prefix=label+'-semantic'
        sampler=managed.Actor(stage);sampler.current=dict(prefix=prefix)
        first=stage.sample();save(stage.output/(prefix+'-core-before.json'),first)
        before=sampler.observe('before',managed.clean)
        stage.budget.reserve('websocket_sessions',prefix);stage.budget.reserve('chats',prefix)
        clip=stage.plan['realtime']['clips']['zh']
        require(sha(clip['path'])==clip['sha256']==evidence.REALTIME_INPUT_SHA,
                'semantic managed real input changed')
        tls=stage.plan['scenario']=='tls'
        args=dict(base_url=f"{'https' if tls else 'http'}://127.0.0.1:{stage.port}/v1",
            api_key=stage.keys['alpha'],file=clip['path'],split_samples=0,
            ca_file=stage.plan['tls_inputs']['ca']['path'] if tls else None,
            settings=evidence.semantic_settings())
        env={k:v for k,v in os.environ.items() if k not in
            ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY',
             'http_proxy','https_proxy','all_proxy','NODE_USE_ENV_PROXY')}
        env['Q4T_NODE_SDK_DIR']=stage.plan['sdk']['sdk_directory']
        process=subprocess.run([stage.plan['sdk']['node']['path'],
            str(managed.ROOT/'tools/serve_realtime/semantic_sdk.mjs')],input=json.dumps(args),
            text=True,capture_output=True,env=env,timeout=135)
        (stage.output/(prefix+'.sdk.json')).write_text(process.stdout)
        (stage.output/(prefix+'.stderr')).write_text(process.stderr)
        require(process.returncode==0 and len(process.stdout.encode()) <= 2 << 20,
                'actual managed semantic SDK failed')
        turn=semantic.compact(json.loads(process.stdout),False)
        after=sampler.observe('after',managed.clean)
        stage.budget.reserve('controls',prefix+'-health')
        response=transport.get(stage.policy['upstream_operations_port'],'/v1/config',timeout=2)
        save(stage.output/(prefix+'-health.json'),response)
        require(response['http_status']==200,'semantic managed native health unavailable')
        identity=read(stage.package/'manifest.json')['recovery_profiles']['identity']
        observed=health(json.loads(response['body']),identity)
        last=stage.sample();save(stage.output/(prefix+'-core-after.json'),last)
        require(owner(first)==owner(last),'semantic managed model changed')
        managed.accounting(before,after,tokens=evidence.semantic_turn(turn,False))
        save(stage.output/(prefix+'-receipt.json'),dict(turn=turn,before=before,after=after,
            input_sha256=clip['sha256'],settings=args['settings'],health=observed,
            core_owner=owner(first)))

    def finish(self):
        require(self.attempted==[name for name,_ in managed.phases(self.stage.plan)],
                'semantic managed phases incomplete')
