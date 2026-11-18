"""Targeted CPU validator and real SDK/TLS checks; never model qualification."""
import base64
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest

from tools.native_asr import service_acceptance as service
from tools.native_tts import managed_service as managed
from tools.serve_recovery import profile_contract, profile_evidence as evidence
from tools.serve_recovery.test_tts_profiles import retained
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]


class ManagedSpeech(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.value, _ = retained()
        cls.configured = dict(package.NATIVE_TTS, directory='/optional/tts',
                              limits=cls.value['limits'])

    def setUp(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)
        self.plan = dict(scenario='tool_version',
                        native_tts=profile_contract.tts_binding(self.configured))
        self.fixture()

    def save(self, name, value):
        (self.out/name).write_text(json.dumps(value))

    def fixture(self):
        for label in managed.labels(self.plan):
            prefix = label+'-tts'
            self.save(prefix+'-config.wire', dict(native_tts_health=self.value['health'],
                                                  native_tts_limits=self.value['limits']))
            self.save(prefix+'-chat.wire', self.value['chat_response'])
            speech = base64.b64decode(self.value['speech_base64'])
            (self.out/(prefix+'-speech.wire')).write_bytes(speech)
            for kind, request in evidence.tts_requests().items():
                name = prefix+'-'+kind
                self.save(name+'-request.json', request)
                self.save(name+'-http.json', dict(status=200))
                python = (kind == 'speech') == (label == 'tls-before')
                path = '/v1/audio/speech' if kind == 'speech' else '/v1/chat/completions'
                self.save(name+'.sdk.json', dict(ok=True,
                    version='2.8.1' if python else '7.47.0', status=200,
                    bytes=len(speech), sha256=hashlib.sha256(speech).hexdigest(),
                    result=self.value['chat_response'],
                    exchanges=[dict(method='POST', url='https://127.0.0.1:12345'+path)]))

    def check(self):
        return managed.check(self.out, self.plan, self.configured)

    def test_full_retained_outputs_require_both_recovery_phases(self):
        self.assertEqual(set(self.check()), {'primary-tts', 'fallback-tts'})
        (self.out/'fallback-tts-speech.wire').unlink()
        with self.assertRaises(FileNotFoundError): self.check()

    def test_audio_corruption_and_synthesis_usage_are_not_accepted(self):
        path = self.out/'fallback-tts-speech.wire'
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(ValueError): self.check()
        self.fixture()
        bad = copy.deepcopy(self.value['chat_response'])
        bad['usage']['q4t_audio_synthesis']['output_tokens'] = 1
        self.save('fallback-tts-chat.wire', bad)
        with self.assertRaises(ValueError): self.check()

    def test_omitted_binding_or_unhealthy_reload_cannot_reuse_other_phase(self):
        self.plan.pop('native_tts')
        with self.assertRaises(RuntimeError): self.check()
        self.plan['native_tts'] = profile_contract.tts_binding(self.configured)
        bad = dict(native_tts_health=dict(self.value['health'], state='metadata_ready'),
                   native_tts_limits=self.value['limits'])
        self.save('fallback-tts-config.wire', bad)
        with self.assertRaises(ValueError): self.check()
        self.fixture()
        wrong = copy.deepcopy(self.configured);wrong['limits']['max_frames'] += 1
        with self.assertRaises(RuntimeError):managed.check(self.out, self.plan, wrong)

    def test_tls_requires_both_sdk_outputs_and_verified_https_endpoint(self):
        self.plan['scenario'] = 'tls';self.fixture()
        self.assertEqual(set(self.check()), {'tls-before-tts', 'tls-after-tts'})
        path = self.out/'tls-after-tts-chat.sdk.json';value = json.loads(path.read_text())
        value['exchanges'][0]['url'] = 'http://127.0.0.1:12345/v1/chat/completions'
        self.save(path.name, value)
        with self.assertRaises(RuntimeError): self.check()
        self.fixture();value = json.loads(path.read_text())
        value['exchanges'] *= 2;self.save(path.name, value)
        with self.assertRaises(RuntimeError): self.check()

    def test_output_budgets_count_every_phase_and_reject_partial_tls(self):
        for scenario, count in [('tool_retained', 6), ('tool_version', 12), ('tls', 13)]:
            budget = service.limits_for(scenario, True, True)
            self.assertEqual(budget['chats'], count)
            self.assertEqual(budget['starts'], service.LIMITS[scenario]['starts'])
            plan = dict(self.plan, scenario=scenario, chat_audio=True, limits=budget)
            service.configure_plan_limits(plan)
            plan['limits'] = dict(budget, chats=count-1)
            with self.assertRaises(RuntimeError):service.configure_plan_limits(plan)
        with self.assertRaises(RuntimeError):service.limits_for('tls_remaining', False, True)
        with self.assertRaises(RuntimeError):service.limits_for('tls', False, 1)

    def test_absent_tts_keeps_original_asr_only_qualification(self):
        self.assertEqual(managed.check(self.out, dict(scenario='tool_retained'), None), {})
        with self.assertRaises(RuntimeError):managed.check(self.out, self.plan, None)


class SpeechSdkTls(unittest.TestCase):
    def exercise(self, python):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            out = Path(temporary)
            for name in ('server', 'untrusted'):
                subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                    '-nodes', '-days', '1', '-subj', '/CN=127.0.0.1',
                    '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(out/(name+'.key')),
                    '-out', str(out/(name+'.pem'))], check=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10)
            observed = []
            payload = b'CPU-only-SDK-transport-fixture'
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    observed.append((self.path, self.headers.get('Authorization'), body))
                    status = 200 if self.headers.get('Authorization') == 'Bearer fixture-key' else 401
                    self.send_response(status);self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Content-Length', str(len(payload)));self.end_headers()
                    self.wfile.write(payload)
                def log_message(self, *args): pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(out/'server.pem', out/'server.key')
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=.05))
            thread.start()
            try:
                pinned = json.loads((ROOT/'.q4t-work/serve-chat-audio-20261118/tls-service-01/plan.json').read_text())
                command = ([pinned['python_sdk']['python'], '-I', '-B',
                            str(ROOT/'tools/native_tts/http_sdk.py')] if python else
                           [pinned['sdk']['node']['path'], str(ROOT/'tools/native_tts/http_sdk.mjs')])
                request = evidence.tts_requests()['speech']
                for trusted in (True, False):
                    ca = out/('server.pem' if trusted else 'untrusted.pem')
                    config = dict(base_url=f'https://127.0.0.1:{server.server_port}/v1',
                        ca=str(ca), api_key='fixture-key', request=request,
                        output=str(out/('good.wire' if trusted else 'bad.wire')),
                        sdk_directory=pinned['sdk']['sdk_directory'])
                    env = {k:v for k,v in os.environ.items() if k not in
                        ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
                         'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
                    env.update(NODE_EXTRA_CA_CERTS=str(ca), PYTHONDONTWRITEBYTECODE='1')
                    run = subprocess.run(command, input=json.dumps(config), capture_output=True,
                                         text=True, env=env, timeout=20)
                    if trusted:
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertEqual(Path(config['output']).read_bytes(), payload)
                        result = json.loads(run.stdout)
                        self.assertTrue(result['ok']);self.assertEqual(len(result['exchanges']), 1)
                    else:
                        self.assertNotEqual(run.returncode, 0)
                        self.assertFalse(Path(config['output']).exists())
                self.assertEqual(observed, [('/v1/audio/speech', 'Bearer fixture-key', request)])
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_python_sdk_verifies_ca_and_sends_scoped_key(self): self.exercise(True)

    def test_javascript_sdk_verifies_ca_and_sends_scoped_key(self): self.exercise(False)


if __name__ == '__main__': unittest.main()
