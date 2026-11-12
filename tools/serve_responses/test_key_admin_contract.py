"""Targeted native key operator-channel discovery and source bundle contract."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import openapi
from tools.serve_responses.build_contract import standalone_openapi

ROOT = Path(__file__).resolve().parents[2]


class KeyAdminContract(unittest.TestCase):
    def test_optional_channel_status_is_typed_and_never_readiness_dependency(self):
        schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text())
        validator = Draft202012Validator(schema)
        old = json.loads((ROOT/'docs/evidence/serve-access-config-fix-20261109/observed-config.json').read_text())
        validator.validate(old)
        for ready in (True, False):
            value = copy.deepcopy(old)
            value['application_state_health'].update(
                key_admin_ready=ready, key_admin_readiness_dependency=False)
            validator.validate(value)
            for key, invalid in [('key_admin_ready', 1),
                                 ('key_admin_readiness_dependency', True),
                                 ('key_material', 'secret')]:
                bad = copy.deepcopy(value)
                bad['application_state_health'][key] = invalid
                self.assertFalse(validator.is_valid(bad), key)
            for key in ('key_admin_ready', 'key_admin_readiness_dependency'):
                bad = copy.deepcopy(value)
                bad['application_state_health'].pop(key)
                self.assertFalse(validator.is_valid(bad), key)

    def test_discovery_is_in_standalone_openapi_without_external_references(self):
        expected = standalone_openapi(openapi())
        self.assertEqual(expected, json.loads(
            (ROOT/'docs/serve-openapi-bundle-v1.json').read_text()))
        fields = expected['components']['schemas']['serve-config-v1.schema.json'][
            'properties']['application_state_health']['properties']
        self.assertEqual(fields['key_admin_ready'], dict(type='boolean'))
        self.assertEqual(fields['key_admin_readiness_dependency'], dict(const=False))


if __name__ == '__main__':
    unittest.main()
