"""Direct acceptance gates and additional byte-limit retention boundaries."""
import copy
import time
import unittest
from unittest.mock import patch

from tools.serve_ops import bundle, retention, stage
from tools.serve_ops.test_ops import Files, report
from tools.serve_ops.verify import Run, check_diagnostics


class Driver(Files):
    def observations(self):
        before=report();before['state']='HEALTHY'
        after=copy.deepcopy(before)
        after['log']=dict(state='available',events=[dict(kind='http',status=200)],raw_log_included=False)
        after['comparison']=dict(state='available',counters={
            'q4t_requests_total':3,'q4t_requests_success_total':3,
            'q4t_requests_error_total':0,'q4t_requests_aborted_total':0,
            'q4t_structured_workers_started_total':2,'q4t_structured_workers_reaped_total':2})
        down=dict(state='UNKNOWN',metrics=None,comparison=dict(state='unavailable'))
        return before,after,down

    def test_acceptance_does_not_allow_missing_unknown_replaced_by_zero(self):
        before,after,down=self.observations()
        check_diagnostics(before,after,down,b'safe',['PRIVATE'])
        down['metrics']={}
        with self.assertRaisesRegex(RuntimeError,'stopped service'):
            check_diagnostics(before,after,down,b'safe',['PRIVATE'])

    def test_acceptance_rejects_wrong_counters_and_private_bytes(self):
        before,after,down=self.observations()
        with self.assertRaisesRegex(RuntimeError,'private content'):
            check_diagnostics(before,after,down,b'PRIVATE',['PRIVATE'])
        after['comparison']['counters']['q4t_requests_error_total']=1
        with self.assertRaisesRegex(RuntimeError,'accounting'):
            check_diagnostics(before,after,down,b'safe',['PRIVATE'])

    def test_generation_and_time_budget_cannot_expand(self):
        run=Run.__new__(Run);run.out=self.root;run.deadline=time.time()+10
        run.counts=dict(stage.PLANNED)
        run.budget('generations')
        with self.assertRaisesRegex(RuntimeError,'attempts limit'):run.budget('generations')
        run.deadline=time.time()-1
        with self.assertRaisesRegex(RuntimeError,'deadline'):run.budget()

    def test_runtime_dependency_identity_is_read_only_and_verified(self):
        stage.previous.installed()
        dependencies=stage.dependencies()
        self.assertEqual(dependencies['prometheus']['sha256'],
                         'aeebc0ffc6529c87221ab12ff1f9fc276be488869ebc942ce3e4d7ffcfe6a97a')
        self.assertEqual(stage.previous.previous.previous.prior.sdk_identity()['sdk_version'],'7.47.0')

    def test_cleanup_monitor_failure_is_environment_abort(self):
        run=Run.__new__(Run);run.out=self.root;run.started=time.monotonic();run.counts=dict(generations=0,negatives=0,starts=0)
        def fail():raise RuntimeError('fixture stop before HTTP')
        run.start_model=fail;run.stop_model=lambda:None;run.stop_prometheus=lambda:None
        with patch.object(stage.a,'require_idle_gpu',side_effect=stage.a.EnvironmentAbort('fixture observer unavailable')):
            with self.assertRaises(stage.a.EnvironmentAbort):run.run()
        self.assertEqual(stage.h.read(self.root/'result.json')['status'],'ENV_ABORT')

    def test_retention_byte_limit_is_not_just_a_count_limit(self):
        for name,age in [('old',20),('new',10)]:
            value=report();value['unix_seconds']-=age;value['synthetic_padding']='x'*600000
            bundle.write(self.root/name,value)
        plan=retention.plan(self.root,max_bytes=1048576,max_bundles=100)
        self.assertEqual([row['name'] for row in plan['remove']],['old'])
        self.assertLessEqual(plan['retained_bytes'],1048576)
        retention.apply(self.root,plan)
        self.assertTrue((self.root/'new').exists())


if __name__=='__main__':
    unittest.main()
