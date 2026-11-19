"""Synthetic negatives for managed semantic receipts; no model execution."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_realtime import semantic_managed as m
from tools.serve_realtime.test_managed import state, wire
from tools.serve_recovery.test_profile_semantic import record, identity, declaration
from tools.serve_recovery.test_profile_realtime import declaration as realtime_declaration
from tools.native_asr import service_acceptance as service
from tools.serve_service.package import read, save


def core():
    return dict(business_ready=True,model_pid=12,model_start_ticks=30,
                model_generation=1,controller_pid=10,controller_start_ticks=20)


class SemanticManaged(unittest.TestCase):
    def setUp(self):
        parent=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',m.managed.ROOT/'.q4t-work/semantic-managed-cpu'))
        parent.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(temporary.cleanup)
        self.out=Path(temporary.name)

    def fixture(self,scenario='tls'):
        self.identity=identity();self.identity['native_asr']['vad']['limits']=dict(max_chunks=64)
        self.caps=dict(realtime=dict(realtime_declaration(),semantic_vad=declaration()),
                       structured_output=dict(available=True))
        count=1 if scenario=='tool_retained' else 2
        self.plan=dict(scenario=scenario,semantic_vad=True,limits=dict(websocket_sessions=2*count),
            realtime=dict(clips=dict(zh=dict(sha256=m.evidence.REALTIME_INPUT_SHA))))
        for i,(label,_) in enumerate(m.managed.phases(self.plan)):
            prefix=label+'-semantic';fixture=record(self.identity);raw=fixture['turns'][0]
            raw['transport']='openai-realtime-wss' if scenario=='tls' else 'ws-loopback'
            new='semantic-'+str(i)
            raw['terminal']['item_id']=new;raw['retrieved']['item']['id']=new
            for event in raw['events']:
                if 'item_id' in event:event['item_id']=new
            settings=m.evidence.semantic_settings()
            settings['audio']['input']['transcription'].update(language='',prompt='',languages=[],keywords=[])
            raw['events'][:0]=[dict(type='session.created',session=dict(id='session-'+str(i))),
                              dict(type='session.updated',session=settings)]
            before=state();after=state(admitted=1,total=1,success=1,settled=195)
            config=dict(native_asr_health=fixture['asr_health'],native_vad_health=fixture['vad_health'],
                        native_asr_limits=fixture['asr_limits'],native_vad_max_chunks=64)
            row=dict(turn=m.semantic.compact(raw,False),before=before,after=after,
                input_sha256=m.evidence.REALTIME_INPUT_SHA,settings=m.evidence.semantic_settings(),
                health=m.health(config,self.identity),core_owner=m.owner(core()))
            save(self.out/(prefix+'.sdk.json'),raw);save(self.out/(prefix+'-receipt.json'),row)
            save(self.out/(prefix+'-health.json'),dict(http_status=200,body=json.dumps(config)))
            for suffix,value in (('before',before),('after',after)):
                save(self.out/(prefix+'-'+suffix+'-metrics.json'),[dict(http_status=200,body=wire(value))])
                save(self.out/(prefix+'-core-'+suffix+'.json'),core())
        save(self.out/'counts.json',dict(counts=dict(websocket_sessions=count*2)))

    def check(self):return m.check(self.out,self.plan,self.identity,self.caps)

    def rejects(self,name,change):
        path=self.out/name;original=path.read_bytes();value=read(path);change(value);save(path,value)
        try:
            with self.assertRaises((RuntimeError,ValueError,KeyError)):self.check()
        finally:path.write_bytes(original)

    def test_all_recovery_phases_require_real_semantics_and_metered_work(self):
        for scenario,count in (('tool_retained',1),('tls',2),('tool_version',2)):
            self.fixture(scenario);result=self.check()
            self.assertEqual(len(result),count)
            self.assertTrue(all(v['actual_classifier'] and v['actual_tokens']==195 for v in result.values()))

    def test_declared_capability_or_missing_real_phase_cannot_be_skipped(self):
        self.assertEqual(m.check(self.out,{}, {},{}),{})
        self.fixture();self.plan.pop('semantic_vad')
        with self.assertRaises(RuntimeError):self.check()
        self.fixture();self.caps['realtime'].pop('semantic_vad')
        with self.assertRaises(RuntimeError):self.check()
        self.fixture();(self.out/'tls-after-semantic.sdk.json').unlink()
        with self.assertRaises(FileNotFoundError):self.check()

    def test_raw_audio_classifier_and_receipt_cannot_be_fabricated(self):
        self.fixture();prefix='tls-before-semantic'
        self.rejects(prefix+'-receipt.json',lambda v:v['turn']['terminal']['usage'].update(total_tokens=196))
        self.rejects(prefix+'.sdk.json',lambda v:v['terminal']['q4t_semantic_vad'].update(accepted=0))
        self.rejects(prefix+'.sdk.json',lambda v:v['retrieved']['item']['content'][0].update(audio='AAAA'))
        self.rejects(prefix+'-receipt.json',lambda v:v.update(input_sha256='0'*64))

    def test_raw_metrics_require_complete_settlement_and_resource_release(self):
        self.fixture();prefix='tls-before-semantic'
        self.rejects(prefix+'-after-metrics.json',lambda v:v[0].update(http_status=500))
        self.rejects(prefix+'-after-metrics.json',lambda v:v[0].update(body=wire(state())))
        self.rejects(prefix+'-receipt.json',lambda v:v['after'].update(reserved=1))
        self.rejects('counts.json',lambda v:v['counts'].update(websocket_sessions=3))

    def test_actual_backend_health_and_core_owner_must_survive(self):
        self.fixture();prefix='tls-before-semantic'
        self.rejects(prefix+'-health.json',lambda v:v.update(http_status=403))
        self.rejects(prefix+'-health.json',lambda v:v.update(body='{}'))
        self.rejects(prefix+'-core-after.json',lambda v:v.update(model_pid=99))
        self.rejects(prefix+'-core-after.json',lambda v:v.update(business_ready=False))
        config=json.loads(read(self.out/(prefix+'-health.json'))['body'])
        for change in (dict(native_vad_max_chunks=0),dict(native_vad_max_chunks=True)):
            with self.assertRaises(RuntimeError):m.health(dict(config,**change),self.identity)
        config['native_asr_health']['compute_failures']=1
        with self.assertRaises(RuntimeError):m.health(config,self.identity)

    def test_no_transport_downgrade_replay_or_recognition_hints(self):
        self.fixture();prefix='tls-before-semantic'
        for change in (dict(transport='ws-loopback'),dict(automatic_replay=True),dict(connections=2)):
            self.rejects(prefix+'.sdk.json',lambda v,c=change:v.update(c))
        self.rejects(prefix+'.sdk.json',lambda v:v['events'][1]['session']['audio']['input']['transcription'].update(prompt='hint'))

    def test_exact_added_work_budget_and_no_partial_tls_bypass(self):
        for scenario,count in (('tool_retained',1),('tls',2),('tool_version',2)):
            old=service.limits_for(scenario,True,True,native_voice=True,realtime=True)
            new=service.limits_for(scenario,True,True,native_voice=True,realtime=True,semantic_vad=True)
            self.assertEqual(new['starts'],old['starts']);self.assertEqual(new['seconds'],old['seconds'])
            self.assertEqual(new['chats']-old['chats'],count)
            self.assertEqual(new['websocket_sessions']-old['websocket_sessions'],count)
            plan=dict(scenario=scenario,limits=new,chat_audio=True,native_tts={},native_voice={},realtime={},semantic_vad=True)
            service.configure_plan_limits(plan)
            with self.assertRaises(RuntimeError):service.configure_plan_limits(dict(plan,limits=old))
        with self.assertRaises(RuntimeError):service.limits_for('tls',semantic_vad=True)
        with self.assertRaises(RuntimeError):service.limits_for('tls',realtime=True,semantic_vad=1)
        with self.assertRaises(RuntimeError):service.limits_for('tls_remaining',realtime=True,semantic_vad=True)

    def test_actor_rejects_wrong_phase_or_missing_completion(self):
        actor=m.Actor(SimpleNamespace(plan=dict(scenario='tls',semantic_vad=True)))
        with self.assertRaises(RuntimeError):actor.observe('tls-after')
        with self.assertRaises(RuntimeError):actor.finish()
        actor.attempted=['tls-before','tls-after'];actor.finish()
        with self.assertRaises(RuntimeError):actor.observe('tls-after')

    def test_sdk_failure_keeps_wire_without_success_or_retry(self):
        stage=SimpleNamespace(plan=dict(scenario='tool_retained',semantic_vad=True,
            realtime=dict(clips=dict(zh=dict(path='/frozen',sha256=m.evidence.REALTIME_INPUT_SHA))),
            sdk=dict(sdk_directory='/sdk',node=dict(path='/node'))),
            output=self.out,sample=lambda:core(),port=1,keys=dict(alpha='fixture'),
            budget=SimpleNamespace(reserve=mock.Mock()))
        actor=m.Actor(stage)
        with mock.patch.object(m.managed.Actor,'observe',return_value=state()), \
             mock.patch.object(m,'sha',return_value=m.evidence.REALTIME_INPUT_SHA), \
             mock.patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'{"status":"FAIL"}','cause')):
            with self.assertRaisesRegex(RuntimeError,'actual managed semantic SDK failed'):actor.observe('primary')
        self.assertEqual(read(self.out/'primary-semantic.sdk.json'),dict(status='FAIL'))
        self.assertFalse((self.out/'primary-semantic-receipt.json').exists())
        with self.assertRaises(RuntimeError):actor.observe('primary')


if __name__=='__main__':unittest.main()
