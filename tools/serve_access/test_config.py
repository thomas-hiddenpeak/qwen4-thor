"""Validate the actual N04 discovery failure and its public schema contract."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_recovery.quota_control import validate_status

ROOT = Path(__file__).resolve().parents[2]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_bytes())
        self.validator = Draft202012Validator(self.schema)
        self.value = json.loads((ROOT/'docs/evidence/serve-access-config-fix-20261109/observed-config.json').read_bytes())

    def test_actual_dynamic_discovery_and_unchanged_legacy_observation(self):
        Draft202012Validator.check_schema(self.schema)
        self.validator.validate(self.value)
        old = json.loads((ROOT/'docs/evidence/serve-cache-io-business-20261109/business-01/configuration.json').read_bytes())
        self.validator.validate(old)
        current = copy.deepcopy(self.value)
        current['current_client_policy'].update(revision=1,publisher_alive=False,
                                               content_sha256='a'*64,clients=['alpha','beta'])
        self.validator.validate(current)
        self.assertEqual(current['client_policy']['revision'],0)

    def test_dynamic_binding_required_fields_and_no_credentials(self):
        for section, field in [('client_policy','snapshot'),('client_policy','revision'),
                ('client_policy','content_sha256'),('current_client_policy','revision'),
                ('current_client_policy','publisher_alive')]:
            value = copy.deepcopy(self.value);value[section].pop(field)
            self.assertFalse(self.validator.is_valid(value),(section,field))
        for section, field, invalid in [('client_policy','mutable',False),
                ('client_policy','snapshot','current'),('client_policy','revision',True),
                ('current_client_policy','clients',['alpha','alpha']),
                ('current_client_policy','clients',['alpha/key']),
                ('current_client_policy','revision',2**53),
                ('current_client_policy','credential_values_redacted',False),
                ('current_client_policy','api_key','secret')]:
            value = copy.deepcopy(self.value);value[section][field] = invalid
            self.assertFalse(self.validator.is_valid(value),(section,field))
        value = copy.deepcopy(self.value);value.pop('current_client_policy')
        self.assertFalse(self.validator.is_valid(value))

    def test_current_policy_schema_matches_private_observation_contract(self):
        current = self.value['current_client_policy']
        validator = Draft202012Validator(self.schema['properties']['current_client_policy'])
        empty = dict(current,configured=False,mutable=False,publisher_alive=False,
                     revision=0,clients=[],sha256='',content_sha256='')
        for valid in (current,empty,dict(current,mutable=False)):
            validator.validate(valid);validate_status(valid)
        for field, invalid in [('revision',-1),('revision',True),('clients',[]),
                              ('global_inflight',0),('global_queued',-1),('sha256','')]:
            value = dict(current,**{field:invalid})
            self.assertFalse(validator.is_valid(value),field)
            with self.assertRaises(ValueError):validate_status(value)
        for field, invalid in [('revision',1),('clients',['alpha']),('mutable',True)]:
            value = dict(empty,**{field:invalid})
            self.assertFalse(validator.is_valid(value),field)
            with self.assertRaises(ValueError):validate_status(value)

    def test_standalone_openapi_contains_the_same_discovery_contract(self):
        from tools.serve_responses.build_contract import standalone_openapi
        from tools.serve_protocol.build_contract import openapi
        expected = standalone_openapi(openapi())
        self.assertEqual(expected,json.loads((ROOT/'docs/serve-openapi-bundle-v1.json').read_bytes()))
        route = expected['paths']['/v1/config']['get']
        self.assertIn('current_client_policy',json.dumps(expected))
        self.assertIn('200',route['responses'])


if __name__ == '__main__':unittest.main()
