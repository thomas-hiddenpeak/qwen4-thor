"""Ordinary JSON policy bounds, without an external gateway installation."""
import copy
import unittest

from tools.serve_gateway import client_policy as cp
from tools.serve_gateway import gateway


class ApplicationCapacityPolicy(unittest.TestCase):
    def test_native_and_optional_gateway_preserve_explicit_lower_limit(self):
        public = dict(version=1, listen_host='127.0.0.1', listen_port=18443,
            server_name='local.test', upstream_port=18124,
            clients=[dict(id='alpha', api_key='a'*40)])
        self.assertEqual(gateway.policy(public)['body_bytes'], 16*1024**2)
        for size in (1024, 16*1024**2, 96*1024**2):
            configured = dict(public, body_bytes=size)
            self.assertEqual(gateway.policy(configured)['body_bytes'], size)
        with self.assertRaises(ValueError):
            gateway.policy(dict(public, body_bytes=96*1024**2+1))

    def test_private_policy_versions_keep_same_numeric_limits_and_digest(self):
        private = dict(version=1, gateway_token='b'*64, clients=[dict(
            id='alpha', max_inflight=1, max_control_inflight=1,
            max_prompt_tokens=512, max_output_tokens=64, max_body_bytes=1024)])
        for version in (1, 2, 3, 4):
            value = copy.deepcopy(private)
            value['version'] = version
            if version >= 2:
                value['fair_queue'] = True
                value['clients'][0].update(requests_per_minute=5,
                                            tokens_per_minute=512)
            if version >= 3:
                value['revision'] = 0
            if version >= 4:
                value['clients'][0]['max_file_body_bytes'] = 1024
            for size in (1024, 16*1024**2, 96*1024**2):
                value['clients'][0]['max_body_bytes'] = size
                self.assertEqual(cp.validate(value), value)
                self.assertEqual(len(cp.digest(value)), 64)
            value['clients'][0]['max_body_bytes'] += 1
            with self.assertRaises(ValueError):
                cp.validate(value)


if __name__ == '__main__':
    unittest.main()
