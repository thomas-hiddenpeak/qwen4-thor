"""CPU fault cases for consumer qualification; fixtures never certify audio."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_acceptance as driver
from tools.serve_recovery import profile_consumer as consumer
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_voice
from tools.serve_recovery.test_voice_profiles import cpu_record
from tools.serve_service import capacity, package
from tools.serve_service.test_voice_package import selected, registration_asr


def record():
    value = cpu_record()
    witness = copy.deepcopy(evidence.VOICE_CONSUMER_WITNESS)
    value['consent'].update(id=witness['consent'], name='local-consent-renamed')
    value['voice']['id'] = witness['voice']
    value['requests'] = evidence.voice_requests(witness['consent'], witness['voice'])
    value['requests'].pop('transcription')
    value.pop('speech_transcript');value.pop('chat_transcript')
    value['witness'] = witness
    value['stored_chat'] = copy.deepcopy(value['chat'])
    def rejected(status, code='object_not_found'):
        return dict(status=status, body=dict(error=dict(code=code)))
    value['negative'] = dict(cross_consent=rejected(404),
        cross_speech=rejected(404), cross_chat=rejected(404),
        new_consent=rejected(503, 'voice_verification_not_configured'),
        new_voice=rejected(503, 'voice_verification_not_configured'))
    value['revocation']['chat'] = rejected(404)
    for key in ('consent', 'voice'):
        value['revocation'][key+'_deleted']['id'] = witness[key]
    unloaded = dict(state='disabled', reason='none', device_bytes=0,
        core_readiness_dependency=False, load_attempts=0, load_failures=0,
        compute_failures=0)
    value['health']['base'].update(load_attempts=1, load_failures=0, device_bytes=4096)
    value['health']['condition'] = dict(unloaded, state='configured',
        model=package.NATIVE_TTS_BASE['model']+'-speaker')
    for key in ('tts', 'asr', 'alignment', 'vad', 'speaker', 'separator'):
        value['health'][key] = copy.deepcopy(unloaded)
    return value


class ConsumerContracts(unittest.TestCase):
    def setUp(self):
        self.value = record()
        raw = base64.b64decode(self.value['speech']['audio_base64'])
        patch = mock.patch.object(evidence, 'VOICE_CONSUMER_AUDIO',
            dict.fromkeys(('speech', 'chat'), (len(raw), hashlib.sha256(raw).hexdigest())))
        patch.start();self.addCleanup(patch.stop)
        self.identity = dict(native_tts_base=contract.tts_binding(selected(), base=True))
        self.profile = capacity.normalize(dict(version=1, max_queued_requests=3))

    def test_complete_consumer_receipt_is_distinct_from_registration(self):
        evidence.voice_consumer_business(self.value, self.identity)
        self.assertEqual(evidence.format_for(self.profile, self.identity), evidence.VOICE_CONSUMER_FORMAT)
        with self.assertRaises((ValueError, RuntimeError)):
            evidence.voice_business(self.value, self.identity)

    def test_registered_profile_budget_cannot_silently_drop_dependencies(self):
        with self.assertRaisesRegex(RuntimeError, 'registration'):
            driver.limits_for(self.profile, native_tts_base=selected())
        result = driver.limits_for(self.profile, native_tts_base=selected(), voice_consumer=True)
        self.assertEqual(result['starts'], 2)
        self.assertEqual(result['chats'], 8+2*consumer.POSTS_PER_PROFILE)
        for fields in (dict(native_asr=registration_asr()), dict(native_tts=selected()),
                       dict(voice_consumer=1)):
            options = dict(native_tts_base=selected(), voice_consumer=True)|fields
            with self.assertRaises(RuntimeError):driver.limits_for(self.profile, **options)

    def test_cross_client_checks_use_sufficient_budgets_and_distinct_owner(self):
        initial = driver.Stage.client_policy_base();before = copy.deepcopy(initial)
        policy = consumer.client_policy(initial, self.profile, selected())
        self.assertEqual(initial, before)
        first, owner = policy['clients']
        self.assertNotEqual(first['id'], owner['id'])
        self.assertEqual(owner['id'], evidence.VOICE_CONSUMER_WITNESS['owner'])
        self.assertEqual(first['max_output_tokens'], owner['max_output_tokens'])
        self.assertGreaterEqual(owner['max_output_tokens'], 1536)
        capacity.check_clients(self.profile, policy)

    def test_both_commands_use_private_saved_state_without_registration_flags(self):
        settings = package.settings(8080, 0, capacity_profile=self.profile,
            native_tts_base=selected(), application_state=dict(profile='q4t-objects-v1', directory='', **package.APPLICATION_LIMITS))
        plan = dict(base=dict(model_dir='/model', settings=settings),
            profiles=contract.profiles(self.profile), binary='/engine',
            port=19069, operations_port=19070, voice_consumer=dict(witness={}))
        for name in contract.NAMES:
            args = driver.command(plan, name, Path('/private'))
            self.assertIn('--tts-base-model-dir', args)
            self.assertEqual(args[args.index('--object-store-dir')+1],
                             '/private/'+name+'-application-objects')
            self.assertFalse(set(args)&{'--tts-model-dir', '--asr-model-dir',
                '--speaker-model-dir', '--vad-model-dir', '--aligner-model-dir'})
        self.assertEqual(plan['base']['settings']['application_state']['directory'], '')

    def test_complete_audio_hash_rejects_changed_samples_and_truncation(self):
        for key in ('speech', 'chat'):
            bad = copy.deepcopy(self.value)
            target = bad['speech'] if key == 'speech' else bad['chat']['choices'][0]['message']['audio']
            field = 'audio_base64' if key == 'speech' else 'data'
            raw = bytearray(base64.b64decode(target[field]));raw[-1] ^= 1
            target[field] = base64.b64encode(raw).decode()
            with self.assertRaisesRegex(ValueError, 'complete audio'):
                evidence.voice_consumer_business(bad, self.identity)
            target[field] = base64.b64encode(raw[:-2]).decode()
            with self.assertRaises(ValueError):evidence.voice_consumer_business(bad, self.identity)

    def test_saved_state_and_condition_witness_cannot_be_replaced(self):
        for key in ('state_sha256', 'condition_sha256', 'evidence_sha256', 'owner'):
            bad = copy.deepcopy(self.value);bad['witness'][key] = 'x'
            with self.assertRaisesRegex(ValueError, 'witness'):
                evidence.voice_consumer_business(bad, self.identity)

    def test_unloaded_encoder_is_accepted_only_without_failures_or_allocations(self):
        for field, changed in (('state', 'degraded'), ('load_attempts', 1),
                                ('device_bytes', 1), ('load_failures', True),
                                ('compute_failures', 1), ('asset_cleanup_failed', True)):
            bad = copy.deepcopy(self.value);bad['health']['condition'][field] = changed
            with self.assertRaises(ValueError):evidence.voice_consumer_business(bad, self.identity)

    def test_auxiliary_absence_and_base_execution_are_both_required(self):
        for key in ('tts', 'asr', 'alignment', 'vad', 'speaker', 'separator'):
            bad = copy.deepcopy(self.value);bad['health'][key]['state'] = 'ready'
            with self.assertRaisesRegex(ValueError, 'disabled backend'):
                evidence.voice_consumer_business(bad, self.identity)
        bad = copy.deepcopy(self.value);bad['health']['base']['load_attempts'] = 0
        with self.assertRaisesRegex(ValueError, 'not loaded'):
            evidence.voice_consumer_business(bad, self.identity)
        with self.assertRaisesRegex(ValueError, 'Base-only'):
            evidence.voice_consumer_business(self.value, dict(self.identity, native_asr={}))

    def test_history_and_both_revoked_routes_must_match_actual_identity(self):
        mutations = [lambda v:v['stored_chat']['choices'][0]['message']['audio'].update(data=''),
            lambda v:v['revocation']['chat'].update(status=200),
            lambda v:v['revocation']['consent_deleted'].update(id='cons_wrong'),
            lambda v:v['negative']['cross_chat'].update(status=200),
            lambda v:v['negative']['new_voice']['body']['error'].update(code='other_failure')]
        for mutate in mutations:
            bad = copy.deepcopy(self.value);mutate(bad)
            with self.assertRaises(ValueError):evidence.voice_consumer_business(bad, self.identity)


class ConsumerInputs(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]/'.q4t-work/consumer-cpu'
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup);self.root = Path(temporary.name)
        self.state = self.root/'registered-state';self.state.mkdir()
        self.receipt = self.root/'registered-state.json'
        self.witness = copy.deepcopy(evidence.VOICE_CONSUMER_WITNESS)
        self.witness['condition_sha256'] = ledger.digest([1., 2.])
        owner = hashlib.sha256(('client:'+self.witness['owner']).encode()).hexdigest()
        (self.state/'.q4t-format').write_text('CPU fixture, not an engine store')
        for key, kind, field, value in (('consent', 'voice_consent', 'language', 'en'),
                                         ('voice', 'voice', 'type', 'audio_sample')):
            data = dict(name='local-consent-renamed' if key == 'consent' else 'local-sample',
                        **{field:value})
            if key == 'voice':data.update(condition=[1., 2.], consent=self.witness['consent'])
            package.save(self.state/(owner+'.'+kind+'.'+self.witness[key]+'.json'),
                dict(record=dict(owner=owner, id=self.witness[key], kind=kind,
                    created_at=1, expires_at=0, data=data)))
        package.save(self.receipt, dict(files={p.name:package.sha(p) for p in self.state.iterdir()},
            consent=self.witness['consent'], voice=self.witness['voice'],
            completion='chatcmpl_cpu', isolated_fixture=True, use='CPU fixture only'))
        self.witness['state_sha256'] = package.sha(self.receipt)
        patch = mock.patch.object(evidence, 'VOICE_CONSUMER_WITNESS', self.witness)
        patch.start();self.addCleanup(patch.stop)

    def test_saved_state_copies_are_independent_and_original_is_unchanged(self):
        value = consumer.inputs(self.receipt)
        first, second = self.root/'primary', self.root/'light'
        consumer.clone(value, first);consumer.clone(value, second)
        name = next(n for n in value['files'] if '.voice.' in n)
        (first/name).write_text('revoked in this private profile')
        self.assertEqual(package.sha(second/name), value['files'][name])
        self.assertEqual(consumer.inputs(self.receipt), value)
        with self.assertRaisesRegex(RuntimeError, 'new and private'):consumer.clone(value, second)

    def test_drift_added_files_and_symlinks_fail_before_start(self):
        value = consumer.inputs(self.receipt)
        extra = self.state/'unexpected';extra.write_text('x')
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):consumer.inputs(self.receipt)
        extra.unlink()
        name = next(n for n in value['files'] if '.voice.' in n)
        path = self.state/name;raw = path.read_bytes();path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(RuntimeError, 'changed'):consumer.inputs(self.receipt)
        path.write_bytes(raw);other = self.root/'elsewhere';path.replace(other);path.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'regular'):consumer.inputs(self.receipt)

    def test_receipt_and_condition_replacement_cannot_qualify(self):
        raw = self.receipt.read_bytes();self.receipt.write_bytes(raw+b' ')
        with self.assertRaisesRegex(RuntimeError, 'receipt'):consumer.inputs(self.receipt)
        self.receipt.write_bytes(raw)
        with mock.patch.dict(self.witness, condition_sha256='0'*64):
            with self.assertRaisesRegex(RuntimeError, 'condition'):consumer.inputs(self.receipt)


class ConsumerWire(unittest.TestCase):
    setUp = ConsumerContracts.setUp

    def exercise(self, mutate=None):
        from tools.serve_recovery.test_profile_voice import VoiceDriver
        root = Path(__file__).resolve().parents[2]/'.q4t-work/consumer-wire-cpu'
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        value = self.value;owner = value['witness']['owner'];cross = driver.encrypted.CLIENT
        consent = value['consent']['id'];voice = value['voice']['id']
        def ok(body):return 200, json.dumps(body).encode()
        def error(row):return row['status'], json.dumps(row['body']).encode()
        speech = (('event: speech.audio.delta\ndata: '+json.dumps(dict(
            type='speech.audio.delta', audio=value['speech']['audio_base64']))+'\n\n')+
            ('event: speech.audio.done\ndata: '+json.dumps(dict(type='speech.audio.done',
                usage=value['speech']['usage']))+'\n\n')).encode()
        script = [
            ('GET', profile_voice.CONSENTS+'/'+consent, owner, *ok(value['consent'])),
            ('GET', profile_voice.CONSENTS+'/'+consent, cross, *error(value['negative']['cross_consent'])),
            ('POST', profile_voice.CONSENTS, owner, *error(value['negative']['new_consent'])),
            ('POST', profile_voice.VOICES, owner, *error(value['negative']['new_voice'])),
            ('POST', '/v1/audio/speech', cross, *error(value['negative']['cross_speech'])),
            ('POST', '/v1/chat/completions', cross, *error(value['negative']['cross_chat'])),
            ('POST', '/v1/audio/speech', owner, 200, speech),
            ('POST', '/v1/chat/completions', owner, *ok(value['chat'])),
            ('GET', '/v1/chat/completions/'+value['chat']['id'], owner, *ok(value['stored_chat'])),
            ('DELETE', profile_voice.CONSENTS+'/'+consent, owner, *ok(value['revocation']['consent_deleted'])),
            ('POST', '/v1/audio/speech', owner, *error(value['revocation']['speech'])),
            ('POST', '/v1/chat/completions', owner, *error(value['revocation']['chat'])),
            ('GET', profile_voice.CONSENTS+'/'+consent, owner, *error(value['revocation']['consent'])),
            ('DELETE', profile_voice.VOICES+'/'+voice, owner, *ok(value['revocation']['voice_deleted']))]
        if mutate:mutate(script)
        source = dict(manifest='/fixture', witness=value['witness'],
                      metadata=dict(consent=value['consent'], voice=value['voice']))
        stage = SimpleNamespace(plan=dict(voice_consumer=source, identity=self.identity, port=1),
            out=Path(temporary.name), profile_name='primary', counts=dict(chats=0, controls=0))
        def budget(key):stage.counts[key] += 1
        stage.budget = budget;stage.same_model = mock.Mock();stage.drained = mock.Mock()
        config = {'native_'+key+'_health':row for key,row in value['health'].items()
                  if key not in ('base', 'condition')}
        config.update(native_tts_base_health=value['health']['base'],
            native_voice_health=value['health']['condition'], native_tts_limits=value['limits'])
        stage.http = mock.Mock(return_value=ok(config))
        seen = []
        with mock.patch.object(consumer, 'inputs', return_value=source), mock.patch(
                'http.client.HTTPConnection', VoiceDriver.connections(self, script, seen)):
            result = consumer.Lifecycle(stage).run()
        return result, stage, seen

    def test_consumer_drives_real_route_shapes_without_transcription_or_registration_success(self):
        result, stage, seen = self.exercise()
        evidence.voice_consumer_business(result, self.identity)
        self.assertEqual(stage.counts, dict(chats=8, controls=6))
        self.assertEqual(len(seen), 14)
        self.assertNotIn('/v1/audio/transcriptions', [row[1] for row in seen])
        self.assertEqual(stage.same_model.call_count, 14)

    def test_failed_revocation_route_stops_the_driver_without_replaying_work(self):
        def fail(script):
            row = script[11];script[11] = (*row[:3], 200, b'{}')
        with self.assertRaises((RuntimeError, ValueError)):
            self.exercise(fail)


if __name__ == '__main__':
    unittest.main()
