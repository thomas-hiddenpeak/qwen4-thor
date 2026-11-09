"""N07 validation boundary checks; no model or network generation."""
import copy
import json
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


if __name__=='__main__':unittest.main()
