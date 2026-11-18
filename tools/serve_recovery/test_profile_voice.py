"""CPU wire/driver faults only; fake responses never qualify real voice service."""
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import wave

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_voice as voice
from tools.serve_recovery.test_voice_profiles import cpu_record
from tools.serve_service import capacity, package
from tools.serve_service.test_voice_package import registration_asr, selected

ROOT = Path(__file__).resolve().parents[2]


class VoiceDriver(unittest.TestCase):
    def setUp(self):
        work = ROOT/'.q4t-work/voice-profile-cpu';work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup);self.root = Path(self.temp.name)
        self.clips = {}
        for i,name in enumerate(voice.NAMES):
            p = self.root/(name+'.wav')
            with wave.open(str(p), 'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
                wav.writeframes(bytes([i,0])*160)
            self.clips[name] = dict(path=str(p), sha256=package.sha(p))
        self.fixture = self.root/'inputs.json'
        package.save(self.fixture, dict(format=voice.FORMAT, clips=self.clips))
        self.profile = capacity.normalize(dict(version=1, max_queued_requests=3))
        self.identity = dict(native_tts_base=contract.tts_binding(selected(), base=True),
            native_asr=contract.asr_binding(registration_asr(), voice_registration=True))

    def stage(self):
        out = self.root/'wire';out.mkdir()
        stage = SimpleNamespace(plan=dict(voice_clips=self.clips, identity=self.identity, port=1),
            out=out, profile_name='primary', counts=dict(chats=0, controls=0), drains=[])
        def budget(kind):stage.counts[kind] += 1
        stage.budget=budget;stage.same_model=mock.Mock();stage.drained=stage.drains.append
        value=cpu_record()
        config=dict(native_tts_limits=value['limits'],
            native_tts_base_health=value['health']['base'],
            native_voice_health=value['health']['condition'])
        stage.http=mock.Mock(return_value=(200,json.dumps(config).encode()))
        return stage

    def test_fixture_hashes_distinctness_full_pcm_and_bounds_are_checked(self):
        self.assertEqual(voice.fixtures(self.fixture), self.clips)
        wrong=copy.deepcopy(self.clips);wrong['different']=dict(wrong['sample'])
        package.save(self.fixture,dict(format=voice.FORMAT,clips=wrong))
        with self.assertRaisesRegex(RuntimeError,'alias'):voice.fixtures(self.fixture)
        wrong=copy.deepcopy(self.clips);wrong['consent']['sha256']='0'*64
        package.save(self.fixture,dict(format=voice.FORMAT,clips=wrong))
        with self.assertRaisesRegex(RuntimeError,'identity'):voice.fixtures(self.fixture)
        p=Path(self.clips['sample']['path']);raw=p.read_bytes();p.write_bytes(raw[:-2])
        wrong=copy.deepcopy(self.clips);wrong['sample']['sha256']=package.sha(p)
        package.save(self.fixture,dict(format=voice.FORMAT,clips=wrong))
        with self.assertRaisesRegex(RuntimeError,'WAV'):voice.fixtures(self.fixture)

    def test_policy_preflight_requires_three_clients_without_changing_profile(self):
        initial=driver.Stage.client_policy_base();before=copy.deepcopy(initial)
        value=voice.client_policy(initial,self.profile,selected())
        self.assertEqual(initial,before)
        self.assertEqual([r['id'] for r in value['clients']],
                         [initial['clients'][0]['id'],'voice-other','voice-tiny'])
        self.assertEqual(value['clients'][-1]['tokens_per_minute'],64)
        self.assertEqual(value['clients'][0]['max_output_tokens'],2817)
        capacity.check_clients(self.profile,value)
        with self.assertRaisesRegex(RuntimeError,'global capacity'):
            voice.client_policy(initial,dict(self.profile,max_queued_requests=2),selected())

    def test_registration_budget_and_both_commands_keep_actual_base_dependencies(self):
        asr=registration_asr();base=selected()
        plain=driver.limits_for(self.profile,asr)
        scoped=driver.limits_for(self.profile,asr,native_tts_base=base)
        self.assertEqual(scoped['chats']-plain['chats'],24)
        for invalid in (None,dict(asr,speaker=None)):
            with self.assertRaises(RuntimeError):
                driver.limits_for(self.profile,invalid,native_tts_base=base)
        settings=package.settings(8080,0,capacity_profile=self.profile,
                                  native_asr=asr,native_tts_base=base)
        plan=dict(base=dict(model_dir='/model',settings=settings),
            profiles=contract.profiles(self.profile),binary='/candidate',port=18090,operations_port=18091)
        original=copy.deepcopy(plan)
        for name in contract.NAMES:
            cmd=driver.command(plan,name,self.root)
            self.assertIn('--tts-base-model-dir',cmd);self.assertIn('--speaker-model-dir',cmd)
            self.assertNotIn('--tts-model-dir',cmd);self.assertNotIn('--aligner-model-dir',cmd)
            self.assertEqual(cmd.count('--tts-max-frames'),1)
        self.assertEqual(plan,original)

    def test_cli_accepts_explicit_base_and_pinned_fixture_configuration(self):
        with mock.patch.object(driver,'prepare') as prepare, mock.patch('sys.argv',[
            'profile_acceptance.py','prepare','--binary','/candidate','--package','/bundle',
            '--output',str(self.root/'out'),'--tts-base-config','/base.json',
            '--voice-fixtures',str(self.fixture)]):
            driver.main()
        args=prepare.call_args.args[0]
        self.assertEqual(args.tts_base_config,Path('/base.json'))
        self.assertEqual(args.voice_fixtures,self.fixture)

    def wire_script(self):
        value=cpu_record();cons=value['consent']['id'];vid=value['voice']['id']
        primary=driver.encrypted.CLIENT
        def error(key):
            row=value['negative'][key];return row['status'],json.dumps(row['body']).encode()
        def ok(body):return 200,json.dumps(body).encode()
        speech=(('event: speech.audio.delta\ndata: '+json.dumps(dict(type='speech.audio.delta',
            audio=value['speech']['audio_base64']))+'\n\n')+
            ('event: speech.audio.done\ndata: '+json.dumps(dict(type='speech.audio.done',
                usage=value['speech']['usage']))+'\n\n')).encode()
        return value,[
            ('POST',voice.CONSENTS,primary,*error('negated_consent')),
            ('POST',voice.CONSENTS,primary,*ok(value['consent'])),
            ('GET',voice.CONSENTS+'/'+cons,'voice-other',*error('cross_consent')),
            ('POST',voice.VOICES,'voice-other',*error('cross_voice')),
            ('POST',voice.VOICES,primary,*error('different_speaker')),
            ('POST',voice.VOICES,primary,*ok(value['voice'])),
            ('POST',voice.CONSENTS,'voice-tiny',*error('quota')),
            ('POST','/v1/audio/speech','voice-other',*error('cross_speech')),
            ('POST','/v1/audio/speech',primary,200,speech),
            ('POST','/v1/audio/transcriptions',primary,*ok(value['speech_transcript'])),
            ('POST','/v1/chat/completions',primary,*ok(value['chat'])),
            ('GET','/v1/chat/completions/'+value['chat']['id'],primary,*ok(value['chat'])),
            ('POST','/v1/audio/transcriptions',primary,*ok(value['chat_transcript'])),
            ('DELETE',voice.CONSENTS+'/'+cons,primary,*ok(value['revocation']['consent_deleted'])),
            ('POST','/v1/audio/speech',primary,404,json.dumps(value['revocation']['speech']['body']).encode()),
            ('GET',voice.CONSENTS+'/'+cons,primary,404,json.dumps(value['revocation']['consent']['body']).encode()),
            ('DELETE',voice.VOICES+'/'+vid,primary,*ok(value['revocation']['voice_deleted']))]

    def connections(self, script, seen):
        test=self
        class Connection:
            def __init__(self,*args,**kwargs):self.row=None
            def request(self,method,path,body,headers):
                row=script[len(seen)];seen.append((method,path,body,headers))
                test.assertEqual((method,path,headers['X-Q4T-Client-Id']),row[:3])
                test.assertEqual(headers['X-Q4T-Gateway-Token'],driver.encrypted.HEADERS['X-Q4T-Gateway-Token'])
                self.row=row
            def getresponse(self):
                return SimpleNamespace(status=self.row[3],getheaders=lambda:[],
                    read=lambda limit:self.row[4][:limit])
            def close(self):pass
        return Connection

    def test_complete_wire_lifecycle_uses_real_response_ids_and_unhinted_asr(self):
        expected,script=self.wire_script();stage=self.stage();seen=[]
        with mock.patch.object(voice.http.client,'HTTPConnection',self.connections(script,seen)):
            result=voice.Lifecycle(stage).run()
        self.assertEqual(result,expected)
        self.assertEqual(stage.counts,dict(chats=12,controls=5))
        self.assertEqual(len(seen),len(script));self.assertEqual(len(stage.drains),len(script))
        for method,path,body,headers in seen:
            if path=='/v1/audio/transcriptions':
                for hint in ('prompt','language','text'):
                    self.assertNotIn(('name="'+hint+'"').encode(),body)
            if method=='POST' and path==voice.VOICES:
                self.assertIn(expected['consent']['id'].encode(),body)
            if path=='/v1/audio/speech':
                self.assertEqual(json.loads(body)['voice'],dict(id=expected['voice']['id']))

    def test_false_negative_success_bad_asr_and_missing_history_stop_qualification(self):
        for index,changed in ((0,(200,b'{"id":"unexpected"}')),
                              (9,(200,b'{"text":"Wrong sentence."}')),
                              (11,(404,b'{"error":{"code":"object_not_found"}}'))):
            _,script=self.wire_script();row=script[index];script[index]=(*row[:3],*changed)
            stage=self.stage();seen=[]
            with self.subTest(index=index),mock.patch.object(voice.http.client,'HTTPConnection',self.connections(script,seen)),self.assertRaises((ValueError,RuntimeError)):
                voice.Lifecycle(stage).run()
            # Each test uses a fresh private run directory; retain no fixture
            # outputs as qualification evidence.
            import shutil
            shutil.rmtree(stage.out)

    def test_created_ids_are_valid_before_followup_and_private_fields_fail(self):
        value=cpu_record()['consent']
        self.assertEqual(voice.Lifecycle.created((200,json.dumps(value).encode()),
                                                'voice_consent'),value)
        for change in (dict(id='cons_'),dict(id='cons_a?client=other'),
                       dict(id='voice_wrong'),dict(identity=[0]*192)):
            with self.subTest(change=change),self.assertRaises((ValueError,RuntimeError)):
                voice.Lifecycle.created((200,json.dumps(dict(value,**change)).encode()),
                                        'voice_consent')

    def test_request_bound_rejects_before_network(self):
        stage=self.stage();lifecycle=voice.Lifecycle(stage)
        lifecycle.requests=voice.POSTS_PER_PROFILE
        with mock.patch.object(voice.http.client,'HTTPConnection') as connection:
            with self.assertRaisesRegex(RuntimeError,'request bound'):
                lifecycle.http('overflow','/v1/audio/speech',{})
        connection.assert_not_called()


if __name__=='__main__':unittest.main()
