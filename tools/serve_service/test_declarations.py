"""CPU publication checks; never load a model or contact a service."""
import copy
import contextlib
import io
from pathlib import Path
import shutil
import unittest
from unittest import mock

from tools.serve_service import package as pkg
from tools.serve_service import test_service as old
from tools.verify.startup_delivery import deploy


class Declarations(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture

    def application_declaration_fixture(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        declaration = metadata['release_declarations']
        root = Path(declaration['directory'])
        caps = pkg.read(root/'capabilities.json')
        for key, profile in [('completions', 'q4t-completions-v1'),
                             ('responses', 'q4t-responses-v1'),
                             ('conversations', 'q4t-conversations-v1'),
                             ('application_state', 'q4t-objects-v1')]:
            caps[key] = dict(profile=profile)
            metadata['chat_protocol'][key] = caps[key]
        pkg.save(root/'capabilities.json', caps)
        declaration['files']['capabilities.json'] = pkg.sha(root/'capabilities.json')
        declaration['profile'] = pkg.APPLICATION_DECLARATION_PROFILE
        for name, source in [('completion_request', 'serve-completion-request-v1.schema.json'),
                             ('completion_response', 'serve-completion-transport-v1.schema.json'),
                             ('application', 'serve-application-v1.schema.json'),
                             ('openapi', 'serve-openapi-bundle-v1.json')]:
            path = root/'schemas'/(name+'.json')
            shutil.copyfile(pkg.ROOT/'docs'/source, path)
            declaration['files']['schemas/'+name+'.json'] = pkg.sha(path)
            metadata['chat_protocol'][name+'_schema'] = str(path)
            metadata['chat_protocol'][name+'_schema_sha256'] = pkg.sha(path)
        pkg.save(release, metadata)
        return binary, release, model

    def test_application_schemas_required_and_portable(self):
        binary, release, model = self.application_declaration_fixture()
        metadata = pkg.read(release)
        root = Path(metadata['release_declarations']['directory'])
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        shutil.rmtree(root)
        release.unlink()
        with mock.patch.object(pkg, 'ROOT', self.root/'absent-source-tree'):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])
        package = Path(receipt['package'])/'declarations/schemas'
        self.assertEqual(pkg.read(package/'application.json')['x-q4t']['sdk_version'], '7.47.0')
        self.assertIn('/v1/responses/{response_id}/cancel', pkg.read(package/'openapi.json')['paths'])

    def test_application_schema_tampering_and_old_completion_profile(self):
        binary, release, model = self.application_declaration_fixture()
        metadata = pkg.read(release)
        declaration = metadata['release_declarations']
        root = Path(declaration['directory'])
        old_profile = copy.deepcopy(metadata)
        old_profile['release_declarations']['profile'] = pkg.COMPLETION_DECLARATION_PROFILE
        for name in ('application', 'openapi'):
            del old_profile['release_declarations']['files']['schemas/'+name+'.json']
        with self.assertRaisesRegex(RuntimeError, 'application declarations required'):
            pkg.verify_declarations(old_profile)
        missing = copy.deepcopy(metadata)
        del missing['chat_protocol']['responses']
        with self.assertRaisesRegex(RuntimeError, 'application capability sections incomplete'):
            pkg.verify_declarations(missing)
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        path = Path(receipt['package'])/'declarations/schemas/application.json'
        path.chmod(0o600)
        path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def application_fixture(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        config = metadata['observed_configuration']
        config['application_state'] = dict(profile='q4t-objects-v1',
                                           **pkg.APPLICATION_LIMITS)
        root = Path(metadata['release_declarations']['directory'])
        pkg.save(root/'configuration.json', config)
        metadata['release_declarations']['files']['configuration.json'] = pkg.sha(
            root/'configuration.json')
        pkg.save(release, metadata)
        return binary, release, model

    def test_application_state_is_declared_bound_and_kept_outside_generations(self):
        binary, release, model = self.application_fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                               application_state=dict(max_client_mib=128))
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['settings']['application_state']['max_client_mib'], 128)
        with self.assertRaisesRegex(RuntimeError, 'deployment directory'):
            pkg.command(receipt['package'], manifest)
        args = pkg.command(receipt['package'], manifest, state_directory=self.root/'state')
        self.assertEqual(args[args.index('--object-store-dir')+1],
                         str(self.root/'state/application-objects'))
        self.assertEqual(args[args.index('--object-max-client-mib')+1], '128')
        self.assertEqual(args[args.index('--object-io-timeout-ms')+1], '2000')
        shutil.rmtree(Path(pkg.read(release)['release_declarations']['directory']))
        release.unlink()
        pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_application_state_override_requires_engine_and_valid_bounded_settings(self):
        binary, release, model = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'does not declare'):
            pkg.prepare(binary, release, model, self.root/'package',
                        application_state=dict(directory='/persistent'))
        self.assertFalse((self.root/'package').exists())
        value = dict(profile='q4t-objects-v1', directory='', **pkg.APPLICATION_LIMITS)
        for changes in [dict(directory='/'), dict(directory='/a/../b'),
            dict(directory='relative'), dict(max_object_mib=33),
            dict(max_client_mib=1), dict(max_total_objects=999),
            dict(io_timeout_ms=30001), dict(max_client_objects=True)]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                pkg.application_settings(dict(value, **changes))
        manifest = dict(settings=pkg.settings(8080, 0, application_state=dict(
            value, directory='/persistent/objects')), model_dir='/model')
        args = pkg.command(self.root, manifest)
        self.assertEqual(args[args.index('--object-store-dir')+1], '/persistent/objects')

    def test_notices_are_copied_verified_and_do_not_need_repository_at_runtime(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['third_party_notices']['files'], list(pkg.NOTICE_FILES))
        for name in pkg.NOTICE_FILES:
            self.assertEqual((target/'licenses'/name).read_bytes(),
                             (pkg.ROOT/'third_party/licenses'/name).read_bytes())
        with mock.patch.object(pkg, 'ROOT', self.root/'absent-source-tree'):
            pkg.verify(target, receipt['manifest_sha256'])
        path = target/'licenses/openai-node/LICENSE'
        path.chmod(0o600)
        path.write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_managed_cli_carries_application_options(self):
        from tools.serve_service import service
        args = ['q4t-managed', 'prepare', '--model-dir', '/model', '--output', '/out',
                '--object-store-dir', '/persistent', '--object-max-total-mib', '512']
        with mock.patch.object(service.sys, 'argv', args), \
             mock.patch.object(service.pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        self.assertEqual(prepare.call_args.kwargs['application_state'],
                         dict(directory='/persistent', max_total_mib=512))

    def test_completion_schemas_travel_with_optional_registry_and_verify_offline(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        root = Path(metadata['release_declarations']['directory'])
        caps = pkg.read(root/'capabilities.json')
        caps['completions'] = dict(profile='q4t-completions-v1')
        metadata['chat_protocol']['completions'] = caps['completions']
        pkg.save(root/'capabilities.json', caps)
        declaration = metadata['release_declarations']
        declaration['files']['capabilities.json'] = pkg.sha(root/'capabilities.json')
        with self.assertRaisesRegex(RuntimeError, 'completion declarations required'):
            pkg.verify_declarations(metadata)
        declaration['profile'] = pkg.COMPLETION_DECLARATION_PROFILE
        for name, source in [('completion_request', 'serve-completion-request-v1'),
                             ('completion_response', 'serve-completion-transport-v1')]:
            path = root/'schemas'/(name+'.json')
            shutil.copyfile(pkg.ROOT/'docs'/(source+'.schema.json'), path)
            declaration['files']['schemas/'+name+'.json'] = pkg.sha(path)
            metadata['chat_protocol'][name+'_schema'] = str(path)
            metadata['chat_protocol'][name+'_schema_sha256'] = pkg.sha(path)
        pkg.save(release, metadata)
        names = dict(name='private/base', aliases=['org/alias'])
        receipt = pkg.prepare(binary, release, model, self.root/'package', model_names=names)
        shutil.rmtree(root)
        release.unlink()
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['settings']['model_names'], names)
        self.assertEqual(pkg.command(receipt['package'], manifest)[-4:],
            ['--served-model-name', 'private/base', '--model-alias', 'org/alias'])
        path = Path(receipt['package'])/'declarations/schemas/completion_response.json'
        path.chmod(0o600)
        path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_stale_shared_capability_rejected_before_package_creation(self):
        binary, release, model = self.fixture()
        original = pkg.read(release)
        for key in pkg.DECLARATION_SECTIONS:
            with self.subTest(section=key):
                changed = copy.deepcopy(original)
                changed['chat_protocol'][key] = {'stale': True}
                pkg.save(release, changed)
                with self.assertRaisesRegex(RuntimeError, 'capability differs'):
                    pkg.prepare(binary, release, model, self.root/'package')
                self.assertFalse((self.root/'package').exists())

    def test_all_schema_references_bound_to_copied_bytes(self):
        _, release, _ = self.fixture()
        original = pkg.read(release)
        for name in pkg.DECLARATION_SCHEMAS:
            for suffix in ('_schema', '_schema_sha256'):
                changed = copy.deepcopy(original)
                changed['chat_protocol'][name+suffix] = 'stale'
                with self.subTest(field=name+suffix), self.assertRaisesRegex(
                        RuntimeError, 'schema differs'):
                    pkg.verify_declarations(changed)

    def test_changed_binary_or_protocol_cannot_inherit_old_observation(self):
        _, release, _ = self.fixture()
        original = pkg.read(release)
        for key in ('binary_sha256', 'protocol_sha256'):
            changed = copy.deepcopy(original)
            owner = changed if key == 'binary_sha256' else changed['chat_protocol']
            owner[key] = 'b'*64
            with self.subTest(field=key), self.assertRaisesRegex(
                    RuntimeError, 'release identity differs'):
                pkg.verify_declarations(changed)

    def test_shared_future_fields_and_json_types_are_checked(self):
        _, release, _ = self.fixture()
        metadata = pkg.read(release)
        root = Path(metadata['release_declarations']['directory'])
        caps = pkg.read(root/'capabilities.json')
        caps['future_field'] = {'supported': True}
        pkg.save(root/'capabilities.json', caps)
        metadata['release_declarations']['files']['capabilities.json'] = pkg.sha(
            root/'capabilities.json')
        metadata['chat_protocol']['future_field'] = {'supported': 1}
        with self.assertRaisesRegex(RuntimeError, 'capability differs'):
            pkg.verify_declarations(metadata)

    def test_inconsistent_configuration_rejected(self):
        _, release, _ = self.fixture()
        original = pkg.read(release)
        root = Path(original['release_declarations']['directory'])
        config = pkg.read(root/'configuration.json')
        for key, value, error in (
                ('protocol_source_sha256', 'b'*64, 'observed identity'),
                ('model', 'different', 'observed identity'),
                ('observability_version', 4, 'observation versions')):
            changed = copy.deepcopy(original)
            pkg.save(root/'configuration.json', dict(config, **{key: value}))
            changed['release_declarations']['files']['configuration.json'] = (
                pkg.sha(root/'configuration.json'))
            with self.subTest(field=key), self.assertRaisesRegex(RuntimeError, error):
                pkg.verify_declarations(changed)

    def test_copied_bundle_survives_original_removal(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        shutil.rmtree(self.root/'declarations')
        release.unlink()
        pkg.verify(receipt['package'], receipt['manifest_sha256'])
        path = Path(receipt['package'])/'declarations/schemas/config.json'
        path.chmod(0o600)
        path.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_missing_profile_rejected_for_new_preparation_not_old_pair(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        del metadata['release_declarations']
        pkg.save(release, metadata)
        deploy.verify_pair(binary, release, pkg.sha(binary))
        with self.assertRaisesRegex(RuntimeError, 'declarations required'):
            pkg.prepare(binary, release, model, self.root/'package')
        self.assertFalse((self.root/'package').exists())

    def test_present_profile_is_checked_by_delivery_pair_verifier(self):
        binary, release, _ = self.fixture()
        metadata = pkg.read(release)
        metadata['chat_protocol']['observability']['version'] = 4
        pkg.save(release, metadata)
        with self.assertRaisesRegex(RuntimeError, 'capability differs'):
            deploy.verify_pair(binary, release, pkg.sha(binary))

    def test_historical_package_without_extension_remains_verifiable(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        metadata = pkg.read(target/'q4t.release.json')
        del metadata['release_declarations']
        pkg.save(target/'q4t.release.json', metadata)
        manifest = pkg.read(target/'manifest.json')
        del manifest['release_declarations']
        for name in pkg.DECLARATION_FILES:
            del manifest['files']['declarations/'+name]
        manifest['files']['q4t.release.json'] = pkg.sha(target/'q4t.release.json')
        pkg.save(target/'manifest.json', manifest)
        pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_stripped_manifest_or_sidecar_marker_rejected(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        metadata = pkg.read(target/'q4t.release.json')
        del metadata['release_declarations']
        pkg.save(target/'q4t.release.json', metadata)
        manifest = pkg.read(target/'manifest.json')
        manifest['files']['q4t.release.json'] = pkg.sha(target/'q4t.release.json')
        pkg.save(target/'manifest.json', manifest)
        with self.assertRaisesRegex(RuntimeError, 'profile differs'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_unknown_profile_missing_inventory_and_symlink_rejected(self):
        _, release, _ = self.fixture()
        original = pkg.read(release)
        changed = copy.deepcopy(original)
        changed['release_declarations']['profile'] = 'unknown'
        with self.assertRaisesRegex(RuntimeError, 'unknown release declaration'):
            pkg.verify_declarations(changed)
        changed = copy.deepcopy(original)
        del changed['release_declarations']['files']['schemas/config.json']
        with self.assertRaisesRegex(RuntimeError, 'inventory differs'):
            pkg.verify_declarations(changed)
        root = Path(original['release_declarations']['directory'])
        path = root/'schemas/config.json'
        path.unlink()
        path.symlink_to(root/'schemas/message.json')
        with self.assertRaisesRegex(RuntimeError, 'symlink declaration file'):
            pkg.verify_declarations(original)


if __name__ == '__main__':
    unittest.main()
