"""Final-driver contracts without listeners, model starts or GPU work."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tools.serve_client_governance import completion as c


def disabled_config():
    value = c.cpu.read(c.prior.OUTPUT/'business/scoped-model-config-wire.bin')
    value['client_policy'].update(enabled=False, clients=0, sha256='')
    value['client_governance'].update(fair_queue=False, policy_version=1)
    value['limits']['queue_policy'] = 'capacity_fifo_best_fit'
    return value


class CompletionTests(unittest.TestCase):
    def test_disabled_contract_accepts_explicit_off(self):
        c.validate_disabled(disabled_config())

    def test_scoped_or_ambiguous_configuration_is_rejected(self):
        for section, key, value in [('client_policy', 'enabled', True),
                                    ('client_policy', 'enabled', 0),
                                    ('client_policy', 'sha256', 'a'*64),
                                    ('client_governance', 'fair_queue', True),
                                    ('client_governance', 'fair_queue', 0),
                                    ('client_governance', 'policy_version', 2),
                                    ('limits', 'queue_policy', 'client_round_robin_best_fit')]:
            wrong = disabled_config()
            wrong[section][key] = value
            with self.assertRaises((RuntimeError, ValueError)):
                c.validate_disabled(wrong)

    def test_one_additional_start_keeps_original_request_time_caps(self):
        old = c.cpu.read(c.PRIOR_ARCHIVE/'result.json')
        self.assertEqual(c.LIMITS['starts'], 1)
        self.assertEqual(old['models']+c.LIMITS['starts'], c.CUMULATIVE['starts'])
        self.assertEqual(c.LIMITS['generation_attempts'], 2)
        self.assertLessEqual(old['generation_attempts']+2, 24)
        self.assertLess(old['cumulative_lease_seconds']+c.LIMITS['seconds'], 480)
        self.assertLessEqual(28+c.LIMITS['controls'], 250)

    def test_launch_uses_real_helper_without_client_policy_or_gateway(self):
        parent = c.BASE/'fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as raw:
            folder = Path(raw)
            run = c.Run.__new__(c.Run)
            run.plan = dict(binary='/read-only/candidate', model_dir='/read-only/model',
                            protocol=disabled_config()['protocol_source_sha256'])
            run.budget = mock.Mock()
            run.children = mock.Mock()
            def launch(path, argv, env):
                path.mkdir()
                (path/'stderr.log').write_text('serving on port\n')
                return object()
            run.children.launch.side_effect = launch
            run.request = mock.Mock(return_value=disabled_config())
            with mock.patch.object(c.s, 'OUT', folder), mock.patch.object(c.s, 'PORT', c.PORT):
                run.launch(False)
            argv = run.children.launch.call_args.args[1]
            self.assertNotIn('--client-policy-file', argv)
            self.assertEqual(argv[0:2], ['/read-only/candidate', 'serve'])
            self.assertEqual(argv[argv.index('--max-len')+1], '8192')
            self.assertEqual(argv[argv.index('--max-seq')+1], '1')
            self.assertEqual(run.children.launch.call_count, 1)
            run.budget.assert_called_once_with('starts')

    def test_historical_default_redirect_is_exact_and_restored(self):
        expected = c.cpu.read(c.prior.PACKAGE/'plan.json')['old_hashes']
        foreign = {'q4t': 'unknown'}
        def verify():
            c.a.verify_hashes(c.ROOT/'build', expected)
            c.a.verify_hashes(c.ROOT/'build', foreign)
        with mock.patch.object(c.a, 'verify_hashes') as actual, \
             mock.patch.object(c.old_archive, 'verify', side_effect=verify):
            c.verify_prior()
            self.assertIs(c.a.verify_hashes, actual)
            self.assertEqual(actual.call_args_list, [
                mock.call(c.prior.PACKAGE/'previous', expected), mock.call(c.ROOT/'build', foreign)])

    def test_historical_failure_restores_live_checker(self):
        with mock.patch.object(c.a, 'verify_hashes') as actual, \
             mock.patch.object(c.old_archive, 'verify', side_effect=RuntimeError('evidence changed')):
            with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
                c.verify_prior()
            self.assertIs(c.a.verify_hashes, actual)
