"""Local real-browser CORS proof. Uses a supplied browser and CPU-only backend."""
import argparse
import copy
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import test_native_access as access

BROWSER = None


class BrowserTests(access.AccessTests):
    def test_browser_cors_json_sse_and_error_feedback(self):
        completed = threading.Event()
        result = {}
        page_requests = []
        api = 'https://127.0.0.1:'+str(self.stack.value['policy']['listen_port'])
        script = '''<body>running<script>
        (async () => {
          const api = API;
          const headers = {'Authorization': 'Bearer '+ 'A'.repeat(40),
                           'Content-Type': 'application/json'};
          const result = {};
          try {
            const response = await fetch(api+'/v1/chat/completions', {
              method:'POST', headers, credentials:'include', body:'{"prompt":"browser"}'});
            result.status = response.status;
            result.json = await response.json();
            result.request_id = response.headers.get('x-request-id');
            const error = await fetch(api+'/v1/models', {credentials:'include'});
            result.error_status = error.status;
            result.error_request_id = error.headers.get('x-request-id');
            result.error = await error.json();
            const stream = await fetch(api+'/v1/chat/completions', {
              method:'POST', headers, credentials:'include', body:'{"stream":true}'});
            result.stream_status = stream.status;
            const reader = stream.body.getReader();
            const part = await reader.read();
            result.sse = new TextDecoder().decode(part.value);
            await reader.cancel();
            try {
              await fetch(api+'/v1/models', {headers:{...headers, 'X-Unapproved':'blocked'},
                                           credentials:'include'});
              result.forbidden_header = 'incorrectly permitted';
            } catch (error) { result.forbidden_header = error.name; }
            result.denied_origin = await new Promise(resolve => {
              const frame = document.createElement('iframe');
              const target = 'http://localhost:'+location.port;
              window.addEventListener('message', event => {
                if (event.origin === target && event.source === frame.contentWindow)
                  resolve(event.data.denied_origin);
              });
              frame.src = target+'/denied-origin';
              document.body.appendChild(frame);
            });
          } catch (error) { result.failure = String(error); }
          document.body.textContent = JSON.stringify(result);
          window.q4t_result = result;
          await fetch('/result', {method:'POST', body:JSON.stringify(result)});
        })();</script>'''.replace('API', json.dumps(api))
        class Page(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                page_requests.append(dict(method='GET', path=self.path,
                                          host=self.headers.get('Host')))
                if self.path == '/denied-origin':
                    body = ('''<script>(async () => {
                      let result = 'incorrectly permitted';
                      try { await fetch(API+'/v1/models', {
                        headers:{Authorization:'Bearer '+'A'.repeat(40)},
                        credentials:'include'}); }
                      catch(error) { result = error.name; }
                      parent.postMessage({denied_origin:result}, '*');
                    })();</script>'''.replace('API', json.dumps(api))).encode()
                else:
                    body = script.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                page_requests.append(dict(method='POST', path=self.path))
                result.update(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(204)
                self.end_headers()
                completed.set()
        page = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
        thread = threading.Thread(target=page.serve_forever, daemon=True)
        thread.start()
        try:
            cors = copy.deepcopy(access.CORS)
            origin = f'http://127.0.0.1:{page.server_port}'
            cors['allowed_origins'] = [origin]
            # Keep the already allocated API port so the served page binds to
            # the same endpoint after replacing the fixture's CORS policy.
            self.replace(cors=cors, listen_port=int(api.rsplit(':', 1)[1]))
            profile = self.assets/'browser-profile'
            events = []
            try:
                # Keep Chromium's AF_UNIX scratch paths below sockaddr_un's
                # limit while retaining all test artifacts under the workspace.
                with tempfile.TemporaryDirectory(prefix='br-', dir=ROOT/'.q4t-work') as scratch, \
                     sync_playwright() as driver:
                    context = driver.chromium.launch_persistent_context(profile,
                        executable_path=BROWSER, headless=True, chromium_sandbox=False,
                        ignore_https_errors=True, timeout=12000,
                        args=['--disable-gpu', '--use-angle=swiftshader', '--disable-webgl',
                            '--disable-accelerated-video-decode',
                            '--disable-accelerated-video-encode', '--no-proxy-server',
                            '--password-store=basic'],
                        env=dict(os.environ, CUDA_VISIBLE_DEVICES='',
                            NVIDIA_VISIBLE_DEVICES='void', TMPDIR=scratch))
                    try:
                        events.append(dict(browser_version=context.browser.version))
                        tab = context.pages[0]
                        tab.on('pageerror', lambda error: events.append(dict(error=str(error))))
                        tab.on('console', lambda message: events.append(dict(console=message.text)))
                        tab.on('requestfailed', lambda request: events.append(dict(
                            url=request.url, failed=request.failure)))
                        tab.goto(origin, wait_until='domcontentloaded', timeout=10000)
                        tab.wait_for_function('window.q4t_result !== undefined', timeout=15000)
                        self.assertTrue(completed.wait(2), 'browser result was not received')
                    finally:
                        context.close()
            finally:
                (self.assets/'browser-progress.json').write_text(json.dumps(dict(
                    page_requests=page_requests, result=result, events=events), indent=2)+'\n')
            (self.assets/'browser-result.json').write_text(json.dumps(result, indent=2)+'\n')
            self.assertNotIn('failure', result)
            self.assertEqual(result['status'], 200)
            self.assertTrue(result['json']['cpu_fixture'])
            self.assertEqual(result['error_status'], 401)
            self.assertTrue(result['error_request_id'].startswith('req-'))
            self.assertEqual(result['error']['error']['code'], 'gateway_unauthorized')
            self.assertEqual(result['stream_status'], 200)
            self.assertIn('data: ', result['sse'])
            self.assertEqual(result['forbidden_header'], 'TypeError')
            self.assertEqual(result['denied_origin'], 'TypeError')
            self.assertTrue(self.stack.stream_disconnected.wait(2))
            self.assertEqual(len(self.stack.requests), 2)
        finally:
            page.shutdown()
            page.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ingress', type=Path, required=True)
    parser.add_argument('--browser', type=Path, required=True)
    args, rest = parser.parse_known_args()
    access.INGRESS, BROWSER = args.ingress.resolve(strict=True), args.browser.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest],
        defaultTest='BrowserTests.test_browser_cors_json_sse_and_error_feedback')
