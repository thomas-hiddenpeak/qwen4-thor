"""Bounded driver oracles and actual SDK bridge, CPU only."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.serve_responses import files_acceptance as driver
from tools.serve_responses.test_files_transport import Stack


class FilesAcceptanceTests(unittest.TestCase):
    def test_exact_observed_timeout_and_prior_usage_are_not_reinterpreted(self):
        directory = driver.ROOT/'docs/evidence/serve-files-followup-20261110/business-01'
        value = driver.read(directory/'storage-unavailable.sdk.json')
        driver.check_exchange(value, 504, 'object_timeout')
        driver.prior_accounting(directory)
        with self.assertRaises(RuntimeError):driver.check_exchange(value, 503, 'object_timeout')
        for changed in (dict(value, ok=True),
                        dict(value, exchanges=value['exchanges']*2),
                        dict(value, error=dict(value['error'], code='object_io_error'))):
            with self.assertRaises(RuntimeError):driver.check_exchange(changed, 504, 'object_timeout')
        self.assertEqual(driver.RESUME_LIMITS, dict(starts=1, chats=1, files=16, controls=2000, seconds=300))

    def test_download_oracle_requires_original_bytes_size_and_hash(self):
        raw = bytes(range(256))
        value = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                     data_base64=base64.b64encode(raw).decode())
        driver.check_download(value, raw)
        for change in [dict(bytes=0), dict(sha256='0'*64), dict(data_base64='Yg==')]:
            with self.assertRaises(RuntimeError):driver.check_download(dict(value, **change), raw)

    def test_real_document_tool_result_and_frozen_generation_budget(self):
        raw = (driver.ROOT/driver.foreground.DOCUMENT).read_bytes()
        result = driver.tool_result(raw, hashlib.sha256(raw).hexdigest())
        self.assertTrue(result['source']['text'].startswith('1: '+raw.decode().splitlines()[0]))
        self.assertEqual(result['source']['citation'], '[docs/MODEL.md:L1-L3]')
        with self.assertRaises(RuntimeError):driver.tool_result(raw+b'changed', hashlib.sha256(raw).hexdigest())
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=3, files=32, controls=2000, seconds=600))
        for body in driver.requests().values():self.assertFalse(driver.errors(body, 'response_create'))
        self.assertFalse(driver.requests()['healthy']['store'])
        self.assertTrue(driver.requests()['healthy']['stream'])

    def test_actual_sdk_bridge_files_bytes_wire_fields_and_error(self):
        base = Path(os.environ['Q4T_TEST_OUTPUT_DIR']);base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        directory = Path(temp.name);driver.certificates(directory/'tls')
        stack = Stack(directory/'stack', directory/'tls', native=False)
        self.addCleanup(stack.close)
        def call(action, *, body=None, identity=None, status=200):
            command = dict(action=action, request=body, id=identity,
                base_url=f'http://127.0.0.1:{stack.port}/v1', api_key=stack.key,
                sdk_directory=str(driver.ROOT/'.q4t-work/serve-protocol-20261103/sdk'))
            run = subprocess.run(['node', str(driver.ROOT/'tools/serve_responses/run_files_sdk.mjs')],
                input=json.dumps(command), capture_output=True, text=True, timeout=20)
            self.assertFalse(run.stderr, run.stderr)
            value = json.loads(run.stdout)
            self.assertEqual(run.returncode, 0 if status == 200 else 1, value)
            self.assertEqual(len(value['exchanges']), 1)
            self.assertEqual(value['exchanges'][0]['status'], status)
            return value.get('result')
        raw = b'\x00\xffactual document\r\n'
        created = call('create', body=dict(purpose='user_data', filename='真实.bin',
                       data_base64=base64.b64encode(raw).decode()))
        self.assertFalse(driver.errors(created, 'file_resource'))
        listing = call('list')
        self.assertFalse(driver.errors(listing, 'file_list'))
        self.assertEqual(listing['first_id'], created['id'])
        self.assertEqual(call('retrieve', identity=created['id']), created)
        driver.check_download(call('content', identity=created['id']), raw)
        self.assertTrue(call('delete', identity=created['id'])['deleted'])
        call('retrieve', identity=created['id'], status=404)


if __name__ == '__main__':unittest.main()
