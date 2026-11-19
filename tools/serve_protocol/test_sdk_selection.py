"""Explicit acceptance SDK selection; CPU-only, no network or model process."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools.serve_protocol import stage
from tools.serve_recovery import profile_acceptance as profiles
from tools.serve_schema_v2 import acceptance as shared
from tools.serve_service import package


class SdkSelection(unittest.TestCase):
    def setUp(self):
        root=stage.ROOT/'.q4t-work/sdk-selection-cpu';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.sdk=self.root/'sdk';self.sdk.mkdir()
        for name in ('package.json','package-lock.json'):
            shutil.copy2(stage.ROOT/'tools/serve_protocol'/name,self.sdk/name)
        module=self.sdk/'node_modules/openai';module.mkdir(parents=True)
        (module/'package.json').write_text(json.dumps(dict(version='7.47.0')))
        self.node=self.root/'node';self.node.write_text('CPU identity bytes; never executed')
        for patch in (mock.patch.object(stage.shutil,'which',return_value=str(self.node)),
                      mock.patch.object(stage.subprocess,'check_output',return_value='v24.18.0')):
            patch.start();self.addCleanup(patch.stop)

    def test_explicit_directory_keeps_every_file_and_exact_lock_identity(self):
        value=stage.sdk_identity(self.sdk)
        self.assertEqual(value['sdk_directory'],str(self.sdk))
        self.assertEqual(set(value['files']),{'package.json','package-lock.json',
                                            'node_modules/openai/package.json'})
        original=dict(value['files'])
        (self.sdk/'node_modules/openai/new-file').write_text('changed')
        current=stage.sdk_identity(self.sdk)
        self.assertNotEqual(current['files'],original)

    def test_explicit_selection_does_not_relax_version_or_lock_checks(self):
        path=self.sdk/'node_modules/openai/package.json'
        path.write_text(json.dumps(dict(version='7.48.0')))
        with self.assertRaisesRegex(RuntimeError,'version drift'):stage.sdk_identity(self.sdk)
        path.write_text(json.dumps(dict(version='7.47.0')))
        (self.sdk/'package-lock.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'lock drift'):stage.sdk_identity(self.sdk)

    def test_runtime_rechecks_the_directory_frozen_in_its_plan(self):
        binary=self.root/'binary';binary.write_bytes(b'cpu')
        sdk=dict(sdk_directory=str(self.sdk))
        plan=dict(output=str(self.root),limits=shared.Stage.limits,
            requests=shared.Stage.request_templates(),sources={},protected={},
            binary=str(binary),binary_sha256=package.sha(binary),sdk=sdk,
            model='/readonly',model_assets={},port=18090)
        with mock.patch.dict('os.environ',Q4T_MANAGED_LEASE_FD='123'), \
             mock.patch.object(shared.lifecycle,'verify_lease'), \
             mock.patch.object(shared,'sdk_identity',return_value=sdk) as identity, \
             mock.patch.object(shared,'check_assets'), \
             mock.patch.object(shared,'JsSdkClient'):
            shared.Stage(plan)
        identity.assert_called_once_with(str(self.sdk))

    def test_profile_cli_binds_the_explicit_sdk_instead_of_mutating_old_sdk(self):
        with mock.patch.object(profiles,'prepare') as prepare, mock.patch('sys.argv',[
            'profile_acceptance.py','prepare','--binary','/binary','--package','/package',
            '--output','/output','--sdk-directory',str(self.sdk)]):
            profiles.main()
        self.assertEqual(prepare.call_args.args[0].sdk_directory,self.sdk)


if __name__ == '__main__':
    unittest.main()
