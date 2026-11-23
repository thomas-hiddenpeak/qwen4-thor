"""Targeted lifecycle CPU checks, including the actual native dispatch."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import build_tools, bundle, deployment, lifecycle, quota_manager, service, store
from tools.serve_recovery import test_native_bundle
from tools.serve_recovery.test_gateway import config
from tools.serve_service import package as engine, test_service as old

ROOT = Path(__file__).resolve().parents[2]
CLI = Path(os.environ.get('Q4T_SERVICE_CLI',
    ROOT/'.q4t-work/serve-access-20261109/host/q4t_service_cli_fixture'))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        receipt = self.fixture.prepare()
        self.package = Path(receipt['package']);self.digest = receipt['manifest_sha256']
        self.state = self.fixture.directory/'state'
        service.initialize(self.package, self.digest, self.state)
        self.desc = service.descriptor(self.state, self.package, self.digest)

    def files(self):
        return {str(p.relative_to(self.state)):p.read_bytes() for p in self.state.rglob('*')
                if p.is_file() and p.suffix == '.json'}

    def command(self, operation, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as error:
            rc = service.main([operation, '--state', str(self.state), *args],
                              package=self.package, digest=self.digest)
        return rc, json.loads(output.getvalue() or error.getvalue())

    def test_check_preserves_all_journals_and_never_launches(self):
        before = self.files()
        with mock.patch.object(service, 'launch', side_effect=AssertionError('model forbidden')):
            value = lifecycle.check(self.package, self.digest, self.state.relative_to(ROOT))
        self.assertEqual(value['status'], 'PASS')
        self.assertTrue(value['client_state_valid'])
        self.assertEqual(value['desired'], 'STOPPED')
        self.assertFalse(value['model_actions']);self.assertFalse(value['gpu_queries'])
        self.assertEqual(self.files(), before)

    def test_pending_policy_or_bad_envelope_stays_unknown_without_repair(self):
        directory = self.state/'client-limits'
        pending = directory/'policy.pending'
        pending.write_text('partial commit');pending.chmod(0o600)
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'commit unknown'):
            lifecycle.check(self.package, self.digest, self.state)
        self.assertEqual(pending.read_text(), 'partial commit')
        self.assertEqual(self.files(), before)
        pending.unlink()
        path = directory/'policy.json';value=json.loads(path.read_bytes())
        value['sha256'] = 'f'*64;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'envelope identity'):
            lifecycle.check(self.package, self.digest, self.state)

    def test_writer_and_guardian_ownership_prevent_false_stopped(self):
        for name in (store.LOCK, 'guardian.lock'):
            with deployment.claim(self.state, name=name), \
                 self.assertRaises(BlockingIOError):
                lifecycle.stopped(self.state, self.package, self.digest)
        with quota_manager.open_journal(self.state, self.desc['service_id']), \
             self.assertRaises(BlockingIOError):
            lifecycle.check(self.package, self.digest, self.state)

    def test_status_and_repeated_stop_confirm_durable_stopped(self):
        with mock.patch.object(service.control, 'request', side_effect=ConnectionRefusedError), \
             mock.patch.object(service, 'launch', side_effect=AssertionError('model forbidden')):
            for operation in ('status', 'stop', 'stop'):
                rc, value = self.command(operation)
                self.assertEqual((rc, value['state']), (0, 'STOPPED'))
                self.assertTrue(value['cleanup_confirmed'])
        self.assertEqual(list((self.state/'runs').iterdir()), [])

    def test_missing_core_lock_or_active_grant_cannot_confirm_stop(self):
        for value in (dict(desired='RUNNING', active=False), dict(desired='STOPPED', active=True)):
            with mock.patch.object(lifecycle.native, 'read_ledger', return_value=value), \
                 self.assertRaisesRegex(ValueError, 'not confirmed'):
                lifecycle.stopped(self.state, self.package, self.digest)
        path = self.state/store.LOCK;path.unlink()
        with self.assertRaises(FileNotFoundError):lifecycle.stopped(self.state, self.package, self.digest)
        self.assertFalse(path.exists())

    def test_reload_routes_tls_without_model_actions_and_validates_arguments(self):
        manifest = bundle.verify(self.package, self.digest)
        manifest = dict(manifest, tls_configuration=dict(enabled=True))
        with mock.patch.object(bundle, 'verify', return_value=manifest), \
             mock.patch.object(lifecycle.control, 'request', return_value={}), \
             mock.patch.object(lifecycle.tls_control, 'execute', return_value=dict(reloaded=True)) as execute, \
             mock.patch.object(service, 'launch', side_effect=AssertionError('model forbidden')):
            rc, value = self.command('reload', '--component', 'tls')
            self.assertEqual(rc, 0);self.assertTrue(value['reloaded'])
            self.assertTrue(execute.call_args.kwargs['reload'])
            self.assertFalse(value['model_actions'])
            for component, proposal in (('tls', self.state/'private'), ('access', None), ('limits', None)):
                with self.assertRaises(ValueError):
                    lifecycle.reload(self.package, self.digest, self.state,
                                     component=component, proposal=proposal)

    def test_reload_limits_reports_durable_commit_separate_from_live_application(self):
        clients = copy.deepcopy(config(self.state)['backend_policy']['clients'])
        clients[0]['requests_per_minute'] = 25
        path=self.state/'input.json';engine.save(path, dict(version=1, clients=clients))
        proposal = self.state/'proposal.json'
        quota_manager.propose(self.state, self.desc, path, proposal)
        before = (self.state/'ledger.json').read_bytes()
        rc, value = self.command('reload', '--component', 'limits', '--proposal', str(proposal),
                                 '--wait-seconds', '0')
        self.assertEqual(rc, 1)
        self.assertFalse(value['reloaded']);self.assertFalse(value['live_applied'])
        self.assertEqual(value['revision'], 1)
        self.assertEqual(before, (self.state/'ledger.json').read_bytes())
        with quota_manager.open_journal(self.state, self.desc['service_id']) as current:
            self.assertEqual(current.head['state']['policy']['clients'][0]['requests_per_minute'], 25)

    def test_bound_entry_can_select_a_different_explicit_deployment_target(self):
        destination = self.fixture.directory/'target'
        with mock.patch.object(deployment, 'switch', return_value=dict(switched=True)) as switch:
            rc, _ = self.command('deploy', '--package', str(destination), '--manifest-sha256', 'b'*64,
                '--expected-manifest-sha256', self.digest, '--request-id', str(uuid.uuid4()))
        self.assertEqual(rc, 0)
        self.assertEqual(switch.call_args.args[1:3], (destination, 'b'*64))


class NativeEntryTests(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture

    def invoke(self, cli, *args, ok=0, env=None):
        result = subprocess.run([str(cli), 'service', *map(str,args)], capture_output=True,
                                text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, ok, result.stderr + result.stdout)
        return result

    def setup_inputs(self):
        binary, release, model = self.fixture()
        shutil.copyfile(CLI, binary);binary.chmod(0o500)
        value = engine.read(release)
        value['binary_sha256'] = value['release_declarations']['binary_sha256'] = engine.sha(binary)
        engine.save(release, value)
        (model/'ple').mkdir();(model/'ple/table.bin').write_bytes(b'CPU fixture')
        (model/'weights.safetensors').write_bytes(b'CPU fixture')
        selected=config(self.root)
        policy=self.root/'gateway.json';clients=self.root/'clients.json'
        engine.save(policy, selected['policy']);engine.save(clients, selected['backend_policy'])
        native=self.root/'native.json'
        engine.save(native, dict(version=1,
            source_files={n:engine.sha(ROOT/n) for n in bundle.NATIVE_SOURCES},
            guardian_sha256=engine.sha(binary),cuda_probe_sha256=engine.sha(binary),
            ingress_sha256=engine.sha(binary)))
        return ['prepare','--binary',binary,'--release',release,'--model',model,
            '--guardian',binary,'--cuda-probe',binary,'--native-ingress',binary,
            '--policy',policy,'--backend-policy',clients,'--native-receipt',native]

    def prepare(self, output, *, cli=CLI, args=None, extra=()):
        args = self.setup_inputs() if args is None else args
        return json.loads(self.invoke(cli, *args, '--output', output, *extra).stdout), args

    def test_fresh_build_entry_really_prepares_and_checks_a_package(self):
        receipt, _ = self.prepare(self.root/'package')
        result=self.invoke(CLI,'check','--package',receipt['package'],
                           '--manifest-sha256',receipt['manifest_sha256'])
        value=json.loads(result.stdout)
        self.assertEqual(value['status'], 'PASS')
        self.assertFalse(value['model_actions']);self.assertFalse(value['state_checked'])
        self.assertTrue((Path(receipt['package'])/'engine/licenses/openai-node/LICENSE').is_file())

    def test_audited_rollback_covers_only_reviewed_stat_and_original_bytes(self):
        from tools.serve_recovery import model_content as content
        receipt, args = self.prepare(self.root/'original')
        package, digest = Path(receipt['package']), receipt['manifest_sha256']
        model = Path(args[args.index('--model')+1]).resolve()
        path = model/'weights.safetensors'
        original = engine.read(package/'manifest.json')['model_assets']
        path.chmod(path.stat().st_mode ^ 0o100)
        with self.assertRaisesRegex(ValueError, 'model asset identity changed'):
            bundle.verify(package, digest)
        revision = 'b'*40
        index = self.root/'upstream.json'
        engine.save(index, dict(id='test/cpu', sha=revision, siblings=[
            dict(rfilename=path.name, size=path.stat().st_size,
                 lfs=dict(size=path.stat().st_size, sha256=engine.sha(path)))]))
        report = self.root/'content.json'
        spec = [sys.executable, '-B', str(ROOT/'tools/serve_recovery/model_content.py'),
                'audit','--package',str(package),'--manifest-sha256',digest,
                '--upstream-index',str(index),'--upstream-sha256',engine.sha(index),
                '--repository','test/cpu','--revision',revision,'--report',str(report)]
        proc = subprocess.run(spec, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        audit = engine.read(report)
        self.assertEqual(set(audit['assets']), {path.name})
        rows = content.source_index(index, engine.sha(index), 'test/cpu', revision)
        with self.assertRaisesRegex(ValueError, 'content report identity differs'):
            content.validated_binding(package, digest, report, 'c'*64, engine.sha(index), rows)
        self.assertEqual(content.content_hash(model, path.name, bundle.file_identity(path)),
                         engine.sha(path))
        output = self.root/'rollback'
        result = content.prepare(package, digest, report, engine.sha(report),
                                 engine.sha(index), rows, output)
        check = json.loads(self.invoke(output/'engine/q4t','check','--package',output,
                           '--manifest-sha256',result['manifest_sha256']).stdout)
        self.assertTrue(check['package_verified'])
        bound = engine.read(output/'manifest.json')
        self.assertEqual(bound['model_assets'], original)
        self.assertEqual(bound['engine_manifest_sha256'],
                         engine.read(package/'manifest.json')['engine_manifest_sha256'])
        path.write_bytes(b'other bytes')
        with self.assertRaisesRegex(ValueError, 'model changed after content qualification'):
            bundle.verify(output, result['manifest_sha256'])
        with self.assertRaisesRegex(ValueError, 'only covers ctime drift'):
            content.drift(original, bundle.assets(model))

    def test_selected_workers_execute_only_the_qualified_image(self):
        args = self.setup_inputs()
        binary = args[args.index('--binary')+1]
        release = args[args.index('--release')+1]
        metadata = engine.read(release)
        conf = metadata['observed_configuration']
        conf['live_worker_profile'] = 'q4t-live-workers-v1'
        decl = metadata['release_declarations']
        path = Path(decl['directory'])/'configuration.json'
        engine.save(path, conf)
        decl['files'][path.name] = engine.sha(path)
        directory = self.root/'installed'
        directory.mkdir()
        for name in engine.LIVE_WORKERS:
            shutil.copy2('/usr/bin/true', directory/name)
        files = {name:engine.sha(directory/name) for name in engine.LIVE_WORKERS}
        decision = Path(metadata['acceptance_decision'])
        receipt = engine.read(decision)
        selection = dict(profile='q4t-live-workers-v1', engine_sha256=engine.sha(binary),
                         files=files)
        receipt['live_workers'] = selection
        engine.save(decision, receipt)
        metadata['acceptance_decision_sha256'] = engine.sha(decision)
        engine.save(release, metadata)

        with self.assertRaisesRegex(RuntimeError, 'qualification with this engine'):
            engine.accepted_live_workers(receipt, '0'*64)
        result, _ = self.prepare(self.root/'opt-out', args=args)
        package = Path(result['package'])
        inner = engine.read(package/'engine/manifest.json')
        self.assertEqual(engine.live_transport_args(inner), ['--live-worker-sha256', 'none'])
        result, _ = self.prepare(self.root/'selected', args=args, extra=('--live-workers-dir', directory))
        package = Path(result['package'])
        entry = package/'engine/q4t'
        inner = engine.read(package/'engine/manifest.json')
        self.assertEqual(inner['live_workers'], selection)
        self.assertEqual(engine.live_transport_args(inner),
                         ['--live-worker-sha256', ','.join(files[n] for n in engine.LIVE_WORKERS)])
        def launch(name):
            return subprocess.run([entry, '--live-worker-exec', name, files[name], '3'],
                                  capture_output=True, timeout=5).returncode
        for name in files:
            self.assertEqual(launch(name), 0)
        changed, absent, link = engine.LIVE_WORKERS
        (entry.parent/changed).chmod(0o700)
        with (entry.parent/changed).open('ab') as stream:
            stream.write(b'unqualified')
        (entry.parent/absent).unlink()
        (entry.parent/link).unlink()
        (entry.parent/link).symlink_to(directory/link)
        for name in files:
            self.assertNotEqual(launch(name), 0)
        status = json.loads(self.invoke(entry, 'check', '--package', package,
                            '--manifest-sha256', result['manifest_sha256']).stdout)
        self.assertTrue(status['package_verified'])
        self.assertEqual(status['live_worker_files'], dict.fromkeys(files, 'unavailable'))

    def test_live_transport_is_pinned_and_optional_during_service_check(self):
        args = self.setup_inputs()
        path = self.root/'private-trunks.json'
        row = dict(client_id='alpha', listen_ip='127.0.0.1', port=5061,
                   certificate_dir='/private/carrier', peer_subject='carrier')
        engine.save(path, dict(trunks=[row]))
        path.chmod(0o600)
        receipt, _ = self.prepare(self.root/'parent', args=args,
            extra=('--sip-trunks-file', path, '--live-webrtc-address', '127.0.0.2'))
        package = Path(receipt['package'])
        outer = bundle.verify(package, receipt['manifest_sha256'])
        inner = engine.verify(package/'engine', outer['engine_manifest_sha256'])
        self.assertEqual(inner['live_transport'], dict(profile='q4t-live-transport-v1',
            webrtc_address='127.0.0.2', sip_trunks_file=str(path), sip_trunks_sha256=engine.sha(path)))
        launch = engine.command(package/'engine', inner, state_directory=self.root/'state')
        for option, expected in [('--live-webrtc-address','127.0.0.2'),
                                 ('--sip-trunks-file',str(path)), ('--sip-trunks-sha256',engine.sha(path))]:
            self.assertEqual(launch[launch.index(option)+1], expected)
        path.unlink()
        self.invoke(package/'engine/q4t', 'check', '--package', package,
                    '--manifest-sha256', receipt['manifest_sha256'])
        self.assertFalse(any(p.name == path.name for p in package.rglob('*')))

    def test_live_transport_rejects_invalid_options_offline(self):
        path = self.root/'private-trunks.json'
        engine.save(path, dict(trunks=[]))
        path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'native Live transport'):
            engine.live_transport_binding(CLI, None, path)
        for invalid in ('0.0.0.0', '255.255.255.255', '239.1.2.3', 'localhost', ''):
            with self.subTest(address=invalid), self.assertRaisesRegex(RuntimeError, 'WebRTC address'):
                engine.live_transport_binding(CLI, invalid, None)
            check = subprocess.run([CLI, 'live-transport-check', invalid], capture_output=True)
            self.assertNotEqual(check.returncode, 0)
        selected = engine.live_transport_binding(CLI, '127.0.0.2', None)
        self.assertEqual(selected, dict(profile='q4t-live-transport-v1', webrtc_address='127.0.0.2'))
        self.assertIsNone(engine.live_transport_binding(CLI, None, None))

    def test_recording_opt_in_survives_sealed_parent_prepare(self):
        args = self.setup_inputs()
        release = args[args.index('--release')+1]
        metadata = engine.read(release)
        config = metadata['observed_configuration']
        config['application_state'] = dict(profile='q4t-objects-v1',
            live_recordings=False, **engine.APPLICATION_LIMITS)
        declarations = metadata['release_declarations']
        path = Path(declarations['directory'])/'configuration.json'
        engine.save(path, config)
        declarations['files'][path.name] = engine.sha(path)
        engine.save(release, metadata)
        entry = self.root/'entry'
        build_tools.prepare(entry/'service-tools')
        cli = entry/'q4t'
        shutil.copy2(CLI, cli)
        for name, opt_in in [('first', False), ('second', True)]:
            receipt, _ = self.prepare(self.root/name, cli=cli, args=args,
                                      extra=('--live-recordings',) if opt_in else ())
            package = Path(receipt['package'])
            outer = bundle.verify(package, receipt['manifest_sha256'])
            inner = engine.verify(package/'engine', outer['engine_manifest_sha256'])
            launch = engine.command(package/'engine', inner, state_directory=self.root/'state')
            self.assertEqual(launch.count('--live-recordings'), int(opt_in))
            cli = package/'engine/q4t'
        native = args[args.index('--native-receipt')+1]
        altered = engine.read(native)
        altered['source_files']['src/service/guardian_main.cpp'] = '0'*64
        engine.save(native, altered)
        self.invoke(cli, *args, '--output', self.root/'unbound', ok=1)
        self.assertFalse((self.root/'unbound').exists())

    def test_packaged_native_entry_prepares_new_target_and_round_trips_without_state_reset(self):
        first,args = self.prepare(self.root/'first')
        cli=Path(first['package'])/'engine/q4t'
        state=self.root/'state'
        self.invoke(cli,'init','--state',state)
        self.invoke(cli,'check','--state',state)
        before={p: p.read_bytes() for p in state.rglob('*.json') if p.name != 'service.json'}
        cap=self.root/'capacity.json';engine.save(cap,dict(version=1,max_tokens=128))
        second,_=self.prepare(self.root/'second',cli=cli,args=args,extra=('--capacity-config',cap))
        self.invoke(cli,'deploy','--state',state,'--package',second['package'],
            '--manifest-sha256',second['manifest_sha256'],
            '--expected-manifest-sha256',first['manifest_sha256'],'--request-id',uuid.uuid4())
        self.invoke(cli,'rollback','--state',state,
            '--expected-manifest-sha256',second['manifest_sha256'],'--request-id',uuid.uuid4())
        status=json.loads(self.invoke(cli,'status','--state',state).stdout)
        self.assertEqual(status['state'],'STOPPED')
        self.invoke(cli,'check','--state',state)
        for path,raw in before.items():
            if path.parent.name == 'service-deployment':
                # Deployment selection/history advances on each switch.
                prior=json.loads(raw)['state']['policy']
                current=json.loads(path.read_bytes())['state']['policy']
                self.assertEqual(current['revision'],prior['revision']+4)
                self.assertEqual(len(current['history']),len(prior['history'])+2)
            elif path.name == 'ledger.json':
                self.assertEqual(json.loads(raw)['state'], json.loads(path.read_bytes())['state'])
            else:self.assertEqual(path.read_bytes(),raw)

    def relocate_tools(self):
        directory=self.root/'entry';directory.mkdir()
        cli=directory/'q4t';shutil.copyfile(CLI,cli);cli.chmod(0o500)
        tools=directory/'service-tools/current';tools.parent.mkdir()
        shutil.copytree(CLI.parent/'service-tools/current',tools)
        return cli,tools

    def test_source_tampering_refuses_before_import_and_ignores_pythonpath(self):
        cli,tools=self.relocate_tools()
        source=tools/'source/tools/serve_recovery/service.py'
        source.chmod(0o600);source.write_text("raise AssertionError('MUST_NOT_IMPORT')\n")
        env=dict(os.environ, PYTHONPATH=str(self.root), PYTHONHOME=str(self.root))
        result=self.invoke(cli,'--help',ok=1,env=env)
        self.assertIn('service_entry_identity_invalid',result.stderr)
        self.assertNotIn('MUST_NOT_IMPORT',result.stderr)

    def test_interpreter_identity_change_refuses_native_dispatch(self):
        cli,tools=self.relocate_tools()
        path=tools/'manifest.json';path.chmod(0o600)
        value=engine.read(path);value['python']['sha256']='f'*64;engine.save(path,value)
        result=self.invoke(cli,'--help',ok=1)
        self.assertIn('Service entry unavailable or changed',result.stderr)

    def test_no_tools_is_explicit_error_without_model_or_fallback_command(self):
        cli=self.root/'q4t';shutil.copyfile(CLI,cli);cli.chmod(0o500)
        result=self.invoke(cli,'--help',ok=1)
        self.assertIn('Keep the verified service package',result.stderr)

    def test_pinned_interpreter_enforces_diagnostic_and_tls_memfd_seals(self):
        snapshot=(CLI.parent/'service-tools/current').resolve()
        manifest=engine.read(snapshot/'manifest.json')
        code='''import contextlib,fcntl,os,sys
sys.path.insert(0,sys.argv[1])
from tools.serve_recovery import service
from tools.serve_gateway import tls
fd=service.diagnostic_buffer()
try:
    assert fcntl.fcntl(fd,service.GET_SEALS)==7
    try:os.ftruncate(fd,service.DIAGNOSTIC_BYTES+1)
    except OSError:pass
    else:raise AssertionError('diagnostic grew')
finally:os.close(fd)
with contextlib.ExitStack() as stack:
    fd=tls.immutable_fd(stack,b'CPU fixture')
    assert fcntl.fcntl(fd,service.GET_SEALS)==15
    try:os.write(fd,b'x')
    except OSError:pass
    else:raise AssertionError('TLS input remained writable')
print('PASS_MEMFD_SEALS')
'''
        result=subprocess.run([manifest['python']['path'],'-I','-B','-c',code,
            str(snapshot/'source')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PASS_MEMFD_SEALS',result.stdout)

    def test_runtime_requirement_refuses_incomplete_python_before_creating_tools(self):
        from tools.serve_recovery import build_tools
        build_tools.check_runtime()
        present=build_tools.os.memfd_create
        try:
            del build_tools.os.memfd_create
            with self.assertRaisesRegex(RuntimeError,'lacks Linux APIs: memfd_create'):
                build_tools.prepare(self.root/'unqualified-tools')
            with self.assertRaisesRegex(RuntimeError,'lacks Linux APIs: memfd_create'):
                engine.prepare('missing', 'missing', 'missing', self.root/'unqualified-engine')
        finally:build_tools.os.memfd_create=present
        self.assertFalse((self.root/'unqualified-tools').exists())
        self.assertFalse((self.root/'unqualified-engine').exists())

    def test_default_engine_and_service_identity_cannot_diverge(self):
        receipt,_=self.prepare(self.root/'package')
        directory=self.root/'default';directory.mkdir()
        cli=directory/'q4t';shutil.copyfile(CLI,cli)
        with cli.open('ab') as stream:stream.write(b'unselected engine identity')
        cli.chmod(0o500);(directory/'service').symlink_to(receipt['package'])
        result=self.invoke(cli,'check',ok=1)
        self.assertIn('service_entry_identity_invalid',result.stderr)


if __name__ == '__main__':unittest.main()
