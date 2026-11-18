"""CPU rejection gates for an offline validator correction, not live evidence."""
import copy
import unittest

from tools.native_tts import managed_review as review


class ReviewEnvironment(unittest.TestCase):
    def setUp(self):
        self.plan=dict(scenario='tool_version',native_voice={'model':'CPU'},
            limits=dict(starts=2,chats=24,probes=1,controls=2500,seconds=600),
            package={'package':'/CPU','manifest_sha256':'0'*64},command=['CPU'])
        self.result=dict(status='FAILED',cleanup_failure=None,seconds=200,
            failure="Traceback (most recent call last):\n"
                "save(self.output/'media-usage.json', check_business(self.output))\n"
                'managed_voice.check(out, plan,\n'+review.FAILURE,
            package=self.plan['package'],scenario='tool_version',
            budget=dict(counts=dict(starts=2,chats=24,probes=1,controls=300)))
        self.guard=dict(status='COMMAND_FAILED',child_returncode=1,
            compute_backend='nvml',lease_inherited_by_child=True,
            external_lease=False,command=['CPU'])
        self.queries=[dict(status='OK',returncode=0)]
        self.events=[dict(event='lease_acquired',pid=10,monotonic=10),
            dict(event='lease_released',pid=10,monotonic=212,status='COMMAND_FAILED')]

    def check(self):
        return review.environment(self.plan,self.result,self.guard,self.queries,self.events)

    def test_completed_failed_run_is_explicitly_reviewed_without_relabelling(self):
        before=copy.deepcopy((self.plan,self.result,self.guard,self.queries,self.events))
        result=self.check()
        self.assertEqual(result['original_driver_status'],'FAILED')
        self.assertEqual(result['original_lease_status'],'COMMAND_FAILED')
        self.assertFalse(result['original_continuous_driver_passed'])
        self.assertTrue(result['reviewed_complete_records'])
        self.assertEqual(before,(self.plan,self.result,self.guard,self.queries,self.events))

    def test_no_environment_abort_monitor_gap_or_missing_lease_can_qualify(self):
        for status in ('ENV_ABORT','COMPLETED','UNKNOWN'):
            self.guard['status']=status
            with self.assertRaises(ValueError):self.check()
        self.guard['status']='COMMAND_FAILED'
        self.queries=[]
        with self.assertRaises(ValueError):self.check()
        self.queries=[dict(status='CLEANUP_FAILED',returncode=1)]
        with self.assertRaises(ValueError):self.check()
        self.queries=[dict(status='OK',returncode=0)];self.events.pop()
        with self.assertRaises(ValueError):self.check()

    def test_other_failure_or_failed_cleanup_cannot_reuse_complete_counts(self):
        original=self.result['failure']
        for value in (None,'unrelated '+review.FAILURE,original+'secondary failure',
                      original+original):
            self.result['failure']=value
            with self.assertRaises(ValueError):self.check()
        self.result['failure']=original;self.result['cleanup_failure']='process survived'
        with self.assertRaises(ValueError):self.check()

    def test_incomplete_or_overspent_work_and_time_are_rejected(self):
        counts=self.result['budget']['counts']
        for key in ('starts','chats','probes','controls'):
            original=counts[key]
            for value in (-1,True,self.plan['limits'][key]+1):
                counts[key]=value
                with self.assertRaises(ValueError):self.check()
            if key!='controls':
                counts[key]=original-1
                with self.assertRaises(ValueError):self.check()
            counts[key]=original
        self.result['seconds']=600
        with self.assertRaises(ValueError):self.check()
        self.result['seconds']=200;self.events[-1]['monotonic']=611
        with self.assertRaises(ValueError):self.check()

    def test_different_package_command_or_scenario_cannot_be_reviewed(self):
        self.guard['command']=['different']
        with self.assertRaises(ValueError):self.check()
        self.guard['command']=['CPU'];self.result['package']={'package':'different'}
        with self.assertRaises(ValueError):self.check()
        self.result['package']=self.plan['package'];self.plan['scenario']='tls'
        with self.assertRaises(ValueError):self.check()


if __name__=='__main__':unittest.main()
