"""CPU counterexamples for reuse of original completed voice exchanges."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_voice_review as review
from tools.serve_service import capacity, package
from tools.serve_service.test_voice_package import registration_asr, selected


class VoiceReview(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2]/'.q4t-work/voice-review-cpu'
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def actor(self, body=b'original'):
        actor = object.__new__(review.RecordedVoice)
        actor.stage = SimpleNamespace(out=self.out)
        actor.requests = 0; actor.seen = set()
        prefix = self.out/'primary-voice-create'
        package.save(str(prefix)+'.request.json',dict(method='POST',path='/v1/audio/voices',
            client=review.profile_voice.encrypted.CLIENT,content_type='application/json',
            body_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest()))
        Path(str(prefix)+'.request.body').write_bytes(body)
        Path(str(prefix)+'.wire').write_bytes(b'original-response')
        package.save(str(prefix)+'.http.json',dict(status=200))
        return actor

    def test_review_uses_original_bytes_without_http_and_rejects_duplicate(self):
        actor = self.actor()
        with mock.patch('http.client.HTTPConnection',side_effect=AssertionError('network forbidden')):
            self.assertEqual(actor.http('create','/v1/audio/voices',b'original'),
                             (200,b'original-response'))
        self.assertEqual(actor.requests,1)
        with self.assertRaises(RuntimeError):actor.http('create','/v1/audio/voices',b'original')

    def test_wrong_owner_body_or_path_cannot_relabel_original_exchange(self):
        for options in ({'client':'other'},{'body':b'changed'},{'path':'/other'}):
            actor=self.actor()
            args=dict(path='/v1/audio/voices',body=b'original');args.update(options)
            with self.assertRaises(RuntimeError):actor.http('create',**args)

    @staticmethod
    def original():
        return {
            'result.json':dict(status='FAILED',error=
                'evidence.voice_business(result, self.stage.plan\n'+review.FAILURE,
                counts=dict(starts=1,chats=12,websocket_sessions=0,ingress_starts=0),seconds=99),
            'exit.json':dict(status='COMMAND_FAILED',child_returncode=1,
                compute_backend='nvml',lease_inherited_by_child=True),
            'cleanup.json':dict(remaining=[],model_exit=0,ports_released=True,
                lease_continuously_held=True)}

    def environment(self, records, query='{"status":"OK","returncode":0}\n'):
        lease=self.out/'lease';lease.mkdir(exist_ok=True)
        (lease/'nvml-queries.jsonl').write_text(query)
        with mock.patch.object(review,'read',side_effect=lambda p:records[p.name]), \
             mock.patch.object(review,'sha',return_value='a'*64):
            return review.original_environment(self.out)

    def test_original_assertion_and_clean_environment_are_kept_distinct(self):
        original=self.original();before=copy.deepcopy(original)
        value=self.environment(original)
        self.assertEqual(original,before)
        self.assertEqual(value['original_command_status'],'COMMAND_FAILED')
        self.assertTrue(value['original_failure_preserved'])
        self.assertEqual(value['requests'],12)

    def test_partial_work_env_abort_device_or_cleanup_failure_cannot_be_reused(self):
        for change in (lambda r:r['result.json']['counts'].update(chats=11),
                       lambda r:r['result.json'].update(error='device_error'),
                       lambda r:r['exit.json'].update(status='ENV_ABORT'),
                       lambda r:r['cleanup.json'].update(remaining=[1234]),
                       lambda r:r['cleanup.json'].update(model_exit=1)):
            wrong=self.original();change(wrong)
            with self.assertRaises(RuntimeError):self.environment(wrong)
        with self.assertRaises(RuntimeError):
            self.environment(self.original(),'{"status":"ERROR","returncode":1}\n')

    def test_reuse_cannot_qualify_a_light_profile_or_changed_runtime(self):
        frozen=dict(format=review.FORMAT,files={},capabilities=dict(version='original'),
                    configuration={},receipt='/unused')
        stage=SimpleNamespace(plan=dict(retained_primary_voice=frozen),
                              profile_name='light',out=self.out)
        with self.assertRaises(RuntimeError):review.consume(stage)
        stage.profile_name='primary'
        with mock.patch.object(review,'read',return_value=dict(version='changed')):
            with self.assertRaises(RuntimeError):review.consume(stage)

    def test_continuation_counts_only_remaining_work_and_keeps_time_bound(self):
        primary=capacity.normalize(dict(version=1))
        args=dict(native_asr=registration_asr(),native_tts_base=selected(),realtime=True)
        full=driver.limits_for(primary,**args)
        tail=driver.limits_for(primary,retained_primary_voice=True,**args)
        self.assertEqual(tail['chats'],full['chats']-12)
        self.assertEqual(tail['starts'],2)
        self.assertEqual(tail['seconds'],800)
        self.assertEqual(tail['websocket_sessions'],2)
        with self.assertRaises(RuntimeError):
            driver.limits_for(primary,retained_primary_voice=True)


if __name__ == '__main__':
    unittest.main()
