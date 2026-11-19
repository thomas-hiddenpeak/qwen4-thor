"""Synthetic CPU receipt/driver negatives; never realtime model qualification."""
import base64
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import profile_realtime as realtime
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_recovery import test_voice_icl_profiles as icl
from tools.serve_service import capacity, package

PCM = b'\x00\x00'*100894  # Deliberately silent CPU bytes, not speech evidence.


def declaration():
    return dict(transcription_configured=True, automatic_replay=False,
                per_turn_client_admission=True)


def record():
    turns, checks = [], []
    for i, initial in enumerate((70, 81, 70)):
        item = 'item_cpu_'+str(i)
        turns.append(dict(item_id=item, before_commit=None, delta=evidence.ASR_TEXT,
            committed=dict(type='input_audio_buffer.committed', item_id=item,
                previous_item_id='item_cpu_0' if i == 1 else None),
            terminal=dict(type='conversation.item.input_audio_transcription.completed',
                item_id=item, transcript=evidence.ASR_TEXT, usage=dict(type='tokens',
                    input_tokens=initial, output_tokens=11, total_tokens=initial+11)),
            retrieved=dict(type='conversation.item.retrieved', item=dict(
                id=item, object='realtime.item', type='message', role='user', status='completed',
                content=[dict(type='input_audio', transcript=evidence.ASR_TEXT,
                    audio=base64.b64encode(PCM).decode())]))))
        if i < 2:
            label='missing-'+str(i)
            checks.extend([dict(type='deleted', item_id=item, event=dict(
                type='conversation.item.deleted', item_id=item)),
                dict(type='absent', item_id=item, label=label, event=dict(type='error',
                    error=dict(code='item_not_found', event_id=label)))])
    state=dict(admitted=5, settled=200, reserved=0, active=0, sessions=0,
               host_bytes=0, free_slots=1)
    return dict(input_sha256=evidence.REALTIME_INPUT_SHA,
        session=dict(status='PASS', connections=1, close_code=1000,
            automatic_replay=False, turns=turns, checks=checks),
        before=state, after=dict(state, admitted=8, settled=454))


class RealtimeEvidence(unittest.TestCase):
    def setUp(self):
        # Only fixture identity is changed; production SHA verification still
        # executes. This patch can never be used to seal real qualifications.
        patch = mock.patch.object(evidence, 'REALTIME_WAVEFORM_SHA',
                                  hashlib.sha256(PCM).hexdigest())
        patch.start();self.addCleanup(patch.stop)

    def test_configuration_binds_asr_and_forbids_replay(self):
        self.assertTrue(evidence.realtime_enabled(dict(native_asr={}),
                                                 dict(realtime=declaration())))
        self.assertFalse(evidence.realtime_enabled(dict(native_asr={}), {}))
        for change in (dict(automatic_replay=True), dict(transcription_configured=1),
                       dict(per_turn_client_admission=False)):
            with self.assertRaises(ValueError):
                evidence.realtime_enabled(dict(native_asr={}),
                    dict(realtime=dict(declaration(), **change)))
        with self.assertRaises(ValueError):
            evidence.realtime_enabled({}, dict(realtime=declaration()))

    def test_complete_actual_usage_and_original_state_are_preserved(self):
        value=record();before=copy.deepcopy(value)
        evidence.realtime_business(value)
        self.assertEqual(value,before)
        self.assertEqual(evidence.format_for({}, dict(native_asr={}),
            dict(realtime=declaration())), evidence.REALTIME_FORMAT)

    def test_context_deletion_and_rate_settlement_cannot_be_faked(self):
        for mutate in (
                lambda r:r['session']['turns'][1]['terminal']['usage'].update(
                    input_tokens=70,total_tokens=81),
                lambda r:r['session']['turns'][2]['terminal']['usage'].update(
                    input_tokens=81,total_tokens=92),
                lambda r:r['session']['turns'][2]['committed'].update(previous_item_id='item_cpu_1'),
                lambda r:r['after'].update(settled=443),
                lambda r:r['after'].update(admitted=9)):
            value=record();mutate(value)
            with self.assertRaises(ValueError):evidence.realtime_business(value)

    def test_waveform_transcript_and_error_correlation_are_checked(self):
        for mutate in (
                lambda r:r.update(input_sha256='0'*64),
                lambda r:r['session']['turns'][0]['retrieved']['item']['content'][0].update(
                    audio=base64.b64encode(b'\x01\x00'+PCM[2:]).decode()),
                lambda r:r['session']['turns'][0]['retrieved']['item']['content'][0].update(transcript='fake'),
                lambda r:r['session']['turns'][0]['terminal'].update(transcript='fake'),
                lambda r:r['session']['checks'][1]['event']['error'].update(event_id='other'),
                lambda r:r['session']['checks'][0]['event'].update(item_id='other')):
            value=record();mutate(value)
            with self.assertRaises(ValueError):evidence.realtime_business(value)

    def test_reconnect_partial_receipt_or_leaked_resources_cannot_qualify(self):
        for mutate in (
                lambda r:r['session'].update(connections=2),
                lambda r:r['session'].update(automatic_replay=True),
                lambda r:r['session']['turns'].pop(),
                lambda r:r['session']['checks'].pop(),
                lambda r:r['after'].update(host_bytes=1073741824),
                lambda r:r['after'].update(sessions=1),
                lambda r:r['after'].update(reserved=5),
                lambda r:r['after'].update(free_slots=0)):
            value=record();mutate(value)
            with self.assertRaises(ValueError):evidence.realtime_business(value)

    def test_metric_projection_cannot_turn_missing_or_invalid_values_into_zero(self):
        client='cpu'
        values={'q4t_client_rate_'+k+'{client="cpu"}':v for k,v in
            [('admitted_total',3),('settled_tokens_total',254),('reserved_tokens',0)]}
        values.update(q4t_active_chats=0,q4t_realtime_sessions=0,
                      q4t_realtime_host_reserved_bytes=0,q4t_seq_slots_free=1)
        self.assertEqual(realtime.state(values,client)['settled'],254)
        for value in (None, True, .5, float('nan'), float('inf'), -1):
            wrong=dict(values,q4t_realtime_host_reserved_bytes=value)
            with self.assertRaises(RuntimeError):realtime.state(wrong,client)

    def test_fixed_limits_and_cli_reserve_every_actual_session_and_turn(self):
        profile=capacity.normalize(dict(version=1))
        plain=driver.limits_for(profile,native_asr={})
        selected=driver.limits_for(profile,native_asr={},realtime=True)
        self.assertEqual(selected['chats']-plain['chats'],6)
        self.assertEqual(selected['websocket_sessions'],2)
        self.assertEqual(selected['ingress_starts'],2)
        with self.assertRaises(RuntimeError):driver.limits_for(profile,realtime=True)
        with mock.patch.object(driver,'prepare') as prepare, mock.patch('sys.argv',[
            'profile_acceptance.py','prepare','--binary','/bin','--package','/pkg',
            '--output','/output','--realtime-reference','/reference']):
            driver.main()
        self.assertEqual(prepare.call_args.args[0].realtime_reference,Path('/reference'))

    def test_sdk_timeout_still_releases_only_the_owned_native_entry(self):
        parent=realtime.ROOT/'.q4t-work/realtime-profile-cpu';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            out=Path(tmp);counts=[]
            stage=SimpleNamespace(out=out,profile_name='primary',history={},
                plan=dict(port=18090,operations_port=18091,sdk=dict(node=dict(path='/node')),
                    realtime=dict(format='q4t-profile-realtime-inputs-v1',files={},
                        clip=dict(path='/fixed.pcm'),sdk_directory='/sdk',ingress='/ingress',
                        public_port=18092,tls=dict(certificate='/ca'),settings={})),
                budget=counts.append,model=SimpleNamespace(poll=lambda:None))
            package.save(out/'client-policy.json',dict(gateway_token='b'*64,
                clients=[dict(id=realtime.encrypted.CLIENT)]))
            stage.http=mock.Mock(return_value=(200,b'ignored by metric fixture'))
            edge=SimpleNamespace(process=SimpleNamespace(pid=1234,returncode=0),close=mock.Mock())
            with mock.patch.object(realtime,'state',return_value=record()['before']), \
                 mock.patch.object(realtime.encrypted.foreground.base.lifecycle,'parse_metrics',return_value={}), \
                 mock.patch.object(realtime,'NativeIngress',return_value=edge), \
                 mock.patch.object(realtime.encrypted.foreground.base.lifecycle.guard,'processes',return_value={1234:{}}), \
                 mock.patch.object(realtime.subprocess,'run',side_effect=subprocess.TimeoutExpired('sdk',135)):
                with self.assertRaises(subprocess.TimeoutExpired):realtime.run(stage)
            edge.close.assert_called_once()
            self.assertEqual(counts,['ingress_starts','websocket_sessions','chats','chats','chats'])
            self.assertTrue((out/'primary-realtime-entry-cleanup.json').exists())
            self.assertFalse((out/'primary-realtime-business.json').exists())


class RealtimeMigration(unittest.TestCase):
    setUp=icl.IclMigration.setUp
    stamp=icl.IclMigration.stamp
    selected=icl.IclMigration.selected
    voice_registry=icl.IclMigration.voice_registry
    extended=icl.IclMigration.extended

    def test_realtime_requires_new_observations_and_cannot_disappear_at_fallback(self):
        _,_,old,first=self.extended()
        new=copy.deepcopy(old)
        new['identity'].update(binary_sha256='3'*64,protocol_sha256='4'*64)
        records={n:copy.deepcopy(row['record']) for n,row in first.items()}
        for row in records.values():
            row['format']=evidence.REALTIME_FORMAT
            row['capabilities']['realtime']=declaration()
        missing=previous.bind_records(new,records)
        with self.assertRaises(ValueError):
            evidence.validate(missing['primary']['record'],new,'primary',missing['primary']['sha256'])
        for row in records.values():row['business']['realtime']=record()
        second=previous.bind_records(new,records)
        with mock.patch.object(evidence,'REALTIME_WAVEFORM_SHA',hashlib.sha256(PCM).hexdigest()):
            grant=migration.permit(old,new,first,second,self.config,self.config,
                                   allow_capability_extension=True)
            self.assertFalse(grant['automatic'])
            with self.assertRaisesRegex(ValueError,'capabilities'):
                migration.permit(new,old,second,first,self.config,self.config,automatic=True)


if __name__ == '__main__':
    unittest.main()
