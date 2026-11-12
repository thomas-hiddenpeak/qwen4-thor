"""Native Batch HTTP/task/disk and SDK checks; generation is explicit CPU data."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest

from jsonschema import Draft202012Validator
from tools.serve_responses import batches_contract
from tools.serve_responses.test_contract import errors, SCHEMA
from tools.serve_responses.test_files_transport import Stack, ROOT
from tools.serve_recovery.test_native_access import certificates


class BatchTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.certs = cls.output/'batch-api-certs'
        certificates(cls.certs)

    def sdk(self, native):
        server = Stack(self.output/self._testMethodName, self.certs, native=native,
            tls=native, prefix='/batch-test' if native else '', json_limit=65536,
            batch_fixture=True)
        self.addCleanup(server.close)
        url = ('https' if native else 'http')+'://127.0.0.1:'+str(server.port)+server.prefix+'/v1'
        result = subprocess.run(['node','tools/serve_responses/batches_sdk.mjs',url,
            server.key,server.other_key,str(server.directory/'sdk.json')], cwd=ROOT,
            env=dict(os.environ,NODE_EXTRA_CA_CERTS=str(self.certs/'ca.pem')),
            capture_output=True,timeout=45)
        (server.directory/'sdk.stdout').write_bytes(result.stdout)
        (server.directory/'sdk.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        data = json.loads((server.directory/'sdk.json').read_text())
        for row in data['requests']:
            value = row.get('response',{})
            if row['status']!=200:continue
            if value.get('object')=='batch':self.assertFalse(errors(value,'batch_resource'))
            if '/batches' in row['path'] and value.get('object')=='list':
                self.assertFalse(errors(value,'batch_list'))
        if native:self.assertEqual({r['lane'] for r in server.admissions},{'data','control'})
        self.assertIsNone(server.error)

    def test_official_sdk_core_batch_lifecycle(self):self.sdk(False)
    def test_official_sdk_native_tls_prefix_batch_lifecycle(self):self.sdk(True)

    def test_actual_cpp_batch_and_config_schema_and_pinned_source(self):
        data = json.loads((self.output/'batches-contract.json').read_text())
        for key in ['created','completed']:self.assertFalse(errors(data[key],'batch_resource'))
        for key in ['list','empty']:self.assertFalse(errors(data[key],'batch_list'))
        config = json.loads((self.output/'batches-config.json').read_text())
        validator = Draft202012Validator(json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text()))
        self.assertEqual([f'{list(e.path)}: {e.message[:300]}' for e in validator.iter_errors(config)], [])
        for path, value in [(('file_inputs','model_media_items'), 0),
                (('file_inputs','fetch','credential'), 'must not be disclosed'),
                (('runtime','media_limits','animated_images'), True),
                (('limits','batch_workers'), 9)]:
            changed = copy.deepcopy(config)
            target = changed
            for field in path[:-1]:target = target[field]
            target[path[-1]] = value
            self.assertTrue(list(validator.iter_errors(changed)))
        self.assertEqual(config['limits']['body_bytes'],96<<20)
        self.assertEqual(config['limits']['batch_client_model_queue_tokens'],4294967296)
        source = batches_contract.source(ROOT/'.q4t-work/serve-protocol-20261103/sdk')
        self.assertEqual(SCHEMA['x-q4t']['sources_sha256'][source['source']],source['sha256'])
        valid = dict(input_file_id='file_x',endpoint='/v1/responses',completion_window='24h')
        self.assertFalse(errors(valid,'batch_create'))
        for changed in [dict(completion_window='1h'),dict(input_file_id=None),dict(owner='other')]:
            self.assertTrue(errors(dict(valid,**changed),'batch_create'))


if __name__=='__main__':unittest.main()
