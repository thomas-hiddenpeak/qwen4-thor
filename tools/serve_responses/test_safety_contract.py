"""Public safety retrieval, independent of the operator-only publication channel."""
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


class SafetyContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads((ROOT/'docs/serve-openapi-bundle-v1.json').read_text())

    def response(self, path, maximum):
        ops = self.spec['paths'][path]
        self.assertEqual(set(ops), {'get'})
        params = ops['get']['parameters']
        self.assertEqual([(p['name'], p['in'], p['schema']['maxLength']) for p in params],
                         [('id', 'path', maximum)])
        schema = ops['get']['responses']['200']['content']['application/json']['schema']
        return Draft202012Validator(dict(schema, components=self.spec['components']))

    def test_project_alert_requires_real_request_identity_and_boolean_pause_receipt(self):
        validator = self.response('/v1/safety/alerts/{id}', 38)
        record = dict(id='alert_'+'a'*32, object='safety.alert', created_at=1,
                      request_id='req_1', response_id='resp_1', model='qwen3.8-flash-next',
                      request_paused=False, reason=None, error_type='other')
        validator.validate(record)
        validator.validate(dict(record, detailed_explanation='Reviewed explanation'))
        for key in record:
            with self.subTest(key=key):
                incomplete = record.copy()
                del incomplete[key]
                self.assertFalse(validator.is_valid(incomplete))
        for change in [dict(error_type='unknown'), dict(request_paused='false'),
                       dict(entity_identifier='peer')]:
            self.assertFalse(validator.is_valid(dict(record, **change)))

    def test_case_requires_entity_and_reviewed_notice(self):
        validator = self.response('/v1/safety/cases/{id}', 128)
        case = dict(id='C-1', object='safety.case', created_at=1, reason=None,
                    notice=dict(type='warning'), entity_identifier='qa')
        validator.validate(case)
        validator.validate(dict(case, notice=dict(type='deactivation')))
        for change in [dict(notice={}), dict(notice=dict(type='unreviewed')), dict(reason=12)]:
            self.assertFalse(validator.is_valid(dict(case, **change)))

    def test_operator_publication_is_absent_from_public_routes(self):
        public = {p for p in self.spec['paths'] if p.startswith('/v1/safety/')}
        self.assertEqual(public, {'/v1/safety/alerts/{id}', '/v1/safety/cases/{id}'})


if __name__ == '__main__':
    unittest.main()
