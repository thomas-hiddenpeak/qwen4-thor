"""Narrow retained-source backport; CPU artifacts never qualify model business."""
import ast
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, cold_compat, ledger, resource_client, resource_compat as adapter
from tools.serve_recovery import deployment, service, version_contract, version_launch
from tools.serve_recovery import test_profile_deployment, test_version_registration
from tools.serve_recovery import test_resource_memory as resource_tests


def origin_sources():
    path=Path(os.environ['Q4T_RETAINED_SOURCE_PATH'])
    return {name:(path/name).read_bytes() for name in ('controller.py','profile_controller.py','profile_memory.py')}


def replace_sources(package):
    value=bundle.engine.read(package/'manifest.json')
    for name,raw in origin_sources().items():
        source=package/(adapter.PREFIX+name);source.chmod(0o600);source.write_bytes(raw);source.chmod(0o400)
        value['files'][adapter.PREFIX+name]=bundle.engine.sha(source)
    bundle.engine.save(package/'manifest.json',value);digest=bundle.engine.sha(package/'manifest.json')
    launcher=package/'bin/q4t';launcher.chmod(0o700)
    launcher.write_bytes(bundle.launcher(digest,value['python']['path']));launcher.chmod(0o500)
    return digest


def method(raw,kind,name):
    tree=ast.parse(raw)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==kind)
    node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
    return ast.dump(node,include_attributes=False)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.base=test_profile_deployment.DeploymentTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.original=self.base.old;self.digest=replace_sources(self.original)
        self.output=self.base.fixture.directory/'resource-adapter'

    def prepare(self): return adapter.prepare(self.original,self.digest,self.output)

    def test_exact_backport_preserves_unrelated_methods_original_and_inventory(self):
        before={name:(self.original/(adapter.PREFIX+name)).read_bytes() for name in origin_sources()}
        receipt=self.prepare();target=Path(receipt['payload'])
        self.assertTrue(adapter.aware(target));self.assertTrue(cold_compat.aware(target))
        self.assertFalse(receipt['business_acceptance']);self.assertFalse(receipt['model_actions'])
        original=bundle.verify(self.original,self.digest);changed=bundle.verify(target,receipt['payload_manifest_sha256'])
        self.assertEqual(original['version'],changed['version']);self.assertEqual(set(original['files']),set(changed['files']))
        for name,raw in before.items():self.assertEqual((self.original/(adapter.PREFIX+name)).read_bytes(),raw)
        raw=(target/cold_compat.SOURCE).read_bytes()
        for name in ('record_fault','protect','transaction','model_start'):
            # Only methods present in the accepted retained version are compared.
            if ('    def '+name+'(').encode() in before['controller.py']:
                self.assertEqual(method(before['controller.py'],'Controller',name),method(raw,'Controller',name))
        program='import sys;sys.path.insert(0,sys.argv[1]);from tools.serve_recovery import controller,profile_controller,profile_memory;assert hasattr(profile_memory,"Observer");print("retained-import-ok")'
        result=subprocess.run([sys.executable,'-I','-B','-c',program,str(target/'source')],capture_output=True,text=True,timeout=4)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'retained-import-ok')

    def test_resigned_unrelated_change_is_rejected_and_adapter_cannot_reapply(self):
        receipt=self.prepare();target=Path(receipt['payload'])
        with self.assertRaises(ValueError):adapter.changes(target,profile_history=True)
        source=target/'source/tools/serve_recovery/monitor.py'
        source.chmod(0o600);source.write_bytes(source.read_bytes()+b'\n# injected unrelated edit\n');source.chmod(0o400)
        manifest=bundle.engine.read(target/'manifest.json');manifest['files']['source/tools/serve_recovery/monitor.py']=bundle.engine.sha(source)
        bundle.engine.save(target/'manifest.json',manifest);digest=bundle.engine.sha(target/'manifest.json')
        launcher=target/'bin/q4t';launcher.chmod(0o700);launcher.write_bytes(bundle.launcher(digest,manifest['python']['path']));launcher.chmod(0o500)
        with self.assertRaisesRegex(ValueError,'another payload contract'):adapter.verify(self.original,self.digest,target,digest)

    def test_changed_hook_cannot_silently_receive_partial_backport(self):
        raw=origin_sources()['controller.py'];raw=cold_compat.adapted(raw)
        old,_=adapter.PATCHES['controller.py'][0]
        with self.assertRaisesRegex(ValueError,'requires review'):
            adapter.apply(raw.replace(old.encode(),b'\n# missing reviewed hook\n'),adapter.PATCHES['controller.py'])
        with self.assertRaises(ValueError):adapter.apply(raw+raw,adapter.PATCHES['controller.py'])

    def test_public_cli_selects_resource_adapter_without_runtime_or_model_action(self):
        result=dict(model_actions=False,business_acceptance=False)
        with mock.patch.object(adapter,'prepare',return_value=result) as call, mock.patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(service.main(['version-adapt','--package','/retained','--manifest-sha256','a'*64,
                '--output','/prepared','--resource-observation']),0)
        call.assert_called_once_with(Path('/retained'),'a'*64,Path('/prepared'))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.q4t-work/serve-resource-version-20261112';base.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name);self.script=self.path/'collector.py'
        self.script.write_text('import json,sys\nprint(json.dumps(dict(executed=True,binding=json.loads(sys.argv[2]))))\n');self.script.chmod(0o400)
        self.spec=dict(path=str(self.script),sha256=hashlib.sha256(self.script.read_bytes()).hexdigest())
        self.binding=dict(directory='/cpu-original',publication=dict(phase='ready',admission_enabled=True,
            device_authorized=False,model_actions=False,service_id=str(uuid.uuid4()),domain_id=str(uuid.uuid4()),runtime_uid=2002))

    def execute(self, spec=None):
        return subprocess.run([sys.executable,'-I','-B',resource_client.__file__,'--q4t-resource-worker',
            json.dumps(self.spec if spec is None else spec),'--binding',json.dumps(self.binding)],
            capture_output=True,text=True,timeout=4)

    def test_actual_worker_executes_verified_bytes_in_fresh_interpreter(self):
        result=self.execute();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),dict(executed=True,binding=self.binding))

    def test_hash_writeability_and_symlink_fail_before_collector_exec(self):
        self.assertNotEqual(self.execute(dict(self.spec,sha256='a'*64)).returncode,0)
        self.script.chmod(0o600)
        result=self.execute();self.assertNotEqual(result.returncode,0);self.assertFalse(result.stdout)
        self.script.chmod(0o400);link=self.path/'link.py';link.symlink_to(self.script)
        self.assertNotEqual(self.execute(dict(self.spec,path=str(link))).returncode,0)


class VersionTests(resource_tests.KernelTests):
    def setUp(self):
        super().setUp()
        self.register=test_version_registration.RegistrationTests();self.register.setUp()
        self.addCleanup(self.register.doCleanups)
        old=self.register.base.old;digest=replace_sources(old)
        self.register.base.old_sha=digest
        decision=bundle.engine.read(self.register.decision);decision['manifest_sha256']=digest
        bundle.engine.save(self.register.decision,decision)
        release=bundle.engine.read(self.register.release);release['manifest_sha256']=digest
        release['acceptance_sha256']=bundle.engine.sha(self.register.decision);bundle.engine.save(self.register.release,release)
        self.register.receipt=self.register.receipts()
        self.old=dict(package=str(old),manifest_sha256=digest)
        self.target=self.register.base.fixture.directory/'resource-fallback'
        result=adapter.prepare(old,digest,self.target)
        self.register.fallback=dict(package=result['payload'],manifest_sha256=result['payload_manifest_sha256'])
        self.register.registration=self.register.prepare();self.register.install()

    def test_two_payloads_receive_same_original_resource_and_fixed_root_collector(self):
        state=self.register.state;before=(state/'ledger.json').read_bytes()
        run=state/'runs'/str(uuid.uuid4());run.mkdir(mode=0o700)
        cfg=version_launch.configuration(state,deployment.read_descriptor(state),run,resources=self.binding)
        for name in ('primary','fallback'):
            config=json.loads((run/name/'controller.json').read_text())
            self.assertEqual(config['version'],6);self.assertEqual(config['resources'],self.binding)
            path=Path(config['resource_worker']['path'])
            self.assertEqual(config['resource_worker']['sha256'],bundle.engine.sha(path))
            self.assertEqual(path,Path(self.register.root['package'])/'source/tools/serve_recovery/resource_memory.py')
            self.assertEqual(cfg['payloads'][name]['binding'],self.register.registration['payloads'][name]['binding'])
        self.assertEqual(before,(state/'ledger.json').read_bytes())
        contract=version_contract.payload(self.register.fallback)
        self.assertEqual(version_contract.accepted_fallback(self.register.receipt,contract),self.register.receipt)

    def test_unadapted_payload_never_silently_loses_resource_observation(self):
        run=self.register.state/'runs'/str(uuid.uuid4());run.mkdir(mode=0o700)
        selected=dict(registration=deepcopy(self.register.registration),state=dict(terminal_fault=None))
        selected['registration']['payloads']['fallback']['descriptor']=self.old
        with mock.patch.object(version_launch.version_registration,'load',return_value=selected):
            with self.assertRaisesRegex(ValueError,'retained resource adapter'):
                version_launch.configuration(self.register.state,
                    deployment.read_descriptor(self.register.state),run,resources=self.binding)
        self.assertFalse((run/'fallback/controller.json').exists())


if __name__=='__main__':unittest.main(module=None)
