"""Duplex qualification and recovery cannot silently lose input or its cost."""
import copy
import unittest
from unittest import mock
from tools.serve_recovery import ledger, profile_evidence as e, profile_migration as m
from tools.serve_recovery import profile_record, profile_duplex_evidence as d
from tools.serve_recovery import profile_duplex_reuse as reuse
from tools.serve_recovery.test_profile_speech import declaration as speech


def capabilities(configured=True, modern=True):
    value=speech()
    if modern:
        value.update(input_audio=d.declaration(configured),pending_execution=['mcp','tracing'],
            session_host_reservation_bytes=(2<<30)+(1536<<20 if configured else 0))
    return dict(realtime=dict(transcription_configured=configured,conversation=value))


class Capabilities(unittest.TestCase):
    def test_new_configured_input_requires_v20_old_speech_remains_v19(self):
        self.assertEqual(e.format_for({}, {},capabilities()),e.DUPLEX_FORMAT)
        self.assertEqual(e.format_for({}, {},capabilities(modern=False)),e.SPEECH_FORMAT)
        self.assertFalse(e.duplex_enabled(capabilities(False)))
        self.assertTrue(e.speech_enabled(capabilities(False)))

    def test_configuration_budget_and_complete_input_contract_are_bound(self):
        for mutate in [lambda v:v['realtime'].update(transcription_configured=False),
            lambda v:v['realtime']['conversation'].update(session_host_reservation_bytes=2<<30),
            lambda v:v['realtime']['conversation']['input_audio'].update(configured=1),
            lambda v:v['realtime']['conversation']['input_audio'].update(automatic_replay=True),
            lambda v:v['realtime']['conversation']['input_audio'].update(response_input_audio=False),
            lambda v:v['realtime']['conversation']['input_audio']['pipeline_recovery'].update(model_restart=True),
            lambda v:v['realtime']['conversation']['input_audio'].update(additional_host_reservation_bytes=0)]:
            v=capabilities();mutate(v)
            with self.assertRaises(ValueError):e.duplex_enabled(v)

    def test_extension_retains_audio_and_its_additional_memory_cost(self):
        a=m.semantic_capabilities(capabilities(modern=False));b=m.semantic_capabilities(capabilities())
        self.assertTrue(m.extends(a,b));self.assertFalse(m.extends(b,a));self.assertNotEqual(a,b)
        self.assertEqual(b['realtime']['conversation']['session_host_reservation_bytes'],2<<30)
        self.assertEqual(b['realtime']['conversation']['input_audio']['additional_host_reservation_bytes'],1536<<20)
        self.assertEqual(capabilities()['realtime']['conversation']['session_host_reservation_bytes'],3584<<20)
        for before,after in [(capabilities(modern=False),capabilities()),(capabilities(),capabilities(modern=False))]:
            with mock.patch.object(m,'observations',side_effect=[before,after]):
                with self.assertRaises(ValueError):m.permit({}, {}, {}, {}, {}, {},
                    automatic=True,allow_capability_extension=True)

    def test_v20_retains_existing_eight_mib_record_limit(self):
        value=dict(format=e.DUPLEX_FORMAT,reused_business=dict(format=reuse.FORMAT))
        profile_record.shape(value,7<<20)
        for v,size in [(value,(8<<20)+1),(dict(value,format=e.SPEECH_FORMAT),7<<20),
                       (dict(value,reused_business={}),7<<20)]:
            with self.assertRaises(ValueError):profile_record.shape(v,size)


def witness():
    before=dict(admitted=10,settled=50,reserved=0,active=0,sessions=0,host_bytes=0,free_slots=1)
    return dict(format=d.FORMAT,source=dict(path='docs/STATUS.md',sha256='f'*64,version='rev376'),
        sessions={mode:dict(session=dict(mode=mode,events=[{}]),before=dict(before),
            after=dict(before,admitted=10+(11 if mode=='manual' else 5),settled=100+(10 if mode=='manual' else 0)))
            for mode in ('manual','vad')})


def observed(session,phrase,source):
    return dict(admitted=11 if session['mode']=='manual' else 5,private_asr=int(session['mode']=='manual'),
                visible_tokens=50,quality=[dict(status='PASS')])


class Business(unittest.TestCase):
    @mock.patch.object(d.duplex_checks,'observe',side_effect=observed)
    def test_each_native_stage_must_settle_its_own_admission_and_resources(self,_observer):
        value=witness();d.business(value)
        for mode in ('manual','vad'):
            for key in ('admitted','settled','reserved','sessions','host_bytes','free_slots'):
                bad=copy.deepcopy(value)
                bad['sessions'][mode]['after'][key]=0 if key in ('admitted','settled','free_slots') else 1
                with self.assertRaises(ValueError):d.business(bad)
        bad=copy.deepcopy(value);bad['sessions']['manual']['after']['settled']=100
        with self.assertRaises(ValueError):d.business(bad)

    def test_quality_failure_and_missing_real_source_cannot_qualify(self):
        value=witness()
        with mock.patch.object(d.duplex_checks,'observe',return_value=dict(quality=[dict(status='FAIL')])):
            with self.assertRaises(ValueError):d.business(value)
        for key,v in [('version','made-up'),('sha256','x'),('path','synthetic.txt')]:
            bad=copy.deepcopy(value);bad['source'][key]=v
            with self.assertRaises(ValueError):d.business(bad)


class SourceReuse(unittest.TestCase):
    def setUp(self):
        review=dict(reuse.REVIEW,previous_binary='a'*64,previous_protocol='b'*64,
                    candidate_binary='c'*64,candidate_protocol='d'*64)
        old=dict(identity=dict(binary_sha256='a'*64,protocol_sha256='b'*64,model='fixed'),
                 capacity=dict(max_tokens=256),capabilities=dict(capabilities(modern=False),protocol_source_sha256='b'*64),
                 business=dict(schema=dict(answer=42),embedding=[1,2,3],realtime={},semantic_vad={}))
        source=dict(identity=ledger.digest(old['identity']),capacity=ledger.digest(old['capacity']),
                    capabilities=ledger.digest(old['capabilities']),business={k:ledger.digest(v) for k,v in old['business'].items()},
                    record_sha256='e'*64,record_format=e.SPEECH_FORMAT,prior_reuse='f'*64)
        self.value=copy.deepcopy(old);self.value.update(format=e.DUPLEX_FORMAT,profile='primary')
        self.value['identity'].update(binary_sha256='c'*64,protocol_sha256='d'*64)
        self.value['capabilities']=dict(capabilities(),protocol_source_sha256='d'*64)
        self.value['business']['realtime_duplex']={}
        for key,v in [('REVIEW',review),('SOURCES',dict(primary=source))]:
            patch=mock.patch.object(reuse,key,v);patch.start();self.addCleanup(patch.stop)

    def test_only_fixed_unaffected_business_may_be_reused(self):
        value=reuse.attach(self.value);reuse.validate(value)
        self.assertEqual(value['reused_business']['keys'],['embedding','schema'])
        self.assertEqual(value['reused_business']['fresh_keys'],list(reuse.FRESH))
        self.assertNotIn('record',value['reused_business'])

    def test_other_binary_backend_capacity_and_capability_changes_are_rejected(self):
        for mutate in [lambda v:v['identity'].update(binary_sha256='0'*64),
            lambda v:v['identity'].update(model='other'),lambda v:v['capacity'].update(max_tokens=1),
            lambda v:v['capabilities'].update(extra=True)]:
            value=reuse.attach(self.value);mutate(value)
            with self.assertRaises(ValueError):reuse.validate(value)

    def test_missing_fresh_work_or_modified_legacy_receipt_cannot_reuse(self):
        for mutate in [lambda v:v['business'].pop('realtime_duplex'),
            lambda v:v['business'].pop('semantic_vad'),lambda v:v['business'].update(schema='changed'),
            lambda v:v['reused_business'].update(keys=[]),lambda v:v['reused_business'].update(fresh_keys=[])]:
            value=reuse.attach(self.value);mutate(value)
            with self.assertRaises(ValueError):reuse.validate(value)

    def test_unknown_source_review_or_prior_certificate_is_rejected(self):
        for field,key in [('review','source_review'),('source','record_sha256'),('source','prior_reuse')]:
            value=reuse.attach(self.value);value['reused_business'][field][key]='0'*64
            with self.assertRaises(ValueError):reuse.validate(value)


class Driver(unittest.TestCase):
    def test_completion_requires_both_capacities_and_all_bounded_work(self):
        from tools.serve_recovery import profile_duplex_targeted as stage
        value=dict(status=stage.SUCCESS,binary_sha256=stage.reuse.REVIEW['candidate_binary'],seconds=90,
                   counts={k:v for k,v in stage.LIMITS.items() if k!='seconds'})
        stage.check_result(value)
        for key in ('starts','chats','websocket_sessions','ingress_starts','controls'):
            bad=copy.deepcopy(value);bad['counts'][key]+=1
            with self.assertRaises(RuntimeError):stage.check_result(bad)
        for mutate in [lambda v:v.update(status='FAIL'),lambda v:v.update(seconds=1201),
                       lambda v:v.update(binary_sha256='0'*64),lambda v:v['counts'].update(starts=True)]:
            bad=copy.deepcopy(value);mutate(bad)
            with self.assertRaises(RuntimeError):stage.check_result(bad)

    def test_partial_evidence_is_bounded_and_never_overwrites_original(self):
        import os
        from pathlib import Path
        import tempfile
        from tools.serve_recovery.profile_duplex import save_observed
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR','.q4t-work/profile-duplex-cpu'))
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path=Path(directory)/'original.json';save_observed(path,dict(status='original'))
            with self.assertRaises(FileExistsError):save_observed(path,dict(status='replacement'))
            self.assertIn('original',path.read_text())
            with self.assertRaises(RuntimeError):save_observed(Path(directory)/'huge.json',dict(value='x'*(4<<20)))
            self.assertFalse((Path(directory)/'huge.json').exists())
