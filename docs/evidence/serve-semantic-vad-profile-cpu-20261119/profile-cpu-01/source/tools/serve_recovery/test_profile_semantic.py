"""Synthetic semantic qualification negatives; no native model execution."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_realtime.test_semantic_acceptance import evidence as sdk_fixture
from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_semantic as semantic
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import test_profile_realtime as realtime
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_service import capacity


def declaration():
    return dict(implemented=True, configured=True, profile='q4t-native-semantic-turn-v1',
        classifier='native_primary_model', calibrated=False, serial_worker=True,
        usage='included_in_item_and_client_tokens',
        maximum_wait_ms=dict(auto=4000, low=8000, medium=4000, high=2000))


def identity():
    return dict(native_asr=dict(model='qwen3-asr-0.6b', limits=dict(max_output_tokens=128),
        vad=dict(model='silero-vad-16k', limits=dict(max_seconds=600))))


def record(fixed=None):
    fixed=fixed or identity(); turns=[]
    for continuation in (False, True):
        row=sdk_fixture(continuation)
        row['terminal']['logprobs']=[dict(token=evidence.ASR_TEXT, logprob=-.2,
                                         bytes=list(evidence.ASR_TEXT.encode()))]
        turns.append(row)
    before=dict(admitted=4, settled=100, reserved=0, active=0, sessions=0,
                host_bytes=0, free_slots=1)
    value=dict(input_sha256=evidence.REALTIME_INPUT_SHA, settings=evidence.semantic_settings(),
        turns=turns, before=before, after=dict(before, admitted=6, settled=490))
    for name, backend in (('asr',fixed['native_asr']),('vad',fixed['native_asr']['vad'])):
        value[name+'_limits']=copy.deepcopy(backend['limits'])
        value[name+'_health']=dict(state='ready', reason='none', model=backend['model'],
                                  core_readiness_dependency=False, compute_failures=0)
    return value


class SemanticEvidence(unittest.TestCase):
    def test_actual_turns_usage_health_and_format_are_bound(self):
        value=record(); before=copy.deepcopy(value)
        evidence.semantic_business(value,identity());self.assertEqual(value,before)
        caps=dict(realtime=dict(realtime.declaration(),semantic_vad=declaration()),
                  structured_output=dict(available=True))
        self.assertTrue(evidence.semantic_vad_enabled(identity(),caps))
        self.assertEqual(evidence.format_for({},identity(),caps),evidence.SEMANTIC_FORMAT)
        self.assertFalse(evidence.semantic_vad_enabled(identity(),{}))

    def test_missing_dependency_or_changed_claim_cannot_qualify(self):
        caps=dict(realtime=dict(semantic_vad=declaration()),structured_output=dict(available=True))
        for change in (dict(configured=False),dict(calibrated=True),dict(serial_worker=False),
                       dict(usage='unaccounted'),dict(maximum_wait_ms=dict(high=2000))):
            wrong=copy.deepcopy(caps);wrong['realtime']['semantic_vad'].update(change)
            with self.assertRaises(ValueError):evidence.semantic_vad_enabled(identity(),wrong)
        with self.assertRaises(ValueError):evidence.semantic_vad_enabled({},caps)
        wrong=copy.deepcopy(caps);wrong['structured_output']['available']=False
        with self.assertRaises(ValueError):evidence.semantic_vad_enabled(identity(),wrong)

    def test_unknown_timeout_or_unbilled_classifier_rejects(self):
        for field,value in [('accepted',0),('attempts',9),('maximum_wait',True),
            ('probability_available',False),('stale',1),('input_tokens',0),
            ('output_tokens',15),('included_in_usage',False),('accepted',True)]:
            row=record();row['turns'][0]['terminal']['q4t_semantic_vad'][field]=value
            with self.assertRaises(ValueError):evidence.semantic_business(row,identity())

    def test_pause_transcript_audio_and_event_correlation_cannot_disappear(self):
        for mutate in (lambda r:r['turns'].pop(),
            lambda r:r['turns'][1]['continuation'].update(no_commit=False),
            lambda r:r['turns'][0]['terminal'].update(transcript='wrong'),
            lambda r:r['turns'][0]['events'].append(dict(type='error')),
            lambda r:r['turns'][0]['events'][0].update(item_id='foreign'),
            lambda r:r['turns'][0]['retrieved']['item']['content'][0].update(audio='AA=='),
            lambda r:r['turns'][0]['terminal']['logprobs'][0].update(logprob=float('nan'))):
            row=record();mutate(row)
            with self.assertRaises(ValueError):evidence.semantic_business(row,identity())

    def test_missing_metric_or_private_work_and_leaked_resources_reject(self):
        for field,value in [('admitted',5),('settled',489),('reserved',1),
                            ('sessions',1),('free_slots',0),('host_bytes',True)]:
            row=record();row['after'][field]=value
            with self.assertRaises(ValueError):evidence.semantic_business(row,identity())
        row=record();row['after'].pop('host_bytes')
        with self.assertRaises(ValueError):evidence.semantic_business(row,identity())

    def test_backend_health_and_limits_are_required_after_actual_turns(self):
        for name in ('asr','vad'):
            for change in (dict(state='failed'),dict(compute_failures=1),
                           dict(core_readiness_dependency=True),dict(model='other')):
                row=record();row[name+'_health'].update(change)
                with self.assertRaises(ValueError):evidence.semantic_business(row,identity())
            row=record();row[name+'_limits']['invented']=True
            with self.assertRaises(ValueError):evidence.semantic_business(row,identity())

    def test_compaction_keeps_audio_and_errors_without_mutating_raw_sdk(self):
        value=record()['turns'][0];value['events'].append(dict(type='conversation.item.retrieved'))
        original=copy.deepcopy(value);result=semantic.compact(value,False)
        self.assertEqual(value,original);self.assertEqual(result['retrieved'],value['retrieved'])
        result['events'].append(dict(type='error'))
        with self.assertRaises(ValueError):semantic.compact(result,False)


class SemanticDriver(unittest.TestCase):
    def directory(self):
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',semantic.ROOT/'.q4t-work/semantic-profile-cpu'))
        root.mkdir(parents=True,exist_ok=True)
        tmp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_limits_charge_both_profiles_and_require_real_vad(self):
        profile=capacity.normalize(dict(version=1))
        old=driver.limits_for(profile,native_asr=identity()['native_asr'],realtime=True)
        new=driver.limits_for(profile,native_asr=identity()['native_asr'],realtime=True,semantic_vad=True)
        self.assertEqual(new['chats']-old['chats'],4)
        self.assertEqual((new['websocket_sessions'],new['ingress_starts']),(6,4))
        with self.assertRaises(RuntimeError):driver.limits_for(profile,native_asr={},realtime=True,semantic_vad=True)
        with self.assertRaises(RuntimeError):driver.limits_for(profile,native_asr=identity()['native_asr'],semantic_vad=True)

    def test_owned_entry_closes_on_sdk_failure_without_business_receipt(self):
        out=self.directory();charged=[]
        stage=SimpleNamespace(out=out,profile_name='primary',history={},model=SimpleNamespace(poll=lambda:None),
            budget=lambda k:charged.append(k),http=lambda *a,**k:(200,b''),
            plan=dict(semantic_vad=True,port=1,operations_port=2,
                sdk=dict(sdk_directory='/sdk',node=dict(path='/node')),
                realtime=dict(files={},sdk_directory='/sdk',ingress='/ingress',public_port=3,
                    tls=dict(certificate='/ca'),clip=dict(path='/fixture'))))
        edge=SimpleNamespace(process=SimpleNamespace(pid=99,returncode=0),close=mock.Mock())
        with (mock.patch.object(semantic,'read',return_value=dict(gateway_token='cpu')),
             mock.patch.object(semantic,'NativeIngress',return_value=edge),
             mock.patch.object(semantic.realtime,'state',return_value=record()['before']),
             mock.patch.object(semantic.realtime.encrypted.foreground.base.lifecycle.guard,'processes',return_value={99:{}}),
             mock.patch.object(semantic.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'{"status":"FAIL"}',''))):
            with self.assertRaisesRegex(RuntimeError,'actual semantic SDK failed'):semantic.run(stage)
        edge.close.assert_called_once()
        self.assertEqual(charged,['ingress_starts','websocket_sessions','chats'])
        self.assertTrue((out/'primary-semantic-entry-cleanup.json').exists())
        self.assertFalse((out/'primary-semantic-business.json').exists())

    def test_health_uses_only_operations_port_and_retains_failure(self):
        out=self.directory();received=[];response=[200,b'{"state":"ready"}']
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                received.append((self.path,self.headers.get('X-Q4T-Client-Id'),self.headers.get('X-Q4T-Gateway-Token')))
                self.send_response(response[0]);self.send_header('Content-Length',str(len(response[1])))
                self.end_headers();self.wfile.write(response[1])
        server=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        stage=SimpleNamespace(out=out,budget=mock.Mock(),plan=dict(port=0,operations_port=server.server_port))
        try:
            self.assertEqual(semantic.health(stage,'good'),dict(state='ready'))
            response[:]=[403,b'{"error":"denied"}']
            with self.assertRaises(RuntimeError):semantic.health(stage,'denied')
            self.assertEqual((out/'denied.wire').read_bytes(),response[1])
            response[:]=[200,b' '*((1<<20)+1)]
            with self.assertRaises(RuntimeError):semantic.health(stage,'large')
        finally:
            server.shutdown();server.server_close();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received,[('/v1/config',None,None)]*3)
        self.assertEqual(stage.budget.call_count,3)


class SemanticMigration(unittest.TestCase):
    setUp=realtime.RealtimeMigration.setUp
    stamp=realtime.RealtimeMigration.stamp
    selected=realtime.RealtimeMigration.selected
    voice_registry=realtime.RealtimeMigration.voice_registry
    extended=realtime.RealtimeMigration.extended

    def test_semantic_upgrade_requires_actual_business_and_cannot_auto_drop_it(self):
        _,_,old,first=self.extended();new=copy.deepcopy(old)
        new['identity'].update(binary_sha256='3'*64,protocol_sha256='4'*64)
        rows={n:copy.deepcopy(row['record']) for n,row in first.items()}
        for row in rows.values():
            row['format']=evidence.SEMANTIC_FORMAT
            row['capabilities']['realtime']=dict(realtime.declaration(),semantic_vad=declaration())
            row['capabilities'].setdefault('structured_output',{})['available']=True
            row['business']['realtime']=realtime.record()
        with mock.patch.object(evidence,'REALTIME_WAVEFORM_SHA',hashlib.sha256(realtime.PCM).hexdigest()):
            missing=previous.bind_records(new,rows)
            with self.assertRaises(ValueError):
                evidence.validate(missing['primary']['record'],new,'primary',missing['primary']['sha256'])
            for row in rows.values():row['business']['semantic_vad']=record(new['identity'])
            second=previous.bind_records(new,rows)
            grant=migration.permit(old,new,first,second,self.config,self.config,allow_capability_extension=True)
            self.assertFalse(grant['automatic'])
            with self.assertRaisesRegex(ValueError,'capabilities'):
                migration.permit(new,old,second,first,self.config,self.config,automatic=True)


if __name__=='__main__':unittest.main()
