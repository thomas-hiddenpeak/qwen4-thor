"""Validate actual CPU startup diagnostics and config-v1 compatibility."""
import copy
import json
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


class ApplicationStartupContract(unittest.TestCase):
    def test_actual_native_startup_health_and_negative_contract(self):
        schema = json.loads((ROOT / 'docs/serve-config-v1.schema.json').read_text())
        item = schema['properties']['application_startup']['properties']['responses']
        validator = Draft202012Validator(item)
        output = Path(os.environ['Q4T_APPLICATION_STARTUP_OUTPUT']).read_text()
        marker = '[application-startup-health] '
        actual = [json.loads(line[len(marker):]) for line in output.splitlines()
                  if line.startswith(marker)]
        self.assertEqual([value['state'] for value in actual], ['backoff', 'ready'])
        for value in actual:
            validator.validate(value)
        self.assertEqual(actual[-1]['recoveries'], 1)
        for key, invalid in [('state', 'healthy'), ('readiness_dependency', True),
                             ('attempts', -1), ('failures', '0'),
                             ('retry_after_ms', -1), ('last_failure', 'unknown')]:
            bad = copy.deepcopy(actual[-1])
            bad[key] = invalid
            self.assertFalse(validator.is_valid(bad), key)
        for key in item['required']:
            bad = copy.deepcopy(actual[-1])
            del bad[key]
            self.assertFalse(validator.is_valid(bad), key)

    def test_config_accepts_new_diagnostics_and_historical_absence(self):
        schema = json.loads((ROOT / 'docs/serve-config-v1.schema.json').read_text())
        validator = Draft202012Validator(schema)
        value = json.loads((ROOT / 'docs/evidence/serve-responses-business-20261109'
                            / 'business-01/config.json').read_text())
        validator.validate(value)
        current = json.loads((ROOT / 'docs/evidence/serve-file-integration-business-20261110'
                              / 'config-1.json').read_text())
        validator.validate(current)
        for body, aggregate in [(16, 192), (96, 48), (16, 32), (32, 64)]:
            bad = copy.deepcopy(current)
            bad['limits'].update(body_bytes=body << 20,
                                 aggregate_body_bytes=aggregate << 20)
            self.assertFalse(validator.is_valid(bad))
        item = dict(state='uninitialized', readiness_dependency=False, attempts=0,
                    failures=0, recoveries=0, retry_after_ms=0, last_failure='none')
        value['application_startup'] = {key: copy.deepcopy(item) for key in
                                       ['responses', 'batches', 'response_scanner']}
        value['application_state_health']['auxiliary_cleanup_failures'] = 1
        validator.validate(value)
        bad = copy.deepcopy(value)
        bad['application_state_health']['auxiliary_cleanup_failures'] = -1
        self.assertFalse(validator.is_valid(bad))
        for key in value['application_startup']:
            bad = copy.deepcopy(value)
            del bad['application_startup'][key]
            self.assertFalse(validator.is_valid(bad))


if __name__ == '__main__':
    unittest.main()
