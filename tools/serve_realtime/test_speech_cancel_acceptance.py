"""Reject a repair proof that hides the actual cancellation defect."""
import copy
import hashlib
import unittest
from unittest import mock

from tools.serve_realtime import managed, speech_checks
from tools.serve_realtime import speech_cancel_acceptance as stage
from tools.serve_realtime.test_speech_service import fixture
from tools.serve_recovery import profile_speech_evidence


def proof():
    before=dict.fromkeys(managed.METRICS,0);before['free_slots']=1
    held=dict(before,admitted=2,total=2,success=1,settled=1101,reserved=1200,
              active=1,sessions=1,host_bytes=1024,free_slots=0)
    after=dict(before,admitted=2,total=2,success=1,aborted=1,settled=1200)
    final=dict(before,admitted=3,total=3,success=2,aborted=1,settled=2301)
    complete=fixture();complete['responses'][0]['id']='next_response'
    for event in complete['events']:
        if event.get('response_id')=='resp_0':event['response_id']='next_response'
        if 'response' in event:event['response']['id']='next_response'
    return dict(format='q4t-speech-cancellation-repair-v1',automatic_replay=False,
        model_restarts=0,before=before,held=held,after=after,final=final,
        interrupted=fixture('hold_stream'),complete=complete,
        owner_before=dict(pid=123,start_ticks=456),owner_after=dict(pid=123,start_ticks=456),
        configuration=dict(native_tts_health=dict(state='ready',compute_failures=0,
            reason='none',core_readiness_dependency=False,device_bytes=1024)))


class Evidence(unittest.TestCase):
    def setUp(self):
        raw=speech_checks.decode64(fixture()['retrievals'][0]['content'][0]['audio'])
        patch=mock.patch.object(profile_speech_evidence,'PCM',
            dict(builtin=(len(raw),hashlib.sha256(raw).hexdigest())))
        patch.start();self.addCleanup(patch.stop)

    def test_owned_partial_audio_settles_once_and_same_model_completes(self):
        value=proof();old=copy.deepcopy(value)
        result=stage.verify_business(value)
        self.assertEqual(result['cancelled_actual_tokens'],99)
        self.assertEqual(result['request_errors'],0);self.assertEqual(value,old)

    def test_original_error_instead_of_aborted_never_qualifies(self):
        value=proof();value['after'].update(aborted=0,errors=1)
        with self.assertRaises(RuntimeError):stage.verify_business(value)

    def test_partial_without_usage_or_active_reservation_cannot_pass(self):
        for key,new in [('reserved',0),('active',0),('host_bytes',0),('success',0)]:
            value=proof();value['held'][key]=new
            with self.assertRaises(RuntimeError):stage.verify_business(value)
        value=proof();value['after']['settled']=1101
        with self.assertRaises(RuntimeError):stage.verify_business(value)

    def test_hidden_model_restart_or_unhealthy_auxiliary_is_rejected(self):
        for change in [lambda v:v['owner_after'].update(start_ticks=457),
            lambda v:v.update(model_restarts=1),lambda v:v.update(automatic_replay=True),
            lambda v:v['configuration']['native_tts_health'].update(compute_failures=1)]:
            value=proof();change(value)
            with self.assertRaises(RuntimeError):stage.verify_business(value)

    def test_every_real_response_and_ingress_restart_is_budgeted(self):
        self.assertEqual(stage.LIMITS,dict(starts=1,ingress_starts=2,websocket_sessions=2,
            response_creates=3,chats=0,controls=1800,seconds=420))
        plan=dict(schema='q4t-speech-cancellation-repair-v1',limits=stage.LIMITS,
            model_restarts=0,controller_recovery_qualification=False)
        stage.check_plan(plan)
        with self.assertRaises(RuntimeError):stage.check_plan(dict(plan,model_restarts=1))


if __name__=='__main__':unittest.main()
