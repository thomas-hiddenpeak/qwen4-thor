"""Synthetic CPU package metadata; never an accepted voice business receipt."""
import contextlib
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from tools.serve_service import package as pkg, service
from tools.serve_service.test_tts_package import TtsPackage
from tools.serve_recovery import service as recovery
from tools.serve_recovery.test_deployment import EmbeddingTargetTests


def selected(directory='/optional/base'):
    return dict(pkg.NATIVE_TTS_BASE, directory=directory,
                limits=dict(pkg.TTS_LIMITS, max_frames=160))


def registration_asr():
    return dict(pkg.NATIVE_ASR, directory='/optional/asr', limits=dict(pkg.ASR_LIMITS),
        **{name:dict(fixed, directory='/optional/'+name, limits=dict(limits))
           for name, (fixed, limits, _) in pkg.ASR_AUXILIARY.items()
           if name in ('speaker', 'vad')})


def capabilities(builtin=False, registration=False):
    speech = dict(pkg.NATIVE_TTS if builtin else pkg.NATIVE_TTS_BASE,
        enabled=True, core_health_dependency=False, builtin_enabled=builtin,
        registered_custom_voices=True, registered_voice_model=pkg.NATIVE_TTS_BASE['model'],
        base_revision=pkg.NATIVE_TTS_BASE['revision'],
        base_weight_sha256=pkg.NATIVE_TTS_BASE['weight_sha256'])
    return dict(speech=speech, chat_audio_output=dict(enabled=True),
        voice_registration=dict(enabled=registration, owner_scoped=True,
            recheck_consent_per_synthesis=True, metadata_requires_healthy_model=False,
            core_health_dependency=False))


class VoicePackage(unittest.TestCase):
    setUp = TtsPackage.setUp
    fixture = TtsPackage.fixture
    selected = TtsPackage.selected
    tts_fixture = TtsPackage.tts_fixture

    def voice_fixture(self, builtin=False):
        binary, release, model = self.tts_fixture()
        metadata = pkg.read(release);declaration = metadata['release_declarations']
        root = Path(declaration['directory']);caps = pkg.read(root/'capabilities.json')
        caps.update(capabilities(builtin));metadata['chat_protocol'].update(capabilities(builtin))
        config = metadata['observed_configuration']
        if not builtin:config['native_tts_health']['state'] = 'disabled'
        for key, name in [('native_tts_base_health', pkg.NATIVE_TTS_BASE['model']),
                          ('native_voice_health', pkg.NATIVE_TTS_BASE['model']+'-speaker')]:
            config[key] = dict(state='configured', model=name, core_readiness_dependency=False)
        for name, value in [('capabilities.json', caps), ('configuration.json', config)]:
            pkg.save(root/name, value);declaration['files'][name] = pkg.sha(root/name)
        pkg.save(release, metadata)
        return binary, release, model

    def test_base_only_package_roundtrip_preserves_optional_assets_and_command(self):
        binary, release, model = self.voice_fixture()
        directory = self.root/'missing-base'
        result = pkg.prepare(binary, release, model, self.root/'package',
            tts_base_model_dir=directory, tts_limits=dict(max_frames=160))
        manifest = pkg.verify(result['package'], result['manifest_sha256'])
        self.assertFalse(directory.exists())
        self.assertEqual(manifest['settings']['native_tts_base'], selected(str(directory)))
        self.assertNotIn('native_tts', manifest['settings'])
        command = pkg.command(result['package'], manifest)
        self.assertNotIn('--tts-model-dir', command)
        self.assertEqual(command[command.index('--tts-base-model-dir')+1], str(directory))
        for key, value in selected()['limits'].items():
            flag = '--tts-'+key.replace('_', '-')
            self.assertEqual(command.count(flag), 1)
            self.assertEqual(command[command.index(flag)+1], str(value))

    def test_families_share_limits_and_cannot_mask_changed_base_identity(self):
        native = self.selected();base = selected()
        cfg = pkg.settings(8080, 0, native_tts=native, native_tts_base=base)
        command = pkg.command('/bundle', dict(model_dir='/model', settings=cfg))
        self.assertEqual(command.count('--tts-max-frames'), 1)
        with self.assertRaisesRegex(RuntimeError, 'share effective'):
            pkg.settings(8080, 0, native_tts=native,
                native_tts_base=dict(base, limits=dict(base['limits'], max_frames=159)))
        for key in pkg.NATIVE_TTS_BASE:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                pkg.settings(8080, 0, native_tts_base=dict(base, **{key:'wrong'}))
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            pkg.settings(8080, 0, native_tts_base=base,
                model_names=dict(name='chat', aliases=[base['model']]))

    def test_registration_uses_base_but_separator_still_needs_alignment(self):
        asr = registration_asr()
        cfg = pkg.settings(8080, 0, native_tts_base=selected(), native_asr=asr)
        self.assertNotIn('alignment', cfg['native_asr'])
        command = pkg.command('/bundle', dict(model_dir='/model', settings=cfg))
        self.assertIn('--speaker-model-dir', command)
        self.assertNotIn('--aligner-model-dir', command)
        with self.assertRaisesRegex(RuntimeError, 'either alignment or TTS Base'):
            pkg.settings(8080, 0, native_asr=asr)
        bad = dict(asr);bad.pop('vad')
        with self.assertRaises(RuntimeError):pkg.settings(8080, 0, native_asr=bad, native_tts_base=selected())
        fixed, limits, _ = pkg.ASR_AUXILIARY['separator']
        bad = dict(asr, separator=dict(fixed, directory='/separator', limits=dict(limits)))
        with self.assertRaisesRegex(RuntimeError, 'separator requires'):
            pkg.settings(8080, 0, native_asr=bad, native_tts_base=selected())

    def test_observed_base_condition_health_and_registration_cannot_be_dropped(self):
        _, release, _ = self.voice_fixture();metadata = pkg.read(release)
        pkg.check_tts_declaration(metadata, None, selected())
        mutations = [lambda d:d['chat_protocol']['speech'].update(base_weight_sha256='0'*64),
            lambda d:d['chat_protocol']['speech'].update(builtin_enabled=True),
            lambda d:d['observed_configuration']['native_voice_health'].update(core_readiness_dependency=True),
            lambda d:d['observed_configuration']['native_tts_base_health'].update(model='customvoice'),
            lambda d:d['observed_configuration']['native_tts_limits'].update(max_frames=159),
            lambda d:d['chat_protocol']['voice_registration'].update(recheck_consent_per_synthesis=False)]
        for mutate in mutations:
            bad = copy.deepcopy(metadata);mutate(bad)
            with self.assertRaises(RuntimeError):pkg.check_tts_declaration(bad, None, selected())
        with self.assertRaises(RuntimeError):pkg.check_tts_declaration(metadata, None)
        caps = capabilities(registration=True)
        pkg.check_tts_capabilities(caps, None, selected(), registration_asr())
        with self.assertRaisesRegex(RuntimeError, 'registration dependencies'):
            pkg.check_tts_capabilities(caps, None, selected())

    def test_both_explicit_clis_forward_base_and_shared_limits(self):
        args = ['service.py', 'prepare', '--model-dir', '/model', '--output', str(self.root/'package'),
            '--tts-base-model-dir', '/base', '--tts-max-frames', '160']
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(pkg, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            service.main()
        self.assertEqual(prepare.call_args.kwargs['tts_base_model_dir'], Path('/base'))
        self.assertEqual(prepare.call_args.kwargs['tts_limits'], dict(max_frames=160))
        args = ['prepare']
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe', 'policy',
                     'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            args += ['--'+name, str(self.root/name)]
        with mock.patch.object(recovery.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recovery.main([*args, '--tts-base-model-dir', '/base',
                '--tts-max-frames', '160']), 0)
        self.assertEqual(prepare.call_args.kwargs['tts_base_model_dir'], Path('/base'))
        self.assertEqual(prepare.call_args.kwargs['tts_limits'], dict(max_frames=160))

    def test_deployment_cannot_drop_accepted_base_but_can_relocate(self):
        target = EmbeddingTargetTests().target;base = selected()
        target(dict(native_tts_base=base), dict(native_tts_base=dict(base, directory='/moved')))
        for value in [None, dict(base, weight_sha256='0'*64)]:
            with self.assertRaisesRegex(ValueError, 'accepted TTS Base'):
                target(dict(native_tts_base=base), {} if value is None else dict(native_tts_base=value))


if __name__ == '__main__':unittest.main()
