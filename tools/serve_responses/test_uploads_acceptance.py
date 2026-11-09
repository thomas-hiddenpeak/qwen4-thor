"""New Uploads driver: real SDK bridge and actual document/request oracles."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.serve_responses import uploads_acceptance as driver
from tools.serve_responses.test_files_transport import Stack


class UploadsAcceptanceTests(unittest.TestCase):
    def test_real_document_plan_and_wire_requests(self):
        raw = (driver.ROOT/driver.files.foreground.DOCUMENT).read_bytes()
        created = driver.create_body(raw)
        self.assertEqual(created['bytes'], len(raw))
        self.assertFalse(driver.files.errors(created, 'upload_create'))
        body = driver.completion(['part_a', 'part_a'], raw+raw)
        self.assertFalse(driver.files.errors(body, 'upload_complete'))
        self.assertEqual(body['md5'], hashlib.md5(raw+raw).hexdigest())
        self.assertNotEqual(body['md5'], hashlib.md5(raw).hexdigest())
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=2, files=32, controls=2000, seconds=600))
        self.assertEqual(set(driver.requests()), {'tool', 'follow'})
        for value in driver.requests().values():self.assertFalse(driver.files.errors(value, 'response_create'))

    def test_actual_sdk_bridge_repeat_checksum_reconciliation_and_cancel(self):
        base = Path(os.environ['Q4T_TEST_OUTPUT_DIR']);base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base);self.addCleanup(temp.cleanup)
        directory = Path(temp.name);driver.files.certificates(directory/'tls')
        stack = Stack(directory/'stack', directory/'tls', native=False)
        self.addCleanup(stack.close)
        def call(action, body=None, identity=None, status=200):
            command = dict(action=action, request=body, id=identity,
                base_url=f'http://127.0.0.1:{stack.port}/v1', api_key=stack.key,
                sdk_directory=str(driver.ROOT/'.q4t-work/serve-protocol-20261103/sdk'))
            run = subprocess.run(['node', str(driver.ROOT/'tools/serve_responses/run_uploads_sdk.mjs')],
                input=json.dumps(command), capture_output=True, text=True, timeout=20)
            self.assertFalse(run.stderr, run.stderr)
            value = json.loads(run.stdout)
            evidence = base/'actual-bridge-exchanges.jsonl'
            with evidence.open('a') as record:
                record.write(json.dumps(dict(action=action, expected_status=status, result=value))+'\n')
            driver.files.check_exchange(value, status)
            self.assertEqual(run.returncode, 0 if status == 200 else 1)
            if status == 200:
                shape = 'upload_part_resource' if action == 'part' else 'upload_resource'
                self.assertFalse(driver.files.errors(value['result'], shape))
            return value.get('result')
        raw = b'\x00\xffactual bytes\r\n'
        upload = call('create', driver.create_body(raw+raw))
        part = call('part', driver.part_body(raw), upload['id'])
        body = driver.completion([part['id'], part['id']], raw+raw)
        call('complete', dict(body, md5='0'*32), upload['id'], 400)
        result = call('complete', body, upload['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result, call('complete', body, upload['id']))
        pending = call('create', driver.create_body(raw))
        self.assertEqual(call('cancel', identity=pending['id'])['status'], 'cancelled')
        call('part', driver.part_body(raw), pending['id'], 409)


if __name__ == '__main__':unittest.main()
