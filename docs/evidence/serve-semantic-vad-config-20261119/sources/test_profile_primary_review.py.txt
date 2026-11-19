"""CPU negatives for complete-primary reuse; no HTTP or model execution."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_primary_review as review
from tools.serve_service import capacity, package
from tools.serve_service.test_voice_package import registration_asr, selected


class PrimaryReview(unittest.TestCase):
    def setUp(self):
        parent=driver.ROOT/'.q4t-work/primary-review-cpu'
        parent.mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.out=Path(temporary.name)

    @staticmethod
    def original():
        return {'plan.json':dict(semantic_vad=True,binary_sha256='a'*64,
                    limits=dict(chats=60,seconds=900)),
            'result.json':dict(status='FAILED',error="profile_semantic.run(self)\nKeyError: 'native_vad_limits'\n",
                binary_sha256='a'*64,counts=dict(starts=1,chats=30,
                    websocket_sessions=3,ingress_starts=2),seconds=185),
            'exit.json':dict(status='COMMAND_FAILED',child_returncode=1,
                compute_backend='nvml',lease_inherited_by_child=True),
            'cleanup.json':dict(remaining=[],model_exit=0,ports_released=True,
                lease_continuously_held=True)}

    def environment(self,records,query=None):
        lease=self.out/'lease';lease.mkdir(exist_ok=True)
        query=query or dict(status='OK',returncode=0,pids=[])
        (lease/'nvml-queries.jsonl').write_text(json.dumps(query)+'\n')
        with mock.patch.object(review,'read',side_effect=lambda p:records[p.name]), \
             mock.patch.object(review,'sha',return_value='b'*64):
            return review.original_environment(self.out)

    def test_only_complete_primary_preserves_original_failure_and_counts(self):
        original=self.original();before=copy.deepcopy(original)
        with mock.patch('http.client.HTTPConnection',side_effect=AssertionError('network forbidden')):
            value=self.environment(original)
        self.assertEqual(original,before)
        self.assertEqual((value['model_starts'],value['requests']),(1,30))
        self.assertTrue(value['original_failure_preserved'])

    def test_partial_work_engine_failure_or_invalid_environment_cannot_be_reused(self):
        changes=[lambda r:r['result.json']['counts'].update(chats=29),
            lambda r:r['result.json']['counts'].update(starts=2),
            lambda r:r['result.json']['counts'].update(websocket_sessions=2),
            lambda r:r['result.json']['counts'].update(ingress_starts=1),
            lambda r:r['result.json'].update(status='PASS'),
            lambda r:r['result.json'].update(error='device_error'),
            lambda r:r['result.json'].update(binary_sha256='c'*64),
            lambda r:r['result.json'].update(seconds=900),
            lambda r:r['exit.json'].update(status='ENV_ABORT'),
            lambda r:r['exit.json'].update(compute_backend='unavailable'),
            lambda r:r['cleanup.json'].update(remaining=[1234]),
            lambda r:r['cleanup.json'].update(model_exit=1),
            lambda r:r['cleanup.json'].update(ports_released=False)]
        for change in changes:
            wrong=self.original();change(wrong)
            with self.assertRaises(RuntimeError):self.environment(wrong)
        for query in (dict(status='ERROR',returncode=1,pids=[]),
                      dict(status='OK',returncode=0,pids=[1234])):
            with self.assertRaises(RuntimeError):self.environment(self.original(),query)

    def test_original_request_body_and_http_status_are_required(self):
        actor=object.__new__(review.RecordedPrimary);actor.source=self.out
        package.save(self.out/'primary-schema.http.json',dict(status=200))
        package.save(self.out/'primary-schema.request.json',dict(original=True))
        (self.out/'primary-schema.wire').write_bytes(b'{"answer":42}')
        self.assertEqual(actor.request('schema',dict(original=True)),dict(answer=42))
        with self.assertRaises(RuntimeError):actor.request('schema',dict(original=False))
        package.save(self.out/'primary-schema.http.json',dict(status=500))
        with self.assertRaises(RuntimeError):actor.request('schema',dict(original=True))

    def test_evidence_binding_rejects_changed_original(self):
        path=self.out/'original';path.write_text('kept')
        frozen=dict(format=review.FORMAT,files={str(path):package.sha(path)},
                    source=str(self.out),environment=dict(original=True))
        with mock.patch.object(review,'original_environment',return_value=dict(original=True)):
            review.verify(frozen);path.write_text('changed')
            with self.assertRaises(RuntimeError):review.verify(frozen)

    def test_resume_counts_only_light_and_rejects_incompatible_reuse_modes(self):
        primary=capacity.normalize(dict(version=1))
        args=dict(native_asr=registration_asr(),native_tts_base=selected(),
                  realtime=True,semantic_vad=True)
        full=driver.limits_for(primary,**args)
        tail=driver.limits_for(primary,retained_primary_business=True,**args)
        self.assertEqual(tail['chats']*2,full['chats'])
        self.assertEqual((tail['starts'],tail['websocket_sessions'],
                          tail['ingress_starts'],tail['seconds']),(1,3,2,600))
        with self.assertRaises(RuntimeError):driver.limits_for(primary,retained_primary_business=True)
        with self.assertRaises(RuntimeError):driver.limits_for(primary,
            retained_primary_business=True,retained_primary_voice=True,**args)

    def test_resume_business_runs_light_once_without_starting_another_model(self):
        stage=object.__new__(driver.Stage)
        stage.plan=dict(retained_primary_business={});stage.out=self.out;stage.profile_name='light'
        stage.profile_business=mock.Mock();stage.start=mock.Mock(side_effect=AssertionError('extra model'))
        stage.cleanup=mock.Mock(side_effect=AssertionError('unexpected cleanup'))
        with mock.patch.object(review,'verify'),mock.patch.object(driver,'read',return_value={}), \
             mock.patch.object(driver.profile_contract,'capability_pair') as pair:
            stage.business();pair.assert_called_once()
            stage.profile_name='primary'
            with self.assertRaises(RuntimeError):stage.business()
        stage.profile_business.assert_called_once();stage.start.assert_not_called()

    def test_resume_start_selects_light_before_any_process_can_launch(self):
        stage=object.__new__(driver.Stage)
        stage.plan=dict(retained_primary_business={},port=1,operations_port=2)
        stage.counts=dict(starts=0)
        with mock.patch.object(driver.background,'probe_ports',side_effect=RuntimeError('stop before launch')):
            with self.assertRaisesRegex(RuntimeError,'stop before launch'):stage.start()
        self.assertEqual(stage.profile_name,'light')


if __name__ == '__main__':unittest.main()
