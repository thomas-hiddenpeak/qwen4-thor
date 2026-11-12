"""N07 validation boundary checks; no model or network generation."""
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_lifecycle import native_acceptance as a


class NativeDriverTests(unittest.TestCase):
    def test_pending_candidate_is_exact_and_production_rejection_is_preserved(self):
        original=mock.Mock()
        metadata=dict(validation_only=True,acceptance='VALIDATION_ONLY_N07',binary='original')
        decision=dict(status='PENDING_REAL_BUSINESS_N07')
        gate=a.validation_gate(original,dict(metadata=a.fingerprint(metadata),decision=a.fingerprint(decision)))
        gate(metadata,decision);original.assert_not_called()
        for changed in (dict(metadata,binary='different'),dict(metadata,acceptance='ACCEPT_FAKE')):
            with self.assertRaises(ValueError):gate(changed,decision)
        with self.assertRaises(ValueError):gate(metadata,dict(decision,status='PASS_FAKE'))
        gate(dict(validation_only=False),decision);original.assert_called_once()

    def test_live_config_projection_uses_effective_values_and_requires_capacity_identity(self):
        from tools.serve_service import capacity
        config=json.loads((a.ROOT/'docs/evidence/serve-http-business-20261109/configuration.json').read_bytes())
        profile=capacity.normalize(dict(version=1))
        a.check_configuration(profile,config)
        for section,key in [('max_context_tokens','effective'),('request_deadline_ms','effective'),
                            ('default_output_tokens','effective'),('runtime','mtp_k')]:
            wrong=copy.deepcopy(config);wrong[section][key]+=1
            with self.assertRaises((ValueError,RuntimeError)):a.check_configuration(profile,wrong)

    def test_failed_cli_stop_still_runs_owned_cleanup(self):
        stage=a.Stage.__new__(a.Stage);stage.process=mock.Mock();stage.process.poll.return_value=None
        stage.service_id='cpu';stage.cli=mock.Mock(side_effect=RuntimeError('CLI failed'))
        with mock.patch.object(a.common.Stage,'stop') as stop,self.assertRaisesRegex(RuntimeError,'CLI failed'):
            stage.stop()
        stop.assert_called_once()

    def test_successful_cli_stop_waits_before_common_cleanup_and_records_receipt(self):
        stage=a.Stage.__new__(a.Stage);stage.process=mock.Mock();stage.process.poll.return_value=None
        stage.service_id='cpu';stage.output=Path('/cpu-only')
        stage.engine=mock.Mock();stage.cli=mock.Mock(return_value=dict(cleanup_confirmed=True,stop_durable=True))
        with mock.patch.object(a,'save') as save,mock.patch.object(a.common.Stage,'stop') as stop:
            stage.stop()
        stage.process.wait.assert_called_once_with(timeout=10)
        save.assert_called_once();stop.assert_called_once()

    def test_resume_charges_failed_start_and_self_check_and_requires_cleanup(self):
        parent=a.ROOT/'.q4t-work/serve-lifecycle-20261109';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            tmp=Path(tmp)
            counts=dict(starts=1,chats=1,probes=0,controls=42)
            prior=dict(status='FAILED',cleanup_failure=None,budget=dict(counts=counts),
                       seconds=145,package=dict(engine_binary_sha256='a'*64))
            plan=dict(resume_capacity_binding=True,prior_evidence=[],prior_counts=counts,
                      prior_seconds=147,package=prior['package'])
            for name,value in [('execution',prior),('guard',dict(status='COMMAND_FAILED')),
                ('early_stop',dict(status='EARLY_STOP_KNOWN_IMPLEMENTATION_FAILURE')),
                ('stopped',dict(desired='STOPPED',active={'model':None}))]:
                path=tmp/(name+'.json');path.write_text(json.dumps(value));plan['prior_'+name]=str(path)
            a.verify_prior(plan)
            for change in (dict(prior_counts=dict(counts,starts=0)),dict(prior_seconds=140),
                           dict(package=dict(engine_binary_sha256='b'*64))):
                with self.assertRaises(ValueError):a.verify_prior(dict(plan,**change))
            Path(plan['prior_stopped']).write_text(json.dumps(dict(desired='RUNNING',active={})))
            with self.assertRaises(ValueError):a.verify_prior(plan)


if __name__=='__main__':unittest.main()
