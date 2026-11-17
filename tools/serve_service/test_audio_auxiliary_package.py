"""Targeted aligned/VAD package settings; no backend loads or GPU actions."""
import contextlib
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from tools.serve_service import package as pkg, service, test_asr_package as asr


class AudioAuxiliaryPackage(unittest.TestCase):
    setUp = asr.AsrPackage.setUp
    fixture = asr.AsrPackage.fixture
    selected = asr.AsrPackage.selected
    asr_fixture = asr.AsrPackage.asr_fixture

    def auxiliary(self):
        return {name:dict(fixed, directory=str(self.root/('absent-'+name)),
                          limits=dict(defaults))
                for name, (fixed, defaults, _) in pkg.ASR_AUXILIARY.items()}

    def audio_fixture(self):
        binary, release, model = self.asr_fixture()
        metadata = pkg.read(release)
        declarations = metadata['release_declarations']
        root = Path(declarations['directory'])
        caps = pkg.read(root/'capabilities.json')
        config = metadata['observed_configuration']
        for name, selected in self.auxiliary().items():
            caps['transcriptions'][name] = dict(pkg.ASR_AUXILIARY[name][0], configured=True)
            config['native_'+name+'_health'] = dict(state='ready', model=selected['model'],
                                                    core_readiness_dependency=False)
        config['native_alignment_limits'] = dict(pkg.ALIGNER_LIMITS)
        config['native_vad_max_chunks'] = 64
        config['native_speaker_limits'] = dict(pkg.SPEAKER_LIMITS)
        metadata['chat_protocol']['transcriptions'] = caps['transcriptions']
        for name, value in [('capabilities.json', caps), ('configuration.json', config)]:
            pkg.save(root/name, value);declarations['files'][name] = pkg.sha(root/name)
        pkg.save(release, metadata)
        return binary, release, model

    def test_portable_missing_assets_and_every_actual_command_limit(self):
        binary, release, model = self.audio_fixture()
        selected = self.selected()
        auxiliary = self.auxiliary()
        result = pkg.prepare(binary, release, model, self.root/'package',
            asr_model_dir=Path(selected['directory']), asr_limits=dict(max_output_tokens=128),
            asr_auxiliary=auxiliary)
        manifest = pkg.verify(result['package'], result['manifest_sha256'])
        self.assertEqual(manifest['settings']['native_asr'], dict(selected, **auxiliary))
        command = pkg.command(result['package'], manifest)
        for name, entry in auxiliary.items():
            self.assertFalse(Path(entry['directory']).exists())
            prefix = pkg.ASR_AUXILIARY[name][2]
            for key, value in dict(model_dir=entry['directory'], **entry['limits']).items():
                flag = '--'+prefix+'-'+key.replace('_', '-')
                self.assertEqual(command.count(flag), 1)
                self.assertEqual(command[command.index(flag)+1], str(value))

    def test_identity_paths_and_capacity_are_strict_and_copied(self):
        value = dict(self.selected(), **self.auxiliary())
        accepted = pkg.asr_settings(value)
        accepted['alignment']['limits']['max_words'] = 1
        self.assertEqual(value['alignment']['limits']['max_words'], 512)
        for name in pkg.ASR_AUXILIARY:
            for field in pkg.ASR_AUXILIARY[name][0]:
                bad = copy.deepcopy(value);bad[name][field] = 'other'
                with self.assertRaises(RuntimeError): pkg.asr_settings(bad)
            for path in ['relative', '/', '/a/', '/a//b', '/a/../b', '/a\0b']:
                bad = copy.deepcopy(value);bad[name]['directory'] = path
                with self.assertRaises(RuntimeError): pkg.asr_settings(bad)
        for domain, key, invalid in [('alignment','max_audio_seconds',401),
                ('alignment','max_context_tokens',128), ('alignment','max_words',0),
                ('alignment','workspace_mib',True), ('vad','max_chunks',257),
                ('vad','max_chunks',True)]:
            bad = copy.deepcopy(value);bad[domain]['limits'][key] = invalid
            with self.assertRaises(RuntimeError): pkg.asr_settings(bad)
        with self.assertRaises(RuntimeError): pkg.asr_settings(dict(value, unknown={}))

    def test_observed_identity_limits_health_and_no_silent_backend_loss(self):
        _, release, _ = self.audio_fixture()
        metadata = pkg.read(release)
        selected = dict(self.selected(), **self.auxiliary())
        pkg.check_asr_declaration(metadata, selected)
        for name in pkg.ASR_AUXILIARY:
            for field, invalid in [('configured',False), ('configured',1),
                                   ('weight_sha256','0'*64), ('revision','other')]:
                bad = copy.deepcopy(metadata)
                bad['chat_protocol']['transcriptions'][name][field] = invalid
                with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, selected)
            bad = copy.deepcopy(metadata)
            bad['observed_configuration']['native_'+name+'_health']['state'] = 'disabled'
            with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, selected)
            dropped = copy.deepcopy(selected);del dropped[name]
            if name in ('alignment', 'vad'): dropped.pop('speaker', None)
            with self.assertRaisesRegex(RuntimeError, 'drops configured'):
                pkg.check_asr_declaration(metadata, dropped)
        bad = copy.deepcopy(metadata)
        bad['observed_configuration']['native_vad_max_chunks'] = 63
        with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, selected)
        bad = copy.deepcopy(metadata)
        bad['observed_configuration']['native_alignment_limits']['max_words'] = 511
        with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, selected)

    def test_orphan_auxiliary_settings_do_not_prepare_partial_package(self):
        binary, release, model = self.audio_fixture()
        out = self.root/'package'
        with self.assertRaisesRegex(RuntimeError, 'require an ASR model directory'):
            pkg.prepare(binary, release, model, out, asr_auxiliary=self.auxiliary())
        self.assertFalse(out.exists())
        with self.assertRaises(RuntimeError):
            pkg.prepare(binary, release, model, out, asr_auxiliary={'vad':None})
        self.assertFalse(out.exists())

    def test_cli_preserves_explicit_auxiliary_settings_and_rejects_orphan_limits(self):
        args = ['service.py','prepare','--model-dir','/model','--output',str(self.root/'package'),
                '--asr-model-dir','/audio','--aligner-model-dir','/align',
                '--aligner-max-words','128','--vad-model-dir','/vad','--vad-max-chunks','3']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        settings = prepare.call_args.kwargs['asr_auxiliary']
        self.assertEqual(settings['alignment'], dict(pkg.NATIVE_ALIGNMENT, directory='/align',
                         limits=dict(pkg.ALIGNER_LIMITS, max_words=128)))
        self.assertEqual(settings['vad'], dict(pkg.NATIVE_VAD, directory='/vad', limits=dict(max_chunks=3)))
        args = ['service.py','prepare','--model-dir','/model','--output',str(self.root/'package'),
                '--vad-max-chunks','3']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare') as prepare, \
             self.assertRaisesRegex(RuntimeError, 'limits require its model directory'):
            service.main()
        prepare.assert_not_called()

    def test_speaker_requires_its_actual_dependencies_before_creating_package(self):
        binary, release, model = self.audio_fixture()
        for dependency in ('alignment', 'vad'):
            auxiliary = self.auxiliary();del auxiliary[dependency]
            output = self.root/('missing-'+dependency)
            with self.assertRaisesRegex(RuntimeError, 'speaker requires'):
                pkg.prepare(binary, release, model, output,
                    asr_model_dir=Path(self.selected()['directory']), asr_auxiliary=auxiliary)
            self.assertFalse(output.exists())

    def test_speaker_windows_step_and_all_five_capacity_boundaries(self):
        value = dict(self.selected(), **self.auxiliary())
        for name, invalid in [('window_ms',999), ('window_ms',30001),
                ('step_ms',99), ('step_ms',3001), ('max_windows',0),
                ('max_windows',1025), ('max_speakers',0), ('max_speakers',65),
                ('workspace_mib',15), ('workspace_mib',1025), ('window_ms',True)]:
            bad = copy.deepcopy(value);bad['speaker']['limits'][name] = invalid
            with self.subTest(name=name, value=invalid), self.assertRaises(RuntimeError):
                pkg.asr_settings(bad)
        for low in (True, False):
            accepted = copy.deepcopy(value)
            accepted['speaker']['limits'] = dict(window_ms=1000 if low else 30000,
                step_ms=100 if low else 30000, max_windows=1 if low else 1024,
                max_speakers=1 if low else 64, workspace_mib=16 if low else 1024)
            self.assertEqual(pkg.asr_settings(accepted), accepted)

    def test_real_speaker_observation_binds_every_budget_and_isolation(self):
        root = Path(__file__).resolve().parents[2]/'docs/evidence/serve-speaker-http-20261118'
        config = pkg.read(root/'config.json');caps = pkg.read(root/'capabilities.json')
        native = dict(self.selected(), **self.auxiliary())
        native['limits'] = config['native_asr_limits']
        for name in pkg.ASR_AUXILIARY:
            native[name]['limits'] = pkg.asr_auxiliary_observed_limits(config, name)
        metadata = dict(chat_protocol=caps, observed_configuration=config)
        pkg.check_asr_declaration(metadata, native)
        for key in pkg.SPEAKER_LIMITS:
            bad = copy.deepcopy(metadata)
            bad['observed_configuration']['native_speaker_limits'][key] += 1
            with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, native)
        bad = copy.deepcopy(metadata)
        bad['observed_configuration']['native_speaker_health']['core_readiness_dependency'] = True
        with self.assertRaises(RuntimeError): pkg.check_asr_declaration(bad, native)

    def test_speaker_cli_keeps_explicit_settings_and_rejects_detached_limits(self):
        args = ['service.py','prepare','--model-dir','/model','--output',str(self.root/'pack'),
            '--asr-model-dir','/audio','--aligner-model-dir','/align','--vad-model-dir','/vad',
            '--speaker-model-dir','/speaker','--speaker-max-windows','8',
            '--speaker-window-ms','2000','--speaker-step-ms','500',
            '--speaker-max-speakers','4','--speaker-workspace-mib','128']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        entry = prepare.call_args.kwargs['asr_auxiliary']['speaker']
        self.assertEqual(entry, dict(pkg.NATIVE_SPEAKER, directory='/speaker',
            limits=dict(window_ms=2000, step_ms=500, max_windows=8,
                        max_speakers=4, workspace_mib=128)))
        args = args[:args.index('--speaker-model-dir')] + ['--speaker-max-windows','8']
        with mock.patch.object(sys, 'argv', args), mock.patch.object(pkg, 'prepare') as prepare, \
             self.assertRaisesRegex(RuntimeError, 'limits require its model directory'):
            service.main()
        prepare.assert_not_called()

    def test_unconfigured_speaker_does_not_rewrite_legacy_package_settings(self):
        value = dict(self.selected(), **self.auxiliary());del value['speaker']
        accepted = pkg.asr_settings(value)
        self.assertEqual(accepted, value)
        self.assertNotIn('speaker', accepted)
        value['speaker'] = copy.deepcopy(self.auxiliary()['speaker'])
        with mock.patch.object(Path, 'stat', side_effect=AssertionError('must not read optional assets')):
            self.assertEqual(pkg.asr_settings(value), value)


if __name__ == '__main__':
    unittest.main()
