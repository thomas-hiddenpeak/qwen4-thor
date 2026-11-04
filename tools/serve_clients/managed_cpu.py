"""Release receipt compatibility and actual default packaging, no listeners."""
import copy
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_clients import cpu
from tools.serve_service import package as pkg
from tools.serve_service.test_service import PackageTests
from tools.serve_gateway import client_policy as cp

BASE=ROOT/'.q4t-work/serve-clients-managed-20261104'


def policy():
    return dict(version=1,gateway_token=secrets.token_hex(32),clients=[
        dict(id=name,max_inflight=1,max_control_inflight=2,max_prompt_tokens=4096,
             max_output_tokens=512 if name=='alpha' else 64,max_body_bytes=131072)
        for name in ('alpha','beta')])


class Receipts(unittest.TestCase):
    def setUp(self):
        parent=BASE/'cpu-private';parent.mkdir(exist_ok=True,mode=0o700)
        self.root=Path(tempfile.mkdtemp(dir=parent))

    fixture=PackageTests.fixture

    def test_old_pass_and_named_aggregate_both_package(self):
        binary,release,model=self.fixture()
        first=pkg.prepare(binary,release,model,self.root/'legacy')
        pkg.verify(first['package'],first['manifest_sha256'])
        meta=pkg.read(release)
        meta['acceptance']='ACCEPT_BOUNDED_CLIENT_ISOLATION'
        pkg.save(meta['acceptance_decision'],dict(status=meta['acceptance']))
        meta['acceptance_decision_sha256']=pkg.sha(meta['acceptance_decision'])
        pkg.save(release,meta)
        second=pkg.prepare(binary,release,model,self.root/'aggregate')
        pkg.verify(second['package'],second['manifest_sha256'])

    def test_failed_pending_and_unknown_aggregate_are_rejected(self):
        for status in ('FAILED','ENV_ABORT','PENDING','ACCEPT_UNKNOWN','ACCEPT_PENDING',None,True,1,[]):
            with self.assertRaisesRegex(RuntimeError,'accepted release'):
                pkg.accepted_release({'acceptance':'ACCEPT_BOUNDED_CLIENT_ISOLATION'},{'status':status})

    def test_aggregate_must_match_release_and_shapes_are_explicit(self):
        with self.assertRaisesRegex(RuntimeError,'accepted release'):
            pkg.accepted_release({'acceptance':'ACCEPT_OTHER'},{'status':'ACCEPT_BOUNDED_CLIENT_ISOLATION'})
        for metadata,decision in [(None,{}),({},[]),({},None)]:
            with self.assertRaisesRegex(RuntimeError,'must be objects'):
                pkg.accepted_release(metadata,decision)

    def test_verify_rejects_failed_receipt_even_with_consistent_hashes(self):
        binary,release,model=self.fixture()
        receipt=pkg.prepare(binary,release,model,self.root/'failed')
        folder=Path(receipt['package'])
        value=pkg.read(folder/'q4t.release.json')
        (folder/'acceptance.json').chmod(0o600)
        pkg.save(folder/'acceptance.json',dict(status='FAILED'))
        value['acceptance_decision_sha256']=pkg.sha(folder/'acceptance.json')
        (folder/'q4t.release.json').chmod(0o600)
        pkg.save(folder/'q4t.release.json',value)
        manifest=pkg.read(folder/'manifest.json')
        for name in ('acceptance.json','q4t.release.json'):
            manifest['files'][name]=pkg.sha(folder/name)
        pkg.save(folder/'manifest.json',manifest)
        with self.assertRaisesRegex(RuntimeError,'accepted release'):
            pkg.verify(folder,pkg.sha(folder/'manifest.json'))

    def test_actual_default_v4_package_policy_and_snapshot_entry(self):
        model=pkg.read(ROOT/'.q4t-work/serve-clients-20261104/cache-plan.json')['model_dir']
        path=self.root/'clients.json';pkg.save(path,policy())
        receipt=pkg.prepare(ROOT/'build/q4t',ROOT/'build/q4t.release.json',model,
                            self.root/'actual-v4',port=18128,client_policy=path)
        manifest=pkg.verify(receipt['package'],receipt['manifest_sha256'])
        self.assertEqual(manifest['version'],4)
        self.assertEqual(manifest['client_policy'],cp.binding(path.read_bytes()))
        self.assertEqual(manifest['files']['q4t'],pkg.sha(ROOT/'build/q4t'))
        package=Path(receipt['package'])
        command=[manifest['python']['path'],'-I','-B',str(package/'source/tools/serve_service/service.py'),
                 'unit','--package',str(package),'--manifest-sha256',receipt['manifest_sha256'],
                 '--output',str(self.root/'not-started')]
        import subprocess
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Restart=no',result.stdout)
        self.assertFalse((self.root/'not-started').exists())
        self.assertIn('--client-policy-file',pkg.command(package,manifest))
        pkg.save(self.root/'actual-receipt.json',receipt)


def run():
    output=BASE/'cpu';output.mkdir(mode=0o700)
    from tools.serve_clients import managed_stage
    paths=set(managed_stage.SOURCES)|{'tools/serve_clients/managed_cpu.py',
        'tools/serve_clients/managed_stage.py','tools/serve_clients/test_managed_stage.py',
        'tools/serve_gateway/managed.py','tools/serve_gateway/gateway.py',
        'tools/serve_soak/stage.py','tools/serve_soak/evidence.py',
        'tools/serve_gateway/managed_stage.py','tools/serve_gateway/acceptance.py',
        'tools/serve_service/acceptance.py','tools/serve_service/controller_exec.py',
        'tools/serve_service/lease_custody.py'}
    sources={name:pkg.sha(ROOT/name) for name in sorted(paths)}
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in (
        'tools.serve_clients.managed_cpu.Receipts',
        'tools.serve_clients.test_managed_stage',
        'tools.serve_service.test_service.PackageTests',
        'tools.serve_clients.test_packages.Clients',
        'tools.serve_soak.test_soak'))
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),sources=sources,
        default={name:pkg.sha(ROOT/name) for name in ('build/q4t','build/q4t.release.json')},
        model_started=False,http_started=False))
    print(log.getvalue(),end='')
    pkg.require(result.wasSuccessful(),'preserve CPU failure')


def repair_driver():
    """Only the changed driver is rerun; preserve the first 37-case result."""
    from tools.serve_clients import managed_stage
    first=pkg.read(BASE/'cpu/result.json')
    pkg.require(not first['passed'] and first['tests_run']==37 and
                first['errors']==2 and first['failures']==0,'unexpected original CPU result')
    changed={'tools/serve_clients/managed_cpu.py','tools/serve_clients/managed_stage.py',
             'tools/serve_clients/test_managed_stage.py','docs/SERVE_CLIENT_MANAGED_2026-11-04.md'}
    for name,digest in first['sources'].items():
        pkg.require(pkg.sha(BASE/'cpu/source'/name)==digest,'original CPU source not preserved')
        if name not in changed:pkg.require(pkg.sha(ROOT/name)==digest,'reused source changed: '+name)
    paths=set(first['sources'])|set(managed_stage.SOURCES)|{
        'tools/serve_service/test_service.py','tools/serve_clients/test_packages.py'}
    output=BASE/'cpu-corrected';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in sorted(paths)}
    for name in sources:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    suite=unittest.defaultTestLoader.loadTestsFromName('tools.serve_clients.test_managed_stage')
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),effective_tests=33+result.testsRun,
        reused_tests=33,reused_suites=['Receipts:5','PackageTests:9','Clients:8','Case:11'],
        original_sha256=pkg.sha(BASE/'cpu/result.json'),sources=sources,
        default=first['default'],model_started=False,http_started=False))
    print(log.getvalue(),end='');pkg.require(result.wasSuccessful(),'preserve corrected driver failure')


def repair_cleanup():
    previous=pkg.read(BASE/'cpu-corrected/result.json')
    pkg.require(not previous['passed'] and previous['tests_run']==5 and previous['errors']==1
                and previous['failures']==0,'unexpected driver correction result')
    changed={'tools/serve_clients/managed_cpu.py','tools/serve_clients/managed_stage.py',
             'tools/serve_clients/test_managed_stage.py'}
    for name,digest in previous['sources'].items():
        pkg.require(pkg.sha(BASE/'cpu-corrected/source'/name)==digest,'correction source missing')
        if name not in changed:pkg.require(pkg.sha(ROOT/name)==digest,'reused dependency changed')
    output=BASE/'cpu-accepted';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in previous['sources']}
    for name in sources:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
    suite=unittest.defaultTestLoader.loadTestsFromName(
        'tools.serve_clients.test_managed_stage.Driver.test_all_frontends_stop_before_backend_releases_lease')
    log=io.StringIO();result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        errors=len(result.errors),failures=len(result.failures),effective_tests=37+result.testsRun,
        reused_tests=37,original_sha256=pkg.sha(BASE/'cpu/result.json'),
        correction_sha256=pkg.sha(BASE/'cpu-corrected/result.json'),sources=sources,
        default=previous['default'],model_started=False,http_started=False))
    print(log.getvalue(),end='');pkg.require(result.wasSuccessful(),'preserve cleanup fixture failure')


if __name__=='__main__':
    os.umask(0o077)
    if sys.argv[1:]==['--repair-cleanup']:repair_cleanup()
    elif sys.argv[1:]==['--repair-driver']:repair_driver()
    elif not sys.argv[1:]:run()
    else:raise SystemExit('usage: managed_cpu.py [--repair-driver | --repair-cleanup]')
