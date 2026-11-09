"""Fail-sensitive summary oracles and actual SDK identity propagation, CPU only."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

from tools.serve_responses.summary_acceptance import (
    HEADERS, LIMITS, MAX_MODEL_STEPS, ROOT, check_steps, check_summary, requests)


def fixture():
    value = json.loads((ROOT/'docs/evidence/serve-responses-summary-cpu-20261109/summary.json').read_text())
    value['output'][-1]['summary'][0]['text'] = 'Added the two numbers and verified 42.'
    return value


class SummaryAcceptance(unittest.TestCase):
    def test_summary_oracle_rejects_raw_copy_missing_reasoning_and_wrong_usage(self):
        value = fixture()
        derived = check_summary(value, arithmetic=True)
        self.assertIn('42', derived['summary'])
        mutants = []
        for change in ('copy', 'no_raw', 'no_summary', 'wrong_answer', 'usage'):
            bad = copy.deepcopy(value)
            if change == 'copy':
                bad['output'][-1]['summary'][0]['text'] = derived['raw']
            elif change == 'no_raw':
                bad['output'][0]['content'] = []
            elif change == 'no_summary':
                bad['output'][-1]['summary'] = []
            elif change == 'wrong_answer':
                bad['output'][1]['content'][0]['text'] = '43'
            else:
                bad['usage']['total_tokens'] += 1
            mutants.append(bad)
        for bad in mutants:
            with self.assertRaises(RuntimeError):
                check_summary(bad, arithmetic=True)

    def test_request_and_model_step_budget_is_explicit(self):
        self.assertEqual(len(requests()), 8)
        self.assertEqual(LIMITS, dict(starts=2, chats=8, controls=2000, seconds=900))
        self.assertEqual(MAX_MODEL_STEPS, 13)
        value = fixture()
        log = ''.join(f'[sampling] request={value["id"]} choice={i}/1\n' for i in range(2))
        check_steps(log, value, 2)
        for bad in (log.splitlines()[0], log+log, ''):
            with self.assertRaises(RuntimeError):
                check_steps(bad, value, 2)

    def test_official_sdk_bridges_send_synthetic_client_headers_once(self):
        captured = []
        value = fixture()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                captured.append(dict(path=self.path, headers=dict(self.headers),
                                     body=json.loads(body)))
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for bridge in ('run_sdk.mjs', 'run_background_sdk.mjs'):
                command = dict(action='create', request=requests()['concise'],
                    base_url=f'http://127.0.0.1:{server.server_port}/v1', headers=HEADERS,
                    sdk_directory=str(ROOT/'.q4t-work/serve-protocol-20261103/sdk'), timeout_ms=3000)
                env = {k: v for k, v in os.environ.items() if k not in
                       ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
                result = subprocess.run(['node', str(ROOT/'tools/serve_responses'/bridge)],
                    input=json.dumps(command), capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                actual = json.loads(result.stdout)
                self.assertTrue(actual['ok'])
                self.assertEqual(len(actual['exchanges']), 1)
                self.assertEqual(captured[-1]['path'], '/v1/responses')
                headers = {k.lower(): v for k, v in captured[-1]['headers'].items()}
                for key, expected in HEADERS.items():
                    self.assertEqual(headers[key.lower()], expected)
            self.assertEqual(len(captured), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
