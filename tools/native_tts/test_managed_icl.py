"""Synthetic CPU persistence/recovery witnesses, never actual voice proof."""
import copy
import json
import unittest

from tools.native_tts import managed_voice as voice
from tools.native_tts import test_managed_voice as legacy
from tools.serve_recovery.test_voice_icl_profiles import record
from tools.serve_service.package import save


class ManagedIcl(unittest.TestCase):
    completed = legacy.ManagedVoice.completed
    check = legacy.ManagedVoice.check
    mutate = legacy.ManagedVoice.mutate

    def setUp(self):
        legacy.ManagedVoice.setUp(self)
        self.value = record()

    def stage(self, scenario='tool_version'):
        stage = legacy.ManagedVoice.stage(self,scenario)
        stage.plan['voice_icl'] = True
        store = stage.output/'state/application-objects'
        store.mkdir(parents=True,exist_ok=True)
        for item in self.value['conditioning'].values():
            save(store/(item['owner']+'.'+item['kind']+'.'+item['id']+'.json'),
                 dict(record=copy.deepcopy(item)))
        return stage

    def test_same_reference_and_full_input_usage_survive_each_recovery_phase(self):
        for scenario in ('tool_retained','tool_version','tls'):
            stage,_ = self.completed(scenario)
            results = self.check(stage)
            self.assertEqual(len({v['reference_sha256'] for v in results.values()}),1)
            for value in results.values():
                self.assertEqual(value['reference_frames'],56)
                self.assertEqual(value['speech_input_tokens'],920)
                self.assertEqual(value['chat_input_tokens'],918)

    def test_changed_persisted_code_rejects_before_next_generation_request(self):
        stage=self.stage();actor=voice.Actor(stage);actor.observe('primary')
        calls=len(stage.calls)
        path=next((stage.output/'state/application-objects').glob('*.voice.*.json'))
        value=json.loads(path.read_text());value['record']['data']['reference']['codes'][0][0]=1
        save(path,value)
        with self.assertRaisesRegex(RuntimeError,'changed during recovery'):
            actor.observe('fallback')
        self.assertEqual(len(stage.calls),calls)

    def test_missing_link_or_wrong_owner_reference_cannot_fall_back_to_vector(self):
        stage=self.stage();store=stage.output/'state/application-objects'
        consent,identifier=self.value['consent']['id'],self.value['voice']['id']
        voice.reference_records(store,consent,identifier)
        path=next(store.glob('*.voice.*.json'));original=path.read_bytes()
        value=json.loads(original);value['record']['owner']='b'*64;save(path,value)
        with self.assertRaises(ValueError):voice.reference_records(store,consent,identifier)
        path.unlink()
        with self.assertRaises(RuntimeError):voice.reference_records(store,consent,identifier)
        held=stage.output/'held.json';held.write_bytes(original);path.symlink_to(held)
        with self.assertRaises(RuntimeError):voice.reference_records(store,consent,identifier)

    def test_recorded_condition_health_and_usage_cannot_be_substituted(self):
        stage,_=self.completed()
        self.mutate(stage,'fallback-voice-conditioning.json',lambda v:
            v['voice']['data']['reference']['codes'][0].__setitem__(0,1))
        for change in (dict(reference_encoder=False),dict(asset_cleanup_pending=True),
                       dict(load_failures=1),dict(model='qwen3-tts-12hz-1.7b-base-speaker')):
            self.mutate(stage,'fallback-voice-config.wire',lambda v,c=change:
                v['native_voice_health'].update(c))
        self.mutate(stage,'fallback-voice-chat.wire',lambda v:
            v['usage']['q4t_audio_synthesis'].update(input_tokens=22))

    def test_fresh_unloaded_reference_only_after_actual_payload_restart(self):
        stage,_=self.completed()
        path=stage.output/'fallback-voice-config.wire';value=json.loads(path.read_text())
        unloaded=dict(value['native_voice_health'],state='configured',device_bytes=0,
            load_attempts=0,load_failures=0,compute_failures=0,asset_cleanup_pending=False)
        value['native_voice_health']=unloaded;save(path,value);self.check(stage)
        self.mutate(stage,'primary-voice-config.wire',lambda v:v.update(native_voice_health=unloaded))
        tls,_=self.completed('tls')
        self.mutate(tls,'tls-after-voice-config.wire',lambda v:v.update(native_voice_health=unloaded))


if __name__ == '__main__':
    unittest.main()
