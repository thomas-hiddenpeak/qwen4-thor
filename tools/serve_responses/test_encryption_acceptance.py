"""Fail-sensitive encrypted business oracles and actual pinned SDK, CPU only."""
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

from tools.serve_responses.encryption_acceptance import (
    CLIENT, HEADERS, LIMITS, MAX_MODEL_STEPS, ROOT, Stage, check_done_events,
    continuation, encrypted_items, key_receipt, requests)

EVIDENCE = ROOT/'docs/evidence/serve-responses-encryption-protocol-20261109'


def fixture():
    return json.loads((EVIDENCE/'response.json').read_text())


class EncryptionAcceptance(unittest.TestCase):
    def test_oracles_require_opaque_authenticated_items_and_exact_done_events(self):
        value = fixture()
        items = encrypted_items(value)
        key = items[0]['encrypted_content'].split(':')[1]
        encrypted_items(value, key)
        body = continuation(requests()['continuation'], value)
        self.assertNotIn('Compute', json.dumps(body['input']))
        self.assertEqual(len(body['input']), len(value['output']) + 1)
        for item in body['input'][:-1]:
            if item['type'] == 'reasoning':
                self.assertNotIn('content', item)
                self.assertEqual(item['summary'], [])
        events = [json.loads(line[6:]) for line in (EVIDENCE/'response.wire').read_text().splitlines()
                  if line.startswith('data: {')]
        check_done_events(dict(chunks=events, result=value))
        for change in ('missing', 'bad_format', 'wrong_key', 'duplicate'):
            bad = copy.deepcopy(value)
            if change == 'missing':
                del bad['output'][0]['encrypted_content']
            elif change == 'bad_format':
                bad['output'][0]['encrypted_content'] = '17+25 equals 42.'
            elif change == 'wrong_key':
                bad['output'][0]['encrypted_content'] = bad['output'][0]['encrypted_content'].replace(key, '0'*32)
            else:
                bad['output'][-1]['encrypted_content'] = bad['output'][0]['encrypted_content']
            with self.assertRaises(RuntimeError):
                encrypted_items(bad, key)
        altered = copy.deepcopy(events)
        next(e for e in altered if e['type'] == 'response.output_item.done')['item']['encrypted_content'] = 'different'
        with self.assertRaises(RuntimeError):
            check_done_events(dict(chunks=altered, result=value))

    def test_key_receipt_rejects_unknown_commit_missing_active_and_private_material(self):
        receipt = dict(status=200, commit_unknown=False, value=dict(version=1,
            revision=5, active='a'*32, keys=[dict(id='a'*32, created_at=42, uses=4)],
            max_keys=8, max_seals_per_key=1048576))
        self.assertEqual(key_receipt(receipt)['revision'], 5)
        for change in ('unknown', 'status', 'missing_active', 'secret'):
            bad = copy.deepcopy(receipt)
            if change == 'unknown':
                bad['commit_unknown'] = True
            elif change == 'status':
                bad['status'] = 503
            elif change == 'missing_active':
                bad['value']['active'] = 'b'*32
            else:
                bad['value']['keys'][0]['key'] = 'private'
            with self.assertRaises(RuntimeError):
                key_receipt(bad)

    def test_budget_and_accounting_reject_extra_generation_or_missing_usage(self):
        self.assertEqual(len(requests()), LIMITS['chats'])
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 8)
        key = '{client="' + CLIENT + '"}'
        value = fixture()
        usage = value['usage']
        metrics = dict(q4t_requests_total=1, q4t_requests_success_total=1,
            q4t_requests_error_total=0, q4t_prompt_tokens_total=usage['input_tokens'],
            q4t_generation_tokens_total=usage['output_tokens'])
        metrics.update({'q4t_client_rate_admitted_total'+key: 1,
                        'q4t_client_rate_reserved_tokens'+key: 0,
                        'q4t_client_rate_settled_tokens_total'+key: usage['total_tokens']})
        stage = Stage.__new__(Stage)
        stage.drained = lambda _: metrics
        stage.accounting('fixture', [value])
        for field in metrics:
            metrics[field] += 1
            with self.assertRaises(RuntimeError, msg=field):
                stage.accounting('mutant', [value])
            metrics[field] -= 1
        metrics['q4t_requests_total'] += 1
        metrics['q4t_requests_error_total'] += 1
        stage.accounting('rejected-before-execution', [value], rejected=1)

    def test_official_sdk_sends_opaque_context_once_and_consumes_sse_or_auth_error(self):
        captured = []
        wire = (EVIDENCE/'response.wire').read_bytes()
        body = continuation(requests()['continuation'], fixture())

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(dict(headers=dict(self.headers),
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                error = len(captured) == 2
                raw = json.dumps(dict(error=dict(message='Invalid encrypted state',
                    type='invalid_request_error', code='invalid_encrypted_content',
                    param='input[0].encrypted_content'))).encode() if error else wire
                self.send_response(400 if error else 200)
                self.send_header('Content-Type', 'application/json' if error else 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for error in (False, True):
                command = dict(action='create', request=body,
                    base_url=f'http://127.0.0.1:{server.server_port}/v1', headers=HEADERS,
                    sdk_directory=str(ROOT/'.q4t-work/serve-protocol-20261103/sdk'), timeout_ms=3000)
                env = {k: v for k, v in os.environ.items() if k not in
                       ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
                run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_sdk.mjs')],
                    input=json.dumps(command), capture_output=True, text=True, env=env, timeout=10)
                actual = json.loads(run.stdout)
                self.assertFalse(run.stderr)
                self.assertEqual(run.returncode, int(error))
                self.assertEqual(actual['ok'], not error)
                self.assertEqual(len(actual['exchanges']), 1)
                self.assertEqual(captured[-1]['body'], body)
                headers = {k.lower(): v for k, v in captured[-1]['headers'].items()}
                for name, expected in HEADERS.items():
                    self.assertEqual(headers[name.lower()], expected)
                if error:
                    self.assertEqual(actual['error']['status'], 400)
                    self.assertEqual(actual['error']['code'], 'invalid_encrypted_content')
                else:
                    self.assertEqual(base64.b64decode(actual['wire_base64']), wire)
                    check_done_events(actual)
            self.assertEqual(len(captured), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
