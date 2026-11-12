"""Negative controls for actual qualification commands and environment receipts."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.serve_recovery import profile_acceptance as stage
from tools.serve_recovery import profile_contract
from tools.serve_service import capacity, package


class QualificationDriverTests(unittest.TestCase):
    def build_fixture(self):
        work = stage.ROOT/'.q4t-work/profile-build-cpu'
        work.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        (directory/'q4t').write_bytes(b'explicit CPU build fixture, never executable')
        (directory/'CMakeCache.txt').write_text('CPU_FIXTURE=TRUE\n')
        path = directory/'generated/q4t/server/chat_protocol_sources.json'
        path.parent.mkdir(parents=True)
        name = 'src/server/chat_capabilities.cpp'
        sources = {name:package.sha(stage.ROOT/name)}
        path.write_text(json.dumps(sources))
        base = dict(files=dict(q4t='a'*64), protocol_sha256='b'*64,
                    settings=dict(private='retained'), model_dir='/readonly/model')
        return directory, path, sources, base

    def test_compiled_candidate_qualification_preserves_retained_settings_and_is_not_release(self):
        directory, path, sources, base = self.build_fixture()
        original = copy.deepcopy(base)
        result, identity = stage.candidate_build_base(base, directory/'q4t', directory)
        expected = hashlib.sha256(''.join(n+':'+h+'\n' for n,h in
            sorted(sources.items())).encode()).hexdigest()
        self.assertEqual(result['protocol_sha256'], expected)
        self.assertEqual(base, original)
        self.assertEqual(result['settings'], base['settings'])
        self.assertEqual(result['model_dir'], base['model_dir'])
        self.assertEqual(identity['scope'], 'qualification_only_not_release')
        self.assertTrue(identity['new_package_qualification_pending'])
        self.assertEqual(identity['compiled_sources']['sha256'], package.sha(path))
        self.assertEqual(result['files']['q4t'], package.sha(directory/'q4t'))

    def test_compiled_candidate_rejects_source_drift_and_wrong_executable(self):
        directory, path, sources, base = self.build_fixture()
        other = directory/'other'
        other.write_bytes((directory/'q4t').read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'executable differs'):
            stage.candidate_build_base(base, other, directory)
        name = next(iter(sources))
        for invalid in ({name:'a'*64}, {'../outside':'a'*64},
                        {str(stage.ROOT/name):sources[name]}, {}, []):
            path.write_text(json.dumps(invalid))
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                stage.candidate_build_base(base, directory/'q4t', directory)

    def test_new_candidate_identity_cannot_relabel_or_modify_retained_settings(self):
        base = dict(files=dict(q4t='a'*64, other='f'*64), protocol_sha256='b'*64,
                    model_dir='/readonly/model', settings=dict(native_embedding=dict(
                        package.NATIVE_EMBEDDING, directory='/readonly/assets'),
                        profile=capacity.normalize(dict(version=1))))
        old = copy.deepcopy(base)
        metadata = dict(binary_sha256='c'*64, chat_protocol=dict(protocol_sha256='d'*64))
        value = stage.candidate_base(base, 'c'*64, metadata)
        self.assertEqual(base, old)
        self.assertEqual(value['settings'], old['settings'])
        self.assertEqual(value['model_dir'], old['model_dir'])
        self.assertEqual(value['files'], dict(q4t='c'*64, other='f'*64))
        self.assertEqual(value['protocol_sha256'], 'd'*64)
        value['settings']['profile']['max_tokens'] = 1
        self.assertEqual(base, old)
        for invalid in ({}, dict(metadata, binary_sha256='e'*64),
                        dict(metadata, chat_protocol=dict(protocol_sha256='bad'))):
            with self.assertRaises(RuntimeError):
                stage.candidate_base(base, 'c'*64, invalid)

    def test_commands_preserve_actual_selected_capacity_and_private_state(self):
        profile = capacity.normalize(dict(version=1))
        base = dict(model_dir='/readonly/model', client_policy={}, settings=dict(
            profile=profile, port=18162, operations_port=18163,
            application_state=dict(package.APPLICATION_LIMITS,
                profile='q4t-objects-v1', directory=''),
            native_embedding=dict(package.NATIVE_EMBEDDING, directory='/readonly/embedding')))
        plan = dict(base=base, profiles=profile_contract.profiles(profile),
                    port=19069, operations_port=19070, binary='/accepted/q4t')
        original = copy.deepcopy(plan)
        commands = {name:stage.command(plan, name, Path('/private/qualification'))
                    for name in ('primary', 'light')}
        self.assertEqual(plan, original)
        light = list(commands['primary'])
        light[light.index('--mtp')] = '--no-mtp'
        self.assertEqual(commands['light'], light)
        for args in commands.values():
            self.assertEqual(args[0], '/accepted/q4t')
            for flag, value in [('--max-tokens', '256'), ('--max-len', '8192'),
                ('--object-store-dir', '/private/qualification/application-objects'),
                ('--client-policy-file', '/private/qualification/client-policy.json'),
                ('--embedding-model-dir', '/readonly/embedding'),
                ('--operations-port', '19070')]:
                self.assertEqual(args.count(flag), 1)
                self.assertEqual(args[args.index(flag)+1], value)

    def test_failed_or_missing_environment_cannot_certify_business(self):
        exit_record = dict(status='COMPLETED', child_returncode=0,
                           compute_backend='nvml', lease_inherited_by_child=True)
        queries = [dict(status='OK', returncode=0)]
        cleanup = dict(remaining=[], model_exit=0, ports_released=True,
                       lease_continuously_held=True)
        self.assertEqual(stage.environment(exit_record, queries, [cleanup, cleanup]),
            dict(status='PASS', env_abort=False, all_queries_ok=True,
                 nvml_queries=1, cleanup_complete=True))
        for key, value in [('status','ENV_ABORT'), ('status','COMPLETED_WITH_MONITOR_GAPS'),
            ('child_returncode',1), ('compute_backend','nvidia-smi'),
            ('lease_inherited_by_child',False)]:
            with self.subTest(exit_field=key, value=value), self.assertRaises(RuntimeError):
                stage.environment(dict(exit_record, **{key:value}), queries, [cleanup, cleanup])
        for invalid in [[], [dict(status='TIMEOUT', returncode=0)],
                        [dict(status='OK', returncode=1)]]:
            with self.assertRaises(RuntimeError):
                stage.environment(exit_record, invalid, [cleanup, cleanup])
        for key, value in [('remaining',[123]), ('model_exit',-9),
                           ('ports_released',False), ('lease_continuously_held',False)]:
            with self.subTest(cleanup_field=key), self.assertRaises(RuntimeError):
                stage.environment(exit_record, queries, [cleanup, dict(cleanup, **{key:value})])
        with self.assertRaises(RuntimeError): stage.environment(exit_record, queries, [cleanup])


if __name__ == '__main__': unittest.main()
