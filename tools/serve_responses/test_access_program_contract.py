"""Native policy identity, sealed configuration and actual response schema."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.serve_gateway import client_policy as cp
from tools.serve_service import package as pkg
from tools.serve_service import test_service as package_tests
from tools.serve_responses.test_contract import errors, wire_values


def clients():
    return dict(version=1, gateway_token='b'*64, clients=[dict(id='reviewer',
        max_inflight=1, max_control_inflight=2, max_prompt_tokens=4096,
        max_output_tokens=128, max_body_bytes=4096)])


def policy():
    return dict(version=1, authority='q4t-local', model='qwen3.8-flash-next',
        cyber_tier='blue', minimum_grant='red', default_selection='model_tier',
        standard_allowed=True, program_instructions='Review defensive evidence.',
        clients=[dict(id='reviewer', grant='red')])


class AccessProgramContract(unittest.TestCase):
    def test_request_selection_exact_shape(self):
        for access in ({}, dict(cyber='standard'), dict(cyber='daybreak_blue'),
                       dict(cyber='daybreak_red')):
            self.assertEqual(errors(dict(input='review', access_programs=access),
                                    'response_create'), [])
        for invalid in (None, [], dict(cyber=None), dict(cyber='unknown'),
                        dict(cyber='standard', grant='red')):
            self.assertTrue(errors(dict(input='review', access_programs=invalid),
                                   'response_create'))

    def test_actual_native_json_and_sse_keep_provenance(self):
        values = [(p, k, v) for p, k, v in wire_values(
            Path(os.environ['Q4T_TEST_OUTPUT_DIR']))
            if p.name.startswith('access-program.')]
        self.assertGreaterEqual(len(values), 3)
        for path, kind, value in values:
            self.assertEqual(errors(value, kind), [], str(path))
        response = next(v for _, k, v in values if k == 'response')
        self.assertEqual(response['access_programs'], dict(cyber='daybreak_blue'))
        self.assertEqual(response['q4t_access_program']['authority'], 'q4t-local')
        self.assertEqual(response['q4t_access_program']['backend_model'], 'cpu')
        self.assertNotIn('NATIVE_AUTHORIZED_REVIEW', json.dumps(response))
        events = [v for _, k, v in values if k == 'response_event']
        self.assertEqual(events[-1]['response'], response)
        self.assertEqual([v['sequence_number'] for v in events], list(range(len(events))))


class AccessProgramPackage(unittest.TestCase):
    fixture = package_tests.PackageTests.fixture

    def setUp(self):
        root = pkg.ROOT/'.q4t-work/serve-access-programs-20261110/package-fixtures'
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=root))

    def test_binding_cannot_claim_cloud_approval_or_invent_clients(self):
        raw = json.dumps(policy()).encode()
        bound = pkg.access_program_binding(raw, clients(), policy()['model'])
        self.assertEqual(bound['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(bound, pkg.access_program_binding(
            raw+b'\n', clients(), policy()['model']))
        for key, value in [('authority', 'openai'), ('model', 'fake-cloud-model'),
            ('version', True), ('version', 1.0), ('cyber_tier', 'standard'),
            ('minimum_grant', 'any'), ('program_instructions', '   '),
            ('program_instructions', 'x'*16385), ('program_instructions', '\0'),
            ('clients', [dict(id='unknown', grant='red')]),
            ('clients', policy()['clients']*2), ('extra', True)]:
            wrong = dict(policy(), **{key: value})
            with self.subTest(key=key, value=str(value)[:40]), \
                    self.assertRaises((RuntimeError, ValueError)):
                pkg.access_program_binding(json.dumps(wrong).encode(), clients(),
                                           policy()['model'])
        for raw in (b'{"version":1,"version":1}', b'\xff', b' '*(65537)):
            with self.assertRaises((RuntimeError, ValueError)):
                pkg.access_program_binding(raw, clients(), policy()['model'])

    def test_private_reader_retains_original_client_parser(self):
        path = self.root/'policy.json'
        pkg.save(path, policy())
        self.assertEqual(cp.read_private(path), path.read_bytes())
        with self.assertRaises(ValueError):
            cp.read(path)  # Generic reader must not weaken the client parser.
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            cp.read_private(path)
        path.chmod(0o600)
        link = self.root/'link'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            cp.read_private(link)
        os.link(path, self.root/'hard')
        with self.assertRaises(ValueError):
            cp.read_private(path)

    def test_sealed_package_survives_source_removal_and_rejects_tampering(self):
        binary, release, model = self.fixture()
        meta = pkg.read(release)
        declaration = Path(meta['release_declarations']['directory'])/'capabilities.json'
        caps = pkg.read(declaration)
        caps['responses'] = dict(access_programs=dict(enabled=True))
        pkg.save(declaration, caps)
        meta['release_declarations']['files']['capabilities.json'] = pkg.sha(declaration)
        pkg.save(release, meta)
        client_file, program_file = self.root/'clients.json', self.root/'program.json'
        pkg.save(client_file, clients())
        pkg.save(program_file, policy())
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            client_policy=client_file, access_program_policy=program_file)
        target = Path(receipt['package'])
        client_file.unlink()
        program_file.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        command = pkg.command(target, manifest)
        self.assertEqual(command[command.index('--access-program-policy-file')+1],
                         str(target/'access-program-policy.json'))
        self.assertEqual(manifest['access_program_policy']['authority'], 'q4t-local')
        self.assertEqual((target/'access-program-policy.json').stat().st_mode & 0o777,
                         0o400)
        (target/'access-program-policy.json').chmod(0o600)
        with (target/'access-program-policy.json').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaises(RuntimeError):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_no_program_configuration_keeps_existing_command(self):
        manifest = dict(model_dir='/read-only-model', settings=pkg.settings(8080, 0))
        self.assertNotIn('--access-program-policy-file', pkg.command('/sealed', manifest))
        binary, release, model = self.fixture()
        path = self.root/'program.json'
        pkg.save(path, policy())
        with self.assertRaises(RuntimeError):
            pkg.prepare(binary, release, model, self.root/'no-client',
                        access_program_policy=path)
        path_clients = self.root/'clients.json'
        pkg.save(path_clients, clients())
        with self.assertRaises(RuntimeError):
            pkg.prepare(binary, release, model, self.root/'no-declaration',
                        client_policy=path_clients, access_program_policy=path)


if __name__ == '__main__':
    unittest.main()
