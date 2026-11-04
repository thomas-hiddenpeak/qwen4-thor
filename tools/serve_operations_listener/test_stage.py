"""Short driver gates, no process, listener or model start."""
import copy
import unittest
from unittest.mock import patch

from tools.serve_operations_listener import stage as s
from tools.serve_service import package as pkg


class Driver(unittest.TestCase):
    def test_cpu_gate_accepts_only_the_reviewed_diagnostic_repair(self):
        s.cpu_gate()
        sha=s.cpu.sha
        def changed(path):
            return '0'*64 if str(path).endswith('src/server/chat_http.cpp') else sha(path)
        with patch.object(s.cpu,'sha',side_effect=changed),self.assertRaises(RuntimeError):s.cpu_gate()

    def test_candidate_command_matches_managed_profile_and_both_ports(self):
        plan=dict(model_dir='/read-only/model',binary='/candidate/q4t',policy='/private/policy')
        command=s.model_command(plan)
        self.assertEqual(command[0],plan['binary'])
        for key,value in (('--port',s.PORT),('--operations-port',s.OPS_PORT),
                          ('--host','127.0.0.1'),('--client-policy-file',plan['policy']),
                          ('--model-dir',plan['model_dir'])):
            self.assertEqual(command.count(key),1);self.assertEqual(command[command.index(key)+1],str(value))
        for name,value in pkg.PROFILE.items():
            if name not in ('name','host'):
                flag='--'+name.replace('_','-')
                self.assertEqual(command[command.index(flag)+1],str(value))
        self.assertNotIn('--no-mtp',command)

    def test_saturation_cannot_be_inferred_from_ready_or_partial_pressure(self):
        metrics=dict(q4t_public_connections=136,q4t_business_connections=1,
                     q4t_active_chats=1,q4t_operations_listener_enabled=1,q4t_operations_connections=1)
        s.saturation_evidence(metrics)
        for key in metrics:
            bad=dict(metrics,**{key:0})
            with self.assertRaises(RuntimeError):s.saturation_evidence(bad)
        with self.assertRaises(RuntimeError):s.saturation_evidence(dict(metrics,q4t_public_connections=135))

    def test_outputs_preserve_each_client_budget_without_mutating_caller(self):
        run=s.Run.__new__(s.Run);run.current_client=1
        body=dict(max_tokens=512,messages=[dict(role='user',content='test')]);original=copy.deepcopy(body)
        with patch.object(s.s.Run,'complete',return_value='captured') as complete:
            self.assertEqual(run.complete('beta',body),'captured')
            self.assertEqual(complete.call_args.args[1]['max_tokens'],64)
            self.assertEqual(complete.call_args.args[2],1)
            run.complete('alpha',body,0)
            self.assertEqual(complete.call_args.args[1]['max_tokens'],512)
        self.assertEqual(body,original)

    def test_generation_and_long_run_scope_is_bounded(self):
        self.assertEqual(s.LIMITS['starts'],1)
        self.assertEqual(s.LIMITS['generation_attempts'],s.PLANNED['successes']+s.PLANNED['cancelled'])
        self.assertEqual(s.PLANNED['real_tools'],2)
        self.assertLessEqual(s.LIMITS['seconds'],300)
        self.assertEqual(len({s.PORT,s.OPS_PORT,s.TLS_PORT}),3)


if __name__=='__main__':unittest.main()
