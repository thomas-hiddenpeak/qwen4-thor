"""Targeted optional-ASR package contracts; no model or service actions."""
import contextlib
import copy
import io
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

from tools.serve_service import package as pkg, service, test_service as old


class AsrPackage(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture

    def selected(self):
        return dict(pkg.NATIVE_ASR, directory=str(self.root/'missing-audio'),
                    limits=dict(pkg.ASR_LIMITS, max_output_tokens=128))

    def asr_fixture(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        declaration = metadata['release_declarations']
        root = Path(declaration['directory'])
        caps = pkg.read(root/'capabilities.json')
        caps['transcriptions'] = dict(pkg.NATIVE_ASR, configured=True)
        metadata['chat_protocol']['transcriptions'] = caps['transcriptions']
        config = metadata['observed_configuration']
        config['native_asr_health'] = dict(state='ready',
            model=pkg.NATIVE_ASR['model'], core_readiness_dependency=False)
        config['native_asr_limits'] = self.selected()['limits']
        for name, value in [('capabilities.json', caps), ('configuration.json', config)]:
            pkg.save(root/name, value)
            declaration['files'][name] = pkg.sha(root/name)
        pkg.save(release, metadata)
        return binary, release, model

    def test_missing_optional_assets_remain_portable_and_preserve_limits(self):
        binary, release, model = self.asr_fixture()
        selected = self.selected()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            asr_model_dir=Path(selected['directory']),
            asr_limits=dict(max_output_tokens=128))
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertFalse(Path(selected['directory']).exists())
        self.assertEqual(manifest['settings']['native_asr'], selected)
        command = pkg.command(receipt['package'], manifest)
        for key, value in {'model_dir':selected['directory'], **selected['limits']}.items():
            flag = '--asr-'+key.replace('_', '-')
            self.assertEqual(command.count(flag), 1)
            self.assertEqual(command[command.index(flag)+1], str(value))
        shutil.rmtree(Path(pkg.read(release)['release_declarations']['directory']))
        release.unlink()
        pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_identity_limits_paths_and_alias_collisions_fail(self):
        base = self.selected()
        for path in ['', '/', '/a/', 'relative', '/a/../b', '/a//b', '/a/.', '/a\0b']:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                pkg.settings(8080, 0, native_asr=dict(base, directory=path))
        for field in pkg.NATIVE_ASR:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                pkg.asr_settings(dict(base, **{field:'wrong'}))
        for key, value in [('max_audio_seconds', 601), ('max_output_tokens', True),
                           ('max_context_tokens', 128), ('workspace_mib', 63),
                           ('unknown', 1)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                pkg.asr_settings(dict(base, limits=dict(base['limits'], **{key:value})))
        for names in [dict(name=base['model'], aliases=[]),
                      dict(name='chat', aliases=[base['model']])]:
            with self.assertRaisesRegex(RuntimeError, 'conflicts'):
                pkg.settings(8080, 0, model_names=names, native_asr=base)
        self.assertNotIn('native_asr', pkg.settings(8080, 0))
        result = pkg.asr_settings(base)
        result['limits']['max_output_tokens'] = 1
        self.assertEqual(base['limits']['max_output_tokens'], 128)

    def test_declaration_binds_actual_backend_and_effective_limits(self):
        _, release, _ = self.asr_fixture()
        metadata = pkg.read(release)
        pkg.check_asr_declaration(metadata, self.selected())
        for field, value in [('configured', False), ('configured', 1),
                              ('revision', 'other'), ('weight_sha256', '0'*64)]:
            bad = copy.deepcopy(metadata)
            bad['chat_protocol']['transcriptions'][field] = value
            with self.assertRaises(RuntimeError):
                pkg.check_asr_declaration(bad, self.selected())
        for state in ['disabled', None, True]:
            bad = copy.deepcopy(metadata)
            bad['observed_configuration']['native_asr_health']['state'] = state
            with self.assertRaises(RuntimeError):
                pkg.check_asr_declaration(bad, self.selected())
        bad = copy.deepcopy(metadata)
        bad['observed_configuration']['native_asr_limits']['max_output_tokens'] = 127
        with self.assertRaises(RuntimeError):
            pkg.check_asr_declaration(bad, self.selected())

    def test_unqualified_engine_or_orphan_limits_do_not_create_package(self):
        binary, release, model = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'does not declare configured native ASR'):
            pkg.prepare(binary, release, model, self.root/'package',
                        asr_model_dir=Path(self.selected()['directory']))
        self.assertFalse((self.root/'package').exists())
        with self.assertRaisesRegex(RuntimeError, 'require an ASR model directory'):
            pkg.prepare(binary, release, model, self.root/'package',
                        asr_limits=dict(max_output_tokens=128))
        self.assertFalse((self.root/'package').exists())

    def test_prepare_cli_forwards_only_explicit_asr_overrides(self):
        args = ['service.py', 'prepare', '--model-dir', '/model',
                '--output', str(self.root/'package'), '--asr-model-dir', '/audio',
                '--asr-max-output-tokens', '128', '--asr-workspace-mib', '768']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        self.assertEqual(prepare.call_args.kwargs['asr_model_dir'], Path('/audio'))
        self.assertEqual(prepare.call_args.kwargs['asr_limits'],
                         dict(max_output_tokens=128, workspace_mib=768))


if __name__ == '__main__':
    unittest.main()
