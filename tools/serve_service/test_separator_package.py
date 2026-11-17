"""Portable separator settings and preserved backend identity; CPU only."""
import contextlib
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from tools.serve_service import package as pkg, service
from tools.serve_service import test_audio_auxiliary_package as old


class SeparatorPackage(unittest.TestCase):
    setUp = old.AudioAuxiliaryPackage.setUp
    fixture = old.AudioAuxiliaryPackage.fixture
    selected = old.AudioAuxiliaryPackage.selected
    asr_fixture = old.AudioAuxiliaryPackage.asr_fixture

    def auxiliary(self):
        return {name:dict(fixed,directory=str(self.root/('absent-'+name)),
                          limits=dict(defaults))
                for name,(fixed,defaults,_) in pkg.ASR_AUXILIARY.items()}

    def audio_fixture(self):
        binary, release, model = old.AudioAuxiliaryPackage.audio_fixture(self)
        metadata = pkg.read(release)
        config = metadata['observed_configuration']
        config['native_separator_limits'] = dict(pkg.SEPARATOR_LIMITS)
        declarations = metadata['release_declarations']
        path = Path(declarations['directory'])/'configuration.json'
        pkg.save(path,config);declarations['files'][path.name] = pkg.sha(path)
        pkg.save(release,metadata)
        return binary,release,model

    def test_portable_package_has_exact_separator_command_without_asset_reads(self):
        binary,release,model = self.audio_fixture()
        auxiliary = self.auxiliary()
        result = pkg.prepare(binary,release,model,self.root/'package',
            asr_model_dir=Path(self.selected()['directory']),
            asr_limits=dict(max_output_tokens=128),asr_auxiliary=auxiliary)
        manifest = pkg.verify(result['package'],result['manifest_sha256'])
        self.assertEqual(manifest['settings']['native_asr']['separator'],auxiliary['separator'])
        command = pkg.command(result['package'],manifest)
        for name,value in dict(model_dir=auxiliary['separator']['directory'],
                               **pkg.SEPARATOR_LIMITS).items():
            flag = '--separator-'+name.replace('_','-')
            self.assertEqual(command.count(flag),1)
            self.assertEqual(command[command.index(flag)+1],str(value))
        self.assertFalse(Path(auxiliary['separator']['directory']).exists())

    def test_all_dependencies_and_capacity_types_are_required(self):
        original = self.auxiliary()
        for name in ('alignment','vad','speaker'):
            value = copy.deepcopy(original);del value[name]
            with self.assertRaises(RuntimeError):pkg.asr_auxiliary_settings(value)
        for key,bad in [('max_audio_seconds',0),('max_audio_seconds',31),
                        ('max_audio_seconds',True),('workspace_mib',63),
                        ('workspace_mib',2049),('workspace_mib',True)]:
            value = copy.deepcopy(original);value['separator']['limits'][key] = bad
            with self.assertRaises(RuntimeError):pkg.asr_auxiliary_settings(value)
        value = pkg.asr_auxiliary_settings(original)
        value['separator']['limits']['max_audio_seconds'] = 1
        self.assertEqual(original['separator']['limits']['max_audio_seconds'],30)

    def test_identity_budget_and_configured_backend_cannot_disappear(self):
        _,release,_ = self.audio_fixture();metadata = pkg.read(release)
        selected = dict(self.selected(),**self.auxiliary())
        pkg.check_asr_declaration(metadata,selected)
        for key in pkg.NATIVE_SEPARATOR:
            bad = copy.deepcopy(selected);bad['separator'][key] = 'other'
            with self.assertRaises(RuntimeError):pkg.asr_settings(bad)
        dropped = copy.deepcopy(selected);del dropped['separator']
        with self.assertRaisesRegex(RuntimeError,'drops configured'):
            pkg.check_asr_declaration(metadata,dropped)
        for change in ['identity','health','budget']:
            bad = copy.deepcopy(metadata)
            if change == 'identity':bad['chat_protocol']['transcriptions']['separator']['configured'] = False
            if change == 'health':bad['observed_configuration']['native_separator_health']['state'] = 'disabled'
            if change == 'budget':bad['observed_configuration']['native_separator_limits']['workspace_mib'] = 64
            with self.assertRaises(RuntimeError):pkg.check_asr_declaration(bad,selected)

    def test_cli_passes_separator_settings_and_rejects_orphan_options(self):
        args = ['service.py','prepare','--model-dir','/model','--output',str(self.root/'package'),
                '--asr-model-dir','/audio','--aligner-model-dir','/align',
                '--vad-model-dir','/vad','--speaker-model-dir','/speaker',
                '--separator-model-dir','/separator','--separator-max-audio-seconds','8',
                '--separator-workspace-mib','512']
        with mock.patch.object(sys,'argv',args),mock.patch.object(pkg,'prepare',return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        self.assertEqual(prepare.call_args.kwargs['asr_auxiliary']['separator'],
            dict(pkg.NATIVE_SEPARATOR,directory='/separator',
                 limits=dict(max_audio_seconds=8,workspace_mib=512)))
        with mock.patch.object(sys,'argv',args[:-6]+['--separator-workspace-mib','512']), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises((RuntimeError,SystemExit)):service.main()


if __name__ == '__main__':unittest.main()
