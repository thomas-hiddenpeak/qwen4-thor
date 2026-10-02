"""Host-only response/fixture contracts; no HTTP, GPU or service execution."""
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_offload_lifecycle import (Contracts, ROOT, contract_environment,
                                   reference_identity, require_idle_device,
                                   require_reference_identity)


class ResponseContracts(unittest.TestCase):
    def setUp(self):
        work = ROOT / '.q4t-work/offload-lifecycle-host-tests'
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.item = dict(id='fixed', prompt='frozen prompt', max_tokens=128)
        self.reference = dict(id='fixed', prompt_sha256=hashlib.sha256(b'frozen prompt').hexdigest(),
                              actual_input=8243, actual_output=128, finish='length', text='frozen output')
        self.args = Namespace(contract_timeout_s=600, reference_role='same_binary')
        self.runner = Contracts(self.args, Path(self.temp.name), None,
                                [self.item], {'fixed': self.reference})

    def response(self, text='frozen output', actual_input=8243, actual_output=128):
        return dict(status=200, body=json.dumps(dict(
            usage=dict(prompt_tokens=actual_input, completion_tokens=actual_output),
            choices=[dict(message=dict(content=text), finish_reason='length')])))

    def test_same_binary_gate_is_exact_text_usage_finish(self):
        checked = self.runner.compare(self.response(), self.item)
        self.assertTrue(checked['reference_equal'])
        self.assertEqual(checked['text'], 'frozen output')
        self.assertNotIn('bit_exact', checked)
        for response in (self.response(text='different'),
                         self.response(actual_input=8242),
                         self.response(actual_output=127)):
            with self.assertRaises(RuntimeError):
                self.runner.compare(response, self.item)

    def test_historical_differences_remain_explicit(self):
        self.args.reference_role = 'historical_precheck'
        checked = self.runner.compare(self.response(text='new baseline'), self.item)
        self.assertFalse(checked['reference_equal'])
        self.assertEqual(checked['historical_precheck_differences'], ['text'])
        self.assertEqual(checked['text'], 'new baseline')
        with self.assertRaises(RuntimeError):
            self.runner.compare(self.response(actual_input=8242), self.item)

    def test_no_error_response_or_changed_prompt_becomes_reference(self):
        with self.assertRaises(RuntimeError):
            self.runner.compare(dict(status=500, body='failure'), self.item)
        changed = dict(self.item, prompt='altered prompt')
        with self.assertRaises(RuntimeError):
            self.runner.compare(self.response(), changed)

    def test_fixed_http_body_retains_chat_template_boundary(self):
        body = self.runner.body(self.item, request_id='r', cancel_token='token')
        self.assertEqual(body['messages'], [dict(role='user', content='frozen prompt')])
        self.assertEqual(body['max_tokens'], 128)
        self.assertEqual(body['temperature'], 0)
        self.assertFalse(body['stream'])
        self.assertEqual(body['request_id'], 'r')
        self.assertNotIn('prompt', body)

    def test_reference_finish_list_requires_exactly_one_terminal(self):
        self.reference['finish'] = ['length']
        self.assertTrue(self.runner.compare(self.response(), self.item)['reference_equal'])
        self.reference['finish'] = ['length', 'stop']
        with self.assertRaises(ValueError):
            self.runner.compare(self.response(), self.item)

    def test_existing_q4t_rejected_before_gpu_query(self):
        with patch('run_offload_lifecycle.find_pids', return_value=[123]), \
                patch('run_offload_lifecycle.subprocess.run') as query:
            with self.assertRaisesRegex(RuntimeError, 'q4t process'):
                require_idle_device()
            query.assert_not_called()

    def test_gpu_state_must_be_available_and_empty(self):
        with patch('run_offload_lifecycle.find_pids', return_value=[]), \
                patch('run_offload_lifecycle.subprocess.run') as query:
            for code, output in ((0, '456\n'), (1, '')):
                query.return_value = Namespace(returncode=code, stdout=output)
                with self.assertRaisesRegex(RuntimeError, 'unavailable or occupied'):
                    require_idle_device()
            query.return_value = Namespace(returncode=0, stdout='\n')
            require_idle_device()
            self.assertEqual(query.call_args.kwargs['timeout'], 10)

    def test_reference_binds_model_metadata_manifest_and_environment(self):
        root = Path(self.temp.name)
        for name, data in (('config.json', '{}'),
                           ('model.safetensors.index.json', '{"weight_map": {}}'),
                           ('requests.jsonl', 'fixture'), ('manifest.json', 'manifest')):
            (root / name).write_text(data)
        on = contract_environment(1, 'all')
        off = contract_environment(0, 'business')
        identity = reference_identity(root, root / 'requests.jsonl',
                                      root / 'manifest.json', on)
        self.assertEqual(identity, reference_identity(root, root / 'requests.jsonl',
                                                      root / 'manifest.json', off))
        protocol = dict(identity, effective_q4t_environment={k: v for k, v in off.items()
                                                           if k.startswith('Q4T_')})
        require_reference_identity(protocol, identity)
        for field in ('model_dir', 'model_config_sha256', 'model_index_sha256',
                      'fixture_sha256', 'manifest_sha256', 'comparable_q4t_environment'):
            changed = dict(protocol, **{field: 'changed'})
            with self.assertRaisesRegex(ValueError, field):
                require_reference_identity(changed, identity)
        changed = dict(protocol, effective_q4t_environment=dict(
            protocol['effective_q4t_environment'], Q4T_MOE_L2_SLOTS='8'))
        with self.assertRaisesRegex(ValueError, 'effective environment'):
            require_reference_identity(changed, identity)
        (root / 'manifest.json').write_text('changed manifest')
        with self.assertRaisesRegex(ValueError, 'manifest_sha256'):
            require_reference_identity(protocol, reference_identity(
                root, root / 'requests.jsonl', root / 'manifest.json', on))

    def test_environment_scrubs_inherited_experiments(self):
        with patch.dict('run_offload_lifecycle.os.environ',
                        {'Q4T_UNEXPECTED': '1', 'LD_PRELOAD': 'injected.so',
                         'PATH': '/usr/bin'}, clear=True):
            env = contract_environment(1, 'all')
        self.assertNotIn('Q4T_UNEXPECTED', env)
        self.assertNotIn('LD_PRELOAD', env)
        self.assertEqual(env['Q4T_RESIDENCY_FAIL_EXPERT'], 'first')
        self.assertEqual(env['Q4T_MOE_CHUNK_ORDER'], '1')
        self.assertEqual(env['PATH'], '/usr/bin')


if __name__ == '__main__':
    unittest.main()
