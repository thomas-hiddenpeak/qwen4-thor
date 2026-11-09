"""Published Uploads shapes must match actual native outputs and pinned SDK."""
import copy
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses import uploads_contract as uploads
from tools.serve_responses.test_contract import errors, SCHEMA
from tools.serve_protocol import build_contract as api
from tools.serve_gateway import gateway

ROOT = Path(__file__).resolve().parents[2]


class UploadsContractTests(unittest.TestCase):
    def test_actual_native_resources_match_published_schema(self):
        values = json.loads((Path(os.environ['Q4T_TEST_OUTPUT_DIR'])/'uploads-contract.json').read_text())
        for name, shape in [('created', 'upload_resource'), ('part', 'upload_part_resource'),
                            ('completed', 'upload_resource'), ('cancelled', 'upload_resource')]:
            self.assertFalse(errors(values[name], shape), name)
        invalid = copy.deepcopy(values['completed']);invalid['file']['bytes'] = '3'
        self.assertTrue(errors(invalid, 'upload_resource'))
        self.assertEqual(values['completed']['file']['bytes'], 3)
        caps = values['capabilities']['uploads']
        self.assertEqual(caps['max_bytes'], uploads.UPLOAD_BYTES)
        self.assertEqual(caps['max_part_bytes'], uploads.PART_BYTES)
        self.assertFalse(caps['core_readiness_dependency'])

    def test_pinned_sdk_and_actual_json_multipart_routes(self):
        source = uploads.source(ROOT/'.q4t-work/serve-protocol-20261103/sdk')
        for name, digest in source['sources_sha256'].items():
            self.assertEqual(SCHEMA['x-q4t']['sources_sha256'][name], digest)
        paths = api.openapi()['paths']
        self.assertNotIn('get', paths['/v1/uploads'])
        for path, media in [('/v1/uploads', 'application/json'),
                            ('/v1/uploads/{upload_id}/complete', 'application/json'),
                            ('/v1/uploads/{upload_id}/cancel', 'application/json'),
                            ('/v1/uploads/{upload_id}/parts', 'multipart/form-data')]:
            row = paths[path]['post']
            self.assertEqual(set(row['requestBody']['content']), {media})
            self.assertEqual(row['requestBody']['required'], not path.endswith('/cancel'))
            self.assertIn(('POST', path, 'control' if path.endswith('/cancel') else 'data'), gateway.APPLICATION_ROUTES)
        gateway.validate_routes()

    def test_closed_control_fields_nulls_capacities_and_optional_checksum(self):
        valid = dict(bytes=0, filename='x.txt', mime_type='text/plain', purpose='user_data')
        self.assertFalse(errors(valid, 'upload_create'))
        for change in (dict(bytes=True), dict(bytes=-1), dict(bytes=uploads.UPLOAD_BYTES+1),
                       dict(purpose='batch_output'), dict(expires_after=None), dict(extra=1)):
            self.assertTrue(errors(dict(valid, **change), 'upload_create'))
        self.assertFalse(errors(dict(part_ids=[]), 'upload_complete'))
        self.assertFalse(errors(dict(part_ids=['part_a', 'part_a'], md5='A'*32), 'upload_complete'))
        for value in (dict(part_ids=None), dict(part_ids=['../private']),
                      dict(part_ids=[], md5=None), dict(part_ids=[], md5='z'*32)):
            self.assertTrue(errors(value, 'upload_complete'))
        self.assertFalse(errors(dict(data='binary'), 'upload_part_create'))
        self.assertTrue(errors(dict(data='binary', file='wrong'), 'upload_part_create'))
