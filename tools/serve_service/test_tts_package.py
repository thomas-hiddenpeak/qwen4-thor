"""Paired optional TTS package identity and limits; synthetic CPU metadata only."""
import contextlib
import copy
import io
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

from tools.serve_service import package as pkg, service, test_service as old


class TtsPackage(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture

    def selected(self):
        return dict(pkg.NATIVE_TTS, directory=str(self.root/'missing-tts'),
                    limits=dict(pkg.TTS_LIMITS, max_frames=160))

    def tts_fixture(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        declaration = metadata['release_declarations']
        root = Path(declaration['directory'])
        caps = pkg.read(root/'capabilities.json')
        caps['speech'] = dict(pkg.NATIVE_TTS, enabled=True, core_health_dependency=False)
        caps['chat_audio_output'] = dict(enabled=True)
        for key in ('speech', 'chat_audio_output'):
            metadata['chat_protocol'][key] = caps[key]
        config = metadata['observed_configuration']
        config['native_tts_health'] = dict(state='ready', model=pkg.NATIVE_TTS['model'],
                                          core_readiness_dependency=False)
        config['native_tts_limits'] = self.selected()['limits']
        for name, value in [('capabilities.json', caps), ('configuration.json', config)]:
            pkg.save(root/name, value)
            declaration['files'][name] = pkg.sha(root/name)
        pkg.save(release, metadata)
        return binary, release, model

    def test_missing_optional_assets_stay_portable_with_all_budgets(self):
        binary, release, model = self.tts_fixture(); selected = self.selected()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
            tts_model_dir=Path(selected['directory']), tts_limits=dict(max_frames=160))
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertFalse(Path(selected['directory']).exists())
        self.assertEqual(manifest['settings']['native_tts'], selected)
        command = pkg.command(receipt['package'], manifest)
        for key, value in {'model_dir':selected['directory'], **selected['limits']}.items():
            flag = '--tts-'+key.replace('_', '-')
            self.assertEqual(command.count(flag), 1)
            self.assertEqual(command[command.index(flag)+1], str(value))
        shutil.rmtree(Path(pkg.read(release)['release_declarations']['directory']))
        release.unlink();pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_identity_capacity_paths_and_aliases_are_strict(self):
        base = self.selected()
        for path in ['', '/', '/a/', 'relative', '/a/../b', '/a//b', '/a/.', '/a\0b']:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                pkg.settings(8080, 0, native_tts=dict(base, directory=path))
        for field in pkg.NATIVE_TTS:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                pkg.tts_settings(dict(base, **{field:'wrong'}))
        for key, value in [('max_text_tokens', 3), ('max_frames', True),
            ('max_frames', 4097), ('max_prefill_tokens', 32768-159),
            ('workspace_mib', 127), ('max_output_mib', 129), ('unknown', 1)]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                pkg.tts_settings(dict(base, limits=dict(base['limits'], **{key:value})))
        for names in [dict(name=base['model'], aliases=[]),
                      dict(name='chat', aliases=[base['model']])]:
            with self.assertRaisesRegex(RuntimeError, 'conflicts'):
                pkg.settings(8080, 0, model_names=names, native_tts=base)
        self.assertNotIn('native_tts', pkg.settings(8080, 0))
        value = pkg.tts_settings(base);value['limits']['max_frames'] = 2
        self.assertEqual(base['limits']['max_frames'], 160)

    def test_declared_pair_health_and_effective_limits_cannot_drift(self):
        _, release, _ = self.tts_fixture();metadata = pkg.read(release)
        for state in ['ready', 'degraded', 'configured', 'loading', 'metadata_ready']:
            value = copy.deepcopy(metadata)
            value['observed_configuration']['native_tts_health']['state'] = state
            pkg.check_tts_declaration(value, self.selected())
        mutations = [
            lambda d:d['chat_protocol']['speech'].update(enabled=1),
            lambda d:d['chat_protocol']['speech'].update(codec_weight_sha256='0'*64),
            lambda d:d['chat_protocol']['speech'].update(core_health_dependency=True),
            lambda d:d['chat_protocol']['chat_audio_output'].update(enabled=False),
            lambda d:d['observed_configuration']['native_tts_health'].update(state='disabled'),
            lambda d:d['observed_configuration']['native_tts_health'].update(core_readiness_dependency=True),
            lambda d:d['observed_configuration']['native_tts_limits'].update(max_frames=159)]
        for mutate in mutations:
            value = copy.deepcopy(metadata);mutate(value)
            with self.assertRaises(RuntimeError):
                pkg.check_tts_declaration(value, self.selected())

    def test_backend_cannot_be_silently_dropped_or_enabled_without_evidence(self):
        binary, release, model = self.tts_fixture()
        with self.assertRaisesRegex(RuntimeError, 'drops configured native TTS'):
            pkg.prepare(binary, release, model, self.root/'package')
        self.assertFalse((self.root/'package').exists())
        metadata = pkg.read(release)
        metadata['chat_protocol']['speech']['enabled'] = False
        with self.assertRaisesRegex(RuntimeError, 'does not declare'):
            pkg.check_tts_declaration(metadata, self.selected())
        with self.assertRaisesRegex(RuntimeError, 'require a TTS model directory'):
            pkg.prepare(binary, release, model, self.root/'package', tts_limits=dict(max_frames=160))
        self.assertFalse((self.root/'package').exists())

    def test_prepare_cli_forwards_only_explicit_tts_overrides(self):
        args = ['service.py', 'prepare', '--model-dir', '/model', '--output',
            str(self.root/'package'), '--tts-model-dir', '/tts', '--tts-max-frames',
            '160', '--tts-workspace-mib', '2048']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        self.assertEqual(prepare.call_args.kwargs['tts_model_dir'], Path('/tts'))
        self.assertEqual(prepare.call_args.kwargs['tts_limits'],
                         dict(max_frames=160, workspace_mib=2048))


if __name__ == '__main__':unittest.main()
