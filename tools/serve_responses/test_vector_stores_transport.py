"""Targeted native vector management, real SDK transport and unified schemas."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from tools.serve_responses import vector_stores_contract as contract
from tools.serve_responses.test_contract import errors, SCHEMA
from tools.serve_responses.test_files_transport import Stack, ROOT
from tools.serve_recovery.test_native_access import certificates


class VectorTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.certs = cls.output/'vector-api-certs'
        certificates(cls.certs)

    def sdk(self, python):
        server = Stack(self.output/self._testMethodName, self.certs, native=python,
            tls=python, prefix='/vectors-test' if python else '', json_limit=65536,
            vector_fixture=True)
        self.addCleanup(server.close)
        url = ('https' if python else 'http')+'://127.0.0.1:'+str(server.port)+server.prefix+'/v1'
        command = ([os.environ['Q4T_PYTHON_SDK'],
                    'tools/serve_responses/vector_stores_sdk.py'] if python else
                   ['node', 'tools/serve_responses/vector_stores_sdk.mjs'])
        command += [url, server.key, server.other_key, str(server.directory/'sdk.json')]
        if python:command.append(str(self.certs/'ca.pem'))
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=45)
        (server.directory/'sdk.stdout').write_bytes(result.stdout)
        (server.directory/'sdk.stderr').write_bytes(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        report = json.loads((server.directory/'sdk.json').read_text())
        self.assertEqual(report['status'], 'PASS')
        actual = set()
        for row in report['requests']:
            if '/v1/vector_stores' not in row['path']:continue
            path = row['path'][row['path'].index('/v1/vector_stores'):]
            for route, method, request, _, response in contract.operations():
                if row['method'].lower() != method or not re.fullmatch(
                        re.sub(r'\{[^}]+\}', r'[^/]+', route), path):continue
                actual.add((route, method))
                if row['status'] != 200:break
                self.assertFalse(errors(row['response'], response), (path, response))
                if request:self.assertFalse(errors(row.get('request', {}), request), path)
                break
            else:self.fail('uncatalogued actual route: '+row['path'])
        self.assertEqual(actual, {(route, method) for route, method, *_ in contract.operations()})
        if python:self.assertEqual({r['lane'] for r in server.admissions}, {'data', 'control'})
        self.assertIsNone(server.error)

    def test_javascript_sdk_core_management_and_real_index_content(self):self.sdk(False)
    def test_python_sdk_native_tls_prefix_and_client_isolation(self):self.sdk(True)

    def test_pinned_sources_and_unified_management_shapes(self):
        pinned = contract.source(ROOT/'.q4t-work/serve-protocol-20261103/sdk')
        for path, digest in pinned['sources_sha256'].items():
            self.assertEqual(SCHEMA['x-q4t']['sources_sha256'][path], digest)
        accepted = [
            ('vector_store_create', {'file_ids':['file_a'], 'metadata':None}),
            ('vector_store_update', {'name':None, 'expires_after':None}),
            ('vector_file_update', {'attributes':None}),
            ('vector_batch_create', {'file_ids':['file_a'], 'attributes':{'phase':80}}),
            ('vector_batch_create', {'files':[{'file_id':'file_a'}],
                                     'attributes':'ignored global value'}),
            ('vector_batch_cancel', {}),
            ('vector_file_query', {'limit':1, 'filter':'completed'})]
        for name, value in accepted:self.assertFalse(errors(value, name), name)
        rejected = [
            ('vector_store_create', {'owner':'forged'}),
            ('vector_store_query', {'after':'vs_a','before':'vs_b'}),
            ('vector_store_query', {'limit':0}),
            ('vector_file_query', {'filter':'queued'}),
            ('vector_file_create', {'file_id':'not_a_file'}),
            ('vector_file_update', {'attributes':{'x':None}}),
            ('vector_batch_create', {'file_ids':[]}),
            ('vector_batch_create', {'file_ids':['file_a'], 'files':[]}),
            ('vector_batch_create', {'file_ids':['file_'+str(i) for i in range(2001)]}),
            ('vector_batch_cancel', {'force':True})]
        for name, value in rejected:self.assertTrue(errors(value, name), name)


if __name__ == '__main__':unittest.main()
