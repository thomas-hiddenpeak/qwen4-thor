"""Validate short-driver fixtures/oracles without a model or HTTP listener."""
from dataclasses import dataclass
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator
from tools.serve_clients import short as s


class Short(unittest.TestCase):
    def setUp(self):
        parent = s.BASE/'driver-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.schema = Draft202012Validator(s.pkg.read(s.ROOT/'docs/serve-chat-request-v1.schema.json'))

    def caches(self, counts):
        outer = self
        class Fixture:
            def request(self, label, body, **_):
                outer.schema.validate(body['request'])
                return dict(prompt_tokens=8230)
            def complete(self, label, body, client=0):
                outer.schema.validate(body)
                outer.assertNotIn('messages', body)
                if label == 'cache-alpha-seed':
                    return type('Response', (), dict(message={'content': '1\n2\n3'},
                        usage={'completion_tokens': 8}))()
                number = counts[label]
                return type('Response', (), dict(message={'content': '43'},
                    usage={'prompt_tokens_details': {'cached_tokens': number}}))()
        with mock.patch.object(s, 'OUT', self.root):
            s.Run.caches(Fixture())

    def test_raw_cache_fixture_has_no_parsed_only_fields(self):
        self.caches({'cache-beta-isolated': 0, 'cache-alpha-private': 8236,
                     'cache-alpha-shared': 8192, 'cache-beta-shared': 8192})
        self.assertTrue((self.root/'cache-isolation.json').exists())

    def test_cross_client_hit_and_missing_private_hit_fail(self):
        base = {'cache-beta-isolated': 0, 'cache-alpha-private': 8236,
                'cache-alpha-shared': 8192, 'cache-beta-shared': 8192}
        for key, value in [('cache-beta-isolated', 8192), ('cache-alpha-private', 8192),
                           ('cache-alpha-shared', 0), ('cache-beta-shared', 0)]:
            with self.assertRaisesRegex(RuntimeError, 'namespace'):
                self.caches(dict(base, **{key: value}))

    def test_token_budget_fixtures_use_correct_routes_and_expected_statuses(self):
        seen = []
        outer = self
        class Fixture:
            def request(self, label, body, expected=200, **kwargs):
                seen.append((label, expected, kwargs.get('path', '/v1/chat/completions')))
                req = body.get('request', body)
                outer.schema.validate(req)
                if label == 'missing-gateway':
                    return {'error': {'code': 'client_authentication_failed'}}
                if label.startswith('output-'):
                    outer.assertEqual(req['max_tokens'], 65)
                    return {'error': {'code': 'client_output_limit_exceeded'}}
                if label == 'beta-default':
                    outer.assertNotIn('max_tokens', req)
                    return {'output': {'requested_tokens': 64}}
                if label == 'forged-client':
                    outer.assertEqual(kwargs['headers']['X-Q4T-Client-Id'], 'beta')
                    return {'client_limits': {'max_output_tokens': 512}}
                return {'error': {'code': 'client_prompt_limit_exceeded'}}
        s.Run.limits(Fixture())
        self.assertEqual(len(seen), 7)
        self.assertEqual(sum(status == 400 for _, status, _ in seen), 4)

    def test_short_scope_never_installs_or_starts_day(self):
        self.assertEqual(s.LIMITS, dict(starts=2, generation_attempts=22, controls=120, seconds=600))
        plan = dict(limits=s.LIMITS, port=s.PORT, tls_port=s.TLS_PORT, sources={},
                    default={}, binaries={}, local={}, model_inputs={}, model_dir='unused',
                    day_plans=[str(self.root/'day/plan.json')])
        s.prepared_identity(plan)
        (self.root/'day/execution').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'long run'):
            s.prepared_identity(plan)


if __name__ == '__main__':
    unittest.main()
