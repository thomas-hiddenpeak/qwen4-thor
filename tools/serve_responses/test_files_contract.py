"""Validate actual native Files/config output and its published SDK contract."""
import copy
import json
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import build_contract as api
from tools.serve_responses import files_contract as files
from tools.serve_responses.test_contract import errors, SCHEMA

ROOT = Path(__file__).resolve().parents[2]


class FilesContractTests(unittest.TestCase):
    def test_native_records_and_expiration_match_published_schema(self):
        directory = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        values = json.loads((directory/'files-contract.json').read_text())
        for name, shape in [('evals', 'file_resource'), ('batch', 'file_resource'),
                            ('list', 'file_list'), ('deleted', 'file_deleted')]:
            self.assertFalse(errors(values[name], shape), name)
        self.assertEqual(values['evals']['purpose'], 'evals')
        self.assertNotIn('expires_at', values['evals'])
        self.assertEqual(values['batch']['expires_at']-values['batch']['created_at'], 2592000)
        for field, bad in [('bytes', -1), ('created_at', True),
                           ('purpose', 'unknown'), ('internal_manifest', {})]:
            self.assertTrue(errors(dict(values['evals'], **{field: bad}), 'file_resource'))
        invalid = copy.deepcopy(values['list']);invalid['data'][0]['bytes'] = '1'
        self.assertTrue(errors(invalid, 'file_list'))

    def test_native_file_policy_configuration_matches_full_schema(self):
        value = json.loads((Path(os.environ['Q4T_TEST_OUTPUT_DIR'])/'files-config.json').read_text())
        schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text())
        Draft202012Validator(schema).validate(value)
        self.assertEqual(value['client_governance']['policy_version'], 4)
        self.assertTrue(value['client_policy']['mutable'])

    def test_sdk_source_multipart_binary_and_route_contracts(self):
        source = files.source(ROOT/'.q4t-work/serve-protocol-20261103/sdk')
        self.assertEqual(source['sha256'], files.SDK_FILES_SHA256)
        self.assertEqual(SCHEMA['x-q4t']['sources_sha256']['src/resources/files.ts'], source['sha256'])
        paths = api.openapi()['paths']
        upload = paths['/v1/files']['post']
        self.assertEqual(set(upload['requestBody']['content']), {'multipart/form-data'})
        content = paths['/v1/files/{file_id}/content']['get']['responses']['200']['content']
        self.assertEqual(set(content), {'application/octet-stream'})
        self.assertEqual(upload['x-q4t-limits']['wire_bytes'], files.WIRE_BYTES)
        self.assertIn('delete', paths['/v1/files/{file_id}'])
        self.assertEqual({p['name'] for p in paths['/v1/files']['get']['parameters']},
                         {'purpose', 'after', 'limit', 'order'})

    def test_closed_upload_fields_and_pagination_boundaries(self):
        for purpose in files.PURPOSES:
            self.assertFalse(errors(dict(file='raw', purpose=purpose), 'file_create'))
        self.assertFalse(errors(dict(file='raw', purpose='batch',
            expires_after=dict(anchor='created_at', seconds=3600)), 'file_create'))
        self.assertTrue(errors(dict(file='raw', purpose='batch',
            expires_after=dict(anchor='created_at', seconds=3599)), 'file_create'))
        form = {'file': 'raw', 'purpose': 'evals', 'expires_after[anchor]': 'created_at'}
        self.assertTrue(errors(form, 'file_multipart'))
        self.assertFalse(errors(dict(form, **{'expires_after[seconds]': '3600'}), 'file_multipart'))
        self.assertTrue(errors(dict(form, **{'expires_after[seconds]': '1e4'}), 'file_multipart'))
        self.assertFalse(errors(dict(limit=10000, purpose='unknown', order='desc'), 'file_query'))
        self.assertTrue(errors(dict(limit=10001), 'file_query'))
        self.assertTrue(errors(dict(limit=0), 'file_query'))
        self.assertTrue(errors(dict(before='file_a'), 'file_query'))


if __name__ == '__main__':
    unittest.main()
