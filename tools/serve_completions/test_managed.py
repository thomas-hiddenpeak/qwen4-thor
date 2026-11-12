"""Registry configuration survives the real managed engine launch command."""
import unittest

from tools.serve_service import package


class Managed(unittest.TestCase):
    def test_optional_registry_survives_command_and_settings_revalidation(self):
        names = dict(name='私有/base', aliases=['org/alias', 'literal%2F'])
        cfg = package.settings(18080, 0, model_names=names)
        restored = package.settings(cfg['port'], cfg['restart_limit'],
                                    model_names=cfg['model_names'])
        self.assertEqual(cfg, restored)
        names['aliases'].append('later-mutation')
        self.assertEqual(cfg['model_names']['aliases'], ['org/alias', 'literal%2F'])
        manifest = dict(settings=cfg, model_dir='/read-only-model')
        args = package.command('/package', manifest)
        self.assertEqual(args[-6:], ['--served-model-name', '私有/base',
            '--model-alias', 'org/alias', '--model-alias', 'literal%2F'])

    def test_invalid_or_ambiguous_registry_is_rejected(self):
        for names in [dict(name='', aliases=[]), dict(name='a', aliases=['a']),
            dict(name='x y', aliases=[]), dict(name='a', aliases=['b', 'b']),
            dict(name='a', aliases=['x'*257]), dict(name='a', aliases=['中'*86]),
            dict(name='a', aliases=['x\x00']), dict(name='a', aliases='b'),
            dict(name='a', aliases=['b'+str(i) for i in range(33)]),
            dict(name='a', aliases=[], extra=True)]:
            with self.subTest(names=names), self.assertRaises(RuntimeError):
                package.settings(18080, 0, model_names=names)

    def test_old_default_profile_does_not_acquire_a_new_model_identity(self):
        cfg = package.settings(18080, 0)
        self.assertNotIn('model_names', cfg)
        args = package.command('/package', dict(settings=cfg, model_dir='/model'))
        self.assertNotIn('--served-model-name', args)
        self.assertNotIn('--model-alias', args)


if __name__ == '__main__':
    unittest.main()
