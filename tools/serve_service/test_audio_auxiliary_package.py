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


if __name__ == '__main__':
    unittest.main()
