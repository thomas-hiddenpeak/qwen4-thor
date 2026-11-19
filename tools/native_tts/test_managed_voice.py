"""CPU transport doubles only; these records never qualify a real service."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.native_tts import managed_voice as voice
from tools.native_asr import service_acceptance as service
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery.test_voice_profiles import cpu_record
from tools.serve_service.test_voice_package import selected
from tools.serve_service.package import save,sha


class ManagedVoice(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ['Q4T_TEST_OUTPUT_DIR']);root.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(temp.cleanup)
        self.out=Path(temp.name);self.clips={}
        for name in ('consent','sample'):
            p=self.out/(name+'.fixture');p.write_bytes(('SYNTHETIC CPU '+name).encode())
            self.clips[name]=dict(path=str(p),sha256=sha(p))
        self.value=cpu_record();self.configured=selected()

    def stage(self,scenario='tool_version'):
        out=self.out/scenario;out.mkdir(exist_ok=True)
        plan=dict(scenario=scenario,native_voice=contract.tts_binding(self.configured,base=True),
                  voice_clips=copy.deepcopy(self.clips))
        stage=SimpleNamespace(output=out,plan=plan,calls=[],stored={})
        def request(name,body=None,*,path,method,client,expected,model_work,content_type,raw_result):
            stage.calls.append(dict(name=name,path=path,body=body,method=method,
                client=client,model_work=model_work))
            if expected==404:
                value=dict(error=dict(type='invalid_request_error',code='object_not_found'))
            elif name=='voice-consent' or name.endswith('-consent'):
                value=copy.deepcopy(self.value['consent'])
            elif name=='voice-create':value=copy.deepcopy(self.value['voice'])
            elif name in ('voice-revoke','voice-delete'):
                item=self.value['consent' if name=='voice-revoke' else 'voice']
                value=dict(id=item['id'],object=item['object'],deleted=True)
            elif name.endswith(('-stored-chat','-prior-chat')):
                value=copy.deepcopy(stage.stored[path.rsplit('/',1)[1]])
            elif name.endswith('-speech-asr'):value=copy.deepcopy(self.value['speech_transcript'])
            elif name.endswith('-chat-asr'):value=copy.deepcopy(self.value['chat_transcript'])
            elif name.endswith('-chat'):
                value=copy.deepcopy(self.value['chat']);value['id']='chatcmpl-'+name
                value['choices'][0]['message']['audio']['id']='audio_'+name
                stage.stored[value['id']]=copy.deepcopy(value)
            elif name.endswith('-speech'):
                value=(('event: speech.audio.delta\ndata: '+json.dumps(dict(
                    type='speech.audio.delta',audio=self.value['speech']['audio_base64']))+'\n\n')+
                    ('event: speech.audio.done\ndata: '+json.dumps(dict(
                    type='speech.audio.done',usage=self.value['speech']['usage']))+'\n\n')).encode()
            else:raise AssertionError('unexpected CPU call '+name)
            wire=value if type(value) is bytes else json.dumps(value).encode()
            (out/(name+'.wire')).write_bytes(wire);save(out/(name+'-http.json'),dict(status=expected))
            return wire if raw_result else json.loads(wire)
        def configuration(label):
            save(out/(label+'-config-http.json'),dict(http_status=200,body='retained independently'))
            save(out/(label+'-config.wire'),dict(native_tts_limits=self.value['limits'],
                native_tts_base_health=self.value['health']['base'],
                native_voice_health=self.value['health']['condition']))
        stage.request=mock.Mock(side_effect=request);stage.configuration=configuration
        stage.drained=mock.Mock();return stage

    def completed(self,scenario='tool_version'):
        stage=self.stage(scenario);actor=voice.Actor(stage)
        for label in voice.labels(stage.plan):actor.observe(label)
        actor.finish();return stage,actor

    def check(self,stage):return voice.check(stage.output,stage.plan,self.configured)

    def mutate(self,stage,name,change):
        path=stage.output/name;raw=path.read_bytes();value=json.loads(raw);change(value)
        save(path,value)
        try:
            with self.assertRaises((ValueError,RuntimeError,KeyError)):self.check(stage)
        finally:path.write_bytes(raw)

    def test_one_registration_persists_across_entry_and_whole_payload_phases(self):
        for scenario,count in [('tool_retained',8),('tool_version',12),('tls',12)]:
            stage,actor=self.completed(scenario);result=self.check(stage)
            self.assertEqual(actor.count,count)
            self.assertEqual(sum(c['model_work'] for c in stage.calls),count)
            self.assertEqual(sum(c['name']=='voice-consent' for c in stage.calls),1)
            self.assertEqual(sum(c['name']=='voice-create' for c in stage.calls),1)
            self.assertTrue(all(r['voice_id']==self.value['voice']['id'] for r in result.values()))
            self.assertEqual(len(result),len(voice.labels(stage.plan)))
            for call in stage.calls:
                if call['client']=='beta':self.assertEqual(call['method'],'GET')

    def test_partial_or_repeated_phase_cannot_retry_a_mutating_request(self):
        stage=self.stage();actor=voice.Actor(stage)
        stage.request.side_effect=RuntimeError('connection failed after submission')
        with self.assertRaises(RuntimeError):actor.observe('primary')
        self.assertEqual(stage.request.call_count,1)
        with self.assertRaises(RuntimeError):actor.observe('primary')
        with self.assertRaises(RuntimeError):actor.finish()
        self.assertEqual(stage.request.call_count,1)
        stage,actor=self.completed('tls');calls=len(stage.calls)
        with self.assertRaises(RuntimeError):actor.observe('tls-after')
        with self.assertRaises(RuntimeError):actor.finish()
        self.assertEqual(len(stage.calls),calls)

    def test_missing_second_phase_or_changed_http_status_cannot_pass(self):
        stage,_=self.completed()
        self.mutate(stage,'fallback-voice-speech-http.json',lambda v:v.update(status=500))
        self.mutate(stage,'fallback-voice-cross-consent-http.json',lambda v:v.update(status=200))
        (stage.output/'fallback-voice-chat.wire').unlink()
        with self.assertRaises(FileNotFoundError):self.check(stage)

    def test_retained_request_body_prevents_hint_and_owner_substitution(self):
        stage,_=self.completed()
        self.mutate(stage,'fallback-voice-speech-asr-exchange.json',lambda v:v.update(bytes=v['bytes']+1))
        self.mutate(stage,'fallback-voice-chat-exchange.json',lambda v:v.update(client='beta'))
        path=stage.output/'fallback-voice-chat-request.body';body=path.read_bytes()
        changed=json.loads(body);changed['audio']['voice']['id']='voice_other';path.write_text(json.dumps(changed))
        try:
            with self.assertRaises(RuntimeError):self.check(stage)
        finally:path.write_bytes(body)

    def test_complete_audio_usage_content_and_isolated_health_are_required(self):
        stage,_=self.completed()
        cases=[('fallback-voice-chat.wire',lambda v:v['usage'].update(total_tokens=1)),
            ('fallback-voice-chat.wire',lambda v:v['choices'][0].update(finish_reason='length')),
            ('fallback-voice-speech-asr.wire',lambda v:v.update(text='wrong content')),
            ('fallback-voice-chat-asr.wire',lambda v:v.update(text='wrong content')),
            ('fallback-voice-config.wire',lambda v:v['native_tts_limits'].update(max_frames=1)),
            ('fallback-voice-config.wire',lambda v:v['native_tts_base_health'].update(state='failed')),
            ('fallback-voice-config.wire',lambda v:v['native_voice_health'].update(core_readiness_dependency=True)),
            ('fallback-voice-prior-chat.wire',lambda v:v['choices'][0]['message']['audio'].update(id='audio_other')),
            ('fallback-voice-stored-chat.wire',lambda v:v['choices'][0]['message']['audio'].update(data=''))]
        for name,change in cases:self.mutate(stage,name,change)

    def test_unloaded_registration_encoder_only_after_whole_payload_restart(self):
        stage,_=self.completed()
        name='fallback-voice-config.wire'
        path=stage.output/name;value=json.loads(path.read_text())
        unloaded=dict(value['native_voice_health'],state='configured',device_bytes=0,
            load_attempts=0,load_failures=0,compute_failures=0,asset_cleanup_pending=False)
        value['native_voice_health']=unloaded;save(path,value)
        self.check(stage)
        for key in ('device_bytes','load_attempts','load_failures','compute_failures'):
            self.mutate(stage,name,lambda v,k=key:v['native_voice_health'].update({k:1}))
        self.mutate(stage,name,lambda v:v['native_voice_health'].update(asset_cleanup_pending=True))
        self.mutate(stage,name,lambda v:v['native_voice_health'].update(reason='load_failed'))
        self.mutate(stage,'primary-voice-config.wire',lambda v:v.update(native_voice_health=unloaded))
        tls,_=self.completed('tls')
        self.mutate(tls,'tls-after-voice-config.wire',lambda v:v.update(native_voice_health=unloaded))

    def test_synthesis_backend_cannot_be_unloaded_before_actual_output(self):
        stage,_=self.completed()
        self.mutate(stage,'fallback-voice-config.wire',lambda v:
            v['native_tts_base_health'].update(state='configured',device_bytes=0,
                load_attempts=0,load_failures=0,compute_failures=0,asset_cleanup_pending=False))

    def test_revocation_must_retire_original_ids_and_both_generation_routes(self):
        stage,_=self.completed()
        for name in ('voice-revoke.wire','voice-delete.wire'):
            self.mutate(stage,name,lambda v:v.update(id='other'))
            self.mutate(stage,name,lambda v:v.update(deleted=False))
        for name in ('voice-revoked-speech','voice-revoked-chat','voice-revoked-consent'):
            self.mutate(stage,name+'-http.json',lambda v:v.update(status=200))
            self.mutate(stage,name+'.wire',lambda v:v.update(id='successful-object'))

    def test_frozen_service_budget_covers_posts_without_changing_restart_counts(self):
        for scenario,expected in [('tool_retained',14),('tool_version',24),('tls',25)]:
            old=service.limits_for(scenario,True,True)
            new=service.limits_for(scenario,True,True,native_voice=True)
            self.assertEqual(new['chats'],expected)
            self.assertEqual(new['starts'],old['starts']);self.assertEqual(new['probes'],old['probes'])
            plan=dict(scenario=scenario,chat_audio=True,native_tts={},native_voice={},limits=new)
            with mock.patch.dict(service.GLOBALS,{},clear=False):
                service.configure_plan_limits(plan)
                with self.assertRaises(RuntimeError):service.configure_plan_limits(dict(plan,limits=old))
        with self.assertRaises(RuntimeError):
            service.limits_for('tls_remaining',False,True,True,native_voice=True)

    def test_package_binding_and_changed_recording_fail_before_service_acceptance(self):
        stage,_=self.completed()
        self.mutate(stage,'voice-consent.wire',lambda v:v.update(language='zh'))
        self.mutate(stage,'voice-create.wire',lambda v:v.update(name='other'))
        missing=copy.deepcopy(stage.plan);missing.pop('native_voice')
        with self.assertRaises(RuntimeError):voice.check(stage.output,missing,self.configured)
        wrong=dict(self.configured,limits=dict(self.configured['limits'],max_frames=200))
        with self.assertRaises(RuntimeError):voice.check(stage.output,stage.plan,wrong)
        self.assertEqual(voice.check(stage.output,{},None),{})
        path=Path(self.clips['consent']['path']);path.write_bytes(b'changed recording')
        fresh=self.stage('tls');actor=voice.Actor(fresh)
        with self.assertRaises(RuntimeError):actor.observe('tls-before')
        self.assertEqual(fresh.request.call_count,0)


class SealedVoiceImports(unittest.TestCase):
    def test_acceptance_helper_keeps_all_production_imports_in_sealed_source(self):
        from tools.serve_recovery import bundle
        root = Path(__file__).resolve().parents[2]
        output = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            source = Path(temporary)/'source'
            for name in bundle.SOURCES:
                target = source/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root/name, target)
            # Reproduce the real package: profile_voice is not a runtime file.
            self.assertFalse((source/'tools/serve_recovery/profile_voice.py').exists())
            code = """
import pathlib,runpy,sys
root,source=map(pathlib.Path,sys.argv[1:])
sys.path.insert(0,str(root))
acceptance=runpy.run_path(str(root/'tools/serve_recovery/acceptance.py'))
acceptance['pin_packaged_modules'](source)
try:
    from tools.serve_recovery import profile_voice
except ImportError:
    pass
else:
    raise AssertionError('unexpected helper inside sealed namespace')
from tools.native_tts import managed_voice
from tools.serve_realtime import managed as managed_realtime
assert pathlib.Path(managed_voice.profile_voice.__file__)==root/'tools/serve_recovery/profile_voice.py'
body,mime=managed_voice.profile_voice.multipart({'name':'CPU'},'recording',b'CPU')
assert b'CPU' in body and 'multipart/form-data' in mime
prefixes=('tools.serve_recovery','tools.serve_service','tools.serve_gateway','tools.serve_ops','tools.serve_host')
observed=[]
for name,module in sys.modules.items():
    if any(name==p or name.startswith(p+'.') for p in prefixes):
        path=getattr(module,'__file__',None)
        if path:
            assert pathlib.Path(path).resolve().is_relative_to(source), (name,path)
            observed.append(name)
assert 'tools.serve_recovery.profile_evidence' in observed
assert 'tools.serve_service.package' in observed
print('PASS_SEALED_PRODUCTION_AND_EXPLICIT_ACCEPTANCE_HELPER')
"""
            child = subprocess.run([sys.executable,'-I','-B','-c',code,str(root),str(source)],
                text=True,capture_output=True,timeout=30,
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=''))
            self.assertEqual(child.returncode,0,child.stdout+child.stderr)
            self.assertIn('PASS_SEALED_PRODUCTION_AND_EXPLICIT_ACCEPTANCE_HELPER',child.stdout)


if __name__=='__main__':unittest.main()
