"""Actual engine policy parser and sealed package contracts; no model/GPU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.serve_service import package as pkg
from tools.serve_service.test_service import PackageTests

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('Q4T_POLICY_TEST_BINARY', str(ROOT /
    '.q4t-work/serve-custom-tools-20261108/engine-build/q4t')))


class FileInputPolicyPackage(unittest.TestCase):
    fixture = PackageTests.fixture

    def setUp(self):
        parent = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', str(ROOT /
            '.q4t-work/serve-file-inputs-20261110/policy-package-tests')))
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def policy(self, **overrides):
        value = dict(version=1, max_files=4, summary_output_tokens=128,
            total_summary_output_tokens=256,
            fetch=dict(public_networks=False, allow_http=False,
                allowed_hosts=['private.example'], allowed_networks=['10.2.0.0/16'],
                ca_file='/private/not-opened.pem', ports=[443]))
        value.update(overrides)
        path = self.root/'file-policy.json'
        pkg.save(path, value)
        return path

    def native_fixture(self):
        binary, release, model = self.fixture()
        # The package receipt is explicitly CPU_FIXTURE, never production.
        shutil.copyfile(BINARY, binary)
        binary.chmod(0o500)
        meta = pkg.read(release)
        meta['binary_sha256'] = pkg.sha(binary)
        meta['release_declarations']['binary_sha256'] = pkg.sha(binary)
        path = Path(meta['release_declarations']['directory'])/'capabilities.json'
        caps = pkg.read(path)
        caps['file_inputs'] = dict(policy_profile='q4t-file-input-policy-v1')
        pkg.save(path, caps)
        meta['release_declarations']['files']['capabilities.json'] = pkg.sha(path)
        pkg.save(release, meta)
        return binary, release, model

    def test_native_checker_uses_actual_parser_and_redacts(self):
        policy = self.policy()
        result = subprocess.run([str(BINARY), 'file-input-policy-check', str(policy)],
            capture_output=True, timeout=10, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value['sha256'], pkg.sha(policy))
        self.assertEqual(value['total_summary_output_tokens'], 256)
        self.assertEqual(value['max_files'], 4)
        self.assertTrue(value['fetch']['custom_ca'])
        self.assertFalse(value['request_override'])
        for secret in ('private.example', '/private/', '10.2.0.0'):
            self.assertNotIn(secret, result.stdout.decode())
        self.assertEqual(pkg.file_input_policy_binding(BINARY, policy,
            policy.read_bytes()), value)

    def test_native_rejects_invalid_or_raced_policy_without_fallback(self):
        path = self.policy(total_summary_output_tokens=0)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            pkg.file_input_policy_binding(BINARY, path, path.read_bytes())
        path = self.policy()
        stale = path.read_bytes()
        self.policy(max_files=3)
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            pkg.file_input_policy_binding(BINARY, path, stale)

    def test_sealed_package_survives_removal_and_rejects_tampering(self):
        binary, release, model = self.native_fixture()
        policy = self.policy()
        digest = pkg.sha(policy)
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              file_input_policy=policy)
        target = Path(receipt['package'])
        policy.unlink()
        binary.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['file_input_policy']['sha256'], digest)
        command = pkg.command(target, manifest)
        self.assertEqual(command[command.index('--file-input-policy-file')+1],
                         str(target/'file-input-policy.json'))
        sealed = target/'file-input-policy.json'
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o400)
        self.assertEqual(pkg.sha(sealed), digest)
        sealed.chmod(0o600)
        with sealed.open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_old_declaration_cannot_claim_policy_support(self):
        binary, release, model = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'lacks native file input policy'):
            pkg.prepare(binary, release, model, self.root/'package',
                        file_input_policy=self.policy())
        self.assertFalse((self.root/'package').exists())

    def test_unconfigured_package_keeps_existing_startup(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertNotIn('file_input_policy', manifest)
        self.assertNotIn('--file-input-policy-file', pkg.command(target, manifest))
        self.assertFalse((target/'file-input-policy.json').exists())


if __name__ == '__main__':
    unittest.main()
