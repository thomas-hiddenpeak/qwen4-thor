"""Targeted publication fault checks; synthetic assets and no model execution."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, bundle, deployment as d, ledger, lifecycle, service, store
from tools.serve_recovery import publication as p, publication_files as files
from tools.serve_recovery import test_lifecycle, test_native_bundle
from tools.serve_service import package as engine


def private_lease(package, path):
    value = engine.read(package/'manifest.json');value['lock_path'] = str(path)
    engine.save(package/'manifest.json',value)
    digest = engine.sha(package/'manifest.json')
    launcher = package/'bin/q4t';launcher.chmod(0o700)
    launcher.write_bytes(bundle.launcher(digest,value['python']['path']));launcher.chmod(0o500)
    return digest


def accepted(package, digest, state, path):
    base = engine.verify(package/'engine',engine.read(package/'manifest.json')['engine_manifest_sha256'])
    decision = path.with_name(path.name+'.decision')
    engine.save(decision,dict(status='PASS_CPU_PUBLICATION_FIXTURE',acceptance='ACCEPT_CPU_FIXTURE_ONLY'))
    engine.save(path,dict(recovery_accepted=True,package=str(package),manifest_sha256=digest,
        engine_binary_sha256=base['files']['q4t'],engine_release_sha256=base['files']['q4t.release.json'],
        state=str(state),service_id=d.read_descriptor(state)['service_id'],
        acceptance='ACCEPT_CPU_FIXTURE_ONLY',acceptance_record=str(decision),
        acceptance_sha256=engine.sha(decision)))
    return path


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_bundle.BundleTests()
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        receipt = self.fixture.prepare();self.package = Path(receipt['package'])
        shutil.copyfile(self.fixture.binary,self.package/'engine/q4t')
        engine.save(self.package/'engine/q4t.release.json',dict(cpu_fixture=True))
        self.fixture.base['files']['q4t.release.json'] = engine.sha(self.package/'engine/q4t.release.json')
        self.lease = self.fixture.directory/'cpu-lease.lock'
        self.digest = private_lease(self.package,self.lease)
        self.state = self.fixture.directory/'state'
        service.initialize(self.package,self.digest,self.state)
        self.desc = d.read_descriptor(self.state)
        self.target = self.fixture.directory/'second';shutil.copytree(self.package,self.target)
        self.release = accepted(self.target,self.digest,self.state,self.fixture.directory/'target.json')
        self.root = self.fixture.directory/'default';self.root.mkdir(mode=0o700)
        for name in ('q4t','q4t.release.json'):shutil.copyfile(self.package/'engine'/name,self.root/name)
        (self.root/'service').symlink_to(self.package)
        accepted(self.package,self.digest,self.state,self.root/'service.release.json')
        self.request = str(uuid.uuid4())
        self.before = self.business()

    def business(self):
        return {str(path.relative_to(self.state)):path.read_bytes() for path in self.state.rglob('*.json')
                if path.name == 'ledger.json' or path.parent.name in ('client-access','client-limits','client-usage')}

    def publish(self, **kwargs):
        return p.execute(self.state,request_id=self.request,root=self.root,package=self.target,
            digest=self.digest,release=self.release,expected=self.digest,revision=0,**kwargs)

    def resume(self):return p.execute(self.state,request_id=self.request,resume=True)

    def check_complete(self):
        result=p.status(self.state);self.assertTrue(result['ready'])
        self.assertEqual(self.business(),self.before)
        self.assertTrue(files.aliases(self.root));self.assertEqual(d.read_descriptor(self.state)['package'],str(self.target))
        self.assertEqual(list((self.state/'runs').iterdir()),[])
        d.ready(self.state,d.read_descriptor(self.state))
        return result

    def test_publish_replay_and_actual_old_generation_rollback_preserve_business_state(self):
        with mock.patch.object(service,'launch',side_effect=AssertionError('model forbidden')):
            result=self.publish();self.assertFalse(result['automatic_start']);self.check_complete()
            self.assertTrue(self.publish()['replayed']);self.assertTrue(self.resume()['replayed'])
            previous=result['history'][-1]['previous']
            # The original receipt may be archived after generation capture.
            (self.root/'service.release.json.decision').unlink()
            backward=p.execute(self.state,request_id=str(uuid.uuid4()),root=self.root,
                expected=self.digest,revision=result['revision'],generation=previous['generation_sha256'])
            self.assertEqual(backward['current'],previous)
            self.assertEqual(d.read_descriptor(self.state)['package'],str(self.package))
            self.assertTrue(p.status(self.state)['ready']);self.assertEqual(self.business(),self.before)

    def test_default_switch_fault_resumes_and_fences_new_launch(self):
        original=files.replace_link;fired=False
        def fail(path,target):
            nonlocal fired
            original(path,target)
            if path == self.root/'q4t' and not fired:
                fired=True;raise OSError('CPU interruption after one alias')
        with mock.patch.object(files,'replace_link',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertTrue(fired);self.assertFalse(p.status(self.state)['ready'])
        before={str(path):path.read_bytes() for path in self.state.rglob('*.json')}
        with self.assertRaises(ValueError):lifecycle.check(self.target,self.digest,self.state)
        with mock.patch.object(service,'acquire_lease') as lease,self.assertRaises(ValueError):
            service.launch(self.target,self.digest,self.state)
        lease.assert_not_called()
        self.assertEqual(before,{str(path):path.read_bytes() for path in self.state.rglob('*.json')})
        self.resume();self.check_complete()

    def test_process_exit_during_selection_releases_ownership_and_resumes(self):
        pid=os.fork()
        if pid == 0:
            original=files.replace_link
            def exit_after_alias(path,target):
                original(path,target)
                if path == self.root/'q4t.release.json':os._exit(86)
            files.replace_link=exit_after_alias
            try:self.publish()
            except BaseException:os._exit(87)
            os._exit(88)
        _,status=os.waitpid(pid,0)
        self.assertTrue(os.WIFEXITED(status));self.assertEqual(os.WEXITSTATUS(status),86)
        self.assertFalse(p.status(self.state)['ready'])
        self.resume();self.check_complete()

    def test_pointer_rename_before_directory_sync_resumes_same_generation(self):
        original=files.sync;fired=False
        def fail(path):
            nonlocal fired
            if Path(path) == self.root/files.DIRECTORY and files.selected(self.root) is not None:
                intent=p.load_intent(self.state,self.desc,self.request)
                if files.selected(self.root) == intent['target']['generation_sha256'] and not fired:
                    fired=True;raise OSError('CPU selection fsync failure')
            return original(path)
        with mock.patch.object(files,'sync',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertTrue(fired);self.resume();self.check_complete()

    def short_write(self, predicate):
        original=store.Store.write_all;fired=False
        def fail(fd,raw):
            nonlocal fired
            if not fired and predicate(raw,Path(os.readlink('/proc/self/fd/'+str(fd)))):
                fired=True;os.write(fd,raw[:max(1,len(raw)//3)])
                raise OSError('CPU partial publication record')
            return original(fd,raw)
        with mock.patch.object(store.Store,'write_all',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertTrue(fired)
        return fired

    def test_initial_publication_writer_short_write_retries_without_reset(self):
        self.short_write(lambda raw,path:path.name == 'writer.lock' and path.parent.name == p.Store.directory_name)
        self.resume();self.check_complete()

    def test_initial_publication_policy_short_write_retries_without_reset(self):
        self.short_write(lambda raw,path:path.name == 'policy.pending' and path.parent.name == p.Store.directory_name)
        self.resume();self.check_complete()

    def test_initial_binding_short_write_retries_without_reset(self):
        self.short_write(lambda raw,path:path.name == p.Store.marker_name+'.pending')
        self.resume();self.check_complete()

    def pending_short(self, family, phase):
        self.short_write(lambda raw,path:path.name == 'policy.pending' and path.parent.name == family and
            json.loads(raw)['state']['policy']['phase'] == phase and
            json.loads(raw)['state']['policy']['revision'] > 0)
        pending=self.state/family/'policy.pending';before=pending.read_bytes()
        self.assertFalse(p.status(self.state)['ready']);self.assertEqual(pending.read_bytes(),before)
        self.resume();self.check_complete()

    def test_publication_prepare_short_write_reconstructs_only_same_intent(self):
        self.pending_short(p.Store.directory_name,'prepared')

    def test_publication_finish_short_write_requires_selected_default(self):
        self.pending_short(p.Store.directory_name,'stable')

    def test_deployment_prepare_short_write_reconstructs_only_same_intent(self):
        self.pending_short(d.Store.directory_name,'prepared')

    def test_deployment_finish_short_write_requires_selected_default(self):
        self.pending_short(d.Store.directory_name,'stable')

    def test_finish_cannot_be_recovered_when_default_selection_is_unknown(self):
        self.short_write(lambda raw,path:path.name == 'policy.pending' and path.parent.name == p.Store.directory_name and
            json.loads(raw)['state']['policy']['phase'] == 'stable')
        pointer=self.root/files.DIRECTORY/'current';selected=os.readlink(pointer)
        pointer.unlink();pointer.symlink_to('generations/'+'f'*64)
        pending=self.state/p.Store.directory_name/'policy.pending';before=pending.read_bytes()
        with self.assertRaises(ValueError):self.resume()
        self.assertEqual(pending.read_bytes(),before)
        pointer.unlink();pointer.symlink_to(selected)
        self.resume();self.check_complete()

    def test_unrelated_partial_bytes_never_repaired_or_removed(self):
        self.short_write(lambda raw,path:path.name == 'policy.pending' and path.parent.name == p.Store.directory_name and
            json.loads(raw)['state']['policy']['phase'] == 'prepared')
        pending=self.state/p.Store.directory_name/'policy.pending';pending.write_bytes(b'unknown-transaction')
        with self.assertRaisesRegex(ValueError,'differs from immutable intent'):self.resume()
        self.assertEqual(pending.read_bytes(),b'unknown-transaction');self.assertEqual(self.business(),self.before)

    def test_immutable_intent_short_write_can_only_resume_same_original_request(self):
        self.short_write(lambda raw,path:path.parent.name == p.INTENTS and path.name.endswith('.json.new'))
        with self.assertRaisesRegex(ValueError,'intent unavailable'):self.resume()
        self.publish();self.check_complete()

    def test_generation_copy_after_chmod_before_rename_is_resumable(self):
        original=os.replace;fired=False
        def fail(source,target,*args,**kwargs):
            nonlocal fired
            if Path(str(source)).name == 'q4t.partial' and not fired:
                fired=True;raise OSError('CPU complete copy not renamed')
            return original(source,target,*args,**kwargs)
        with mock.patch.object(os,'replace',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertTrue(fired);self.publish();self.check_complete()

    def test_generation_partial_with_public_permissions_or_link_is_never_reused(self):
        target=self.fixture.directory/'copy'
        partial=target.with_name(target.name+'.partial')
        shutil.copyfile(self.fixture.binary,partial);partial.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'private generation partial'):
            files.copy_file(self.fixture.binary,target,engine.sha(self.fixture.binary),executable=True)
        self.assertFalse(target.exists());self.assertEqual(partial.stat().st_mode & 0o777,0o644)
        partial.unlink();partial.symlink_to(self.fixture.binary)
        with self.assertRaises(OSError):
            files.copy_file(self.fixture.binary,target,engine.sha(self.fixture.binary),executable=True)
        self.assertTrue(partial.is_symlink());self.assertFalse(target.exists())

    def test_identity_acceptance_and_predecessor_rejection_leave_defaults_unchanged(self):
        defaults={name:(self.root/name).read_bytes() for name in ('q4t','q4t.release.json','service.release.json')}
        value=engine.read(self.release);value['recovery_accepted']=False;engine.save(self.release,value)
        with self.assertRaisesRegex(RuntimeError,'accepted publication identity'):self.publish()
        self.assertEqual(defaults,{name:(self.root/name).read_bytes() for name in defaults})
        self.assertFalse((self.state/p.Store.marker_name).exists());self.assertEqual(self.business(),self.before)
        value['recovery_accepted']=True;engine.save(self.release,value)
        self.publish()
        with self.assertRaisesRegex(ValueError,'idempotency'):
            p.execute(self.state,request_id=self.request,root=self.root,package=self.target,
                digest=self.digest,release=self.release,expected='f'*64,revision=0)
        with self.assertRaisesRegex(ValueError,'predecessor'):
            p.execute(self.state,request_id=str(uuid.uuid4()),root=self.root,package=self.target,
                digest=self.digest,release=self.release,expected=self.digest,revision=0)

    def test_locks_active_grants_and_uncoordinated_deploy_are_refused(self):
        for name in ('publication.lock','deployment.lock','guardian.lock',store.LOCK):
            with self.subTest(lock=name),d.claim(self.state,name=name),self.assertRaises(BlockingIOError):self.publish()
        with mock.patch.object(native := p.native,'read_ledger',return_value=dict(desired='RUNNING',active=False)),\
             self.assertRaisesRegex(ValueError,'durable STOPPED'):self.publish()
        with mock.patch.object(native,'read_ledger',return_value=dict(desired='STOPPED',active=True)),\
             self.assertRaisesRegex(ValueError,'durable STOPPED'):self.publish()
        with self.lease.open('a+') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.publish()
        self.publish()
        with self.assertRaisesRegex(ValueError,'coordinated default publication'):
            d.switch(self.state,self.package,self.digest,expected=self.digest,request_id=str(uuid.uuid4()))
        self.check_complete()

    def test_lost_published_journal_never_rebootstraps_old_initial_intent(self):
        self.publish();shutil.rmtree(self.state/p.Store.directory_name)
        with self.assertRaises(OSError):self.resume()
        (self.state/p.Store.marker_name).unlink()
        with self.assertRaisesRegex(ValueError,'cannot be recreated'):self.resume()
        self.assertFalse((self.state/p.Store.directory_name).exists());self.assertEqual(self.business(),self.before)

    def test_fresh_default_resumes_after_initial_same_generation_selection(self):
        for name in files.ALIASES:(self.root/name).unlink()
        self.target=self.package
        self.release=accepted(self.target,self.digest,self.state,self.fixture.directory/'fresh.json')
        original=files.replace_link;fired=False
        def fail(path,target):
            nonlocal fired
            original(path,target)
            if path.name == 'current' and not fired:
                fired=True;raise OSError('CPU fresh current selected before aliases')
        with mock.patch.object(files,'replace_link',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertTrue(fired);self.resume();self.check_complete()


class NativePublicationTests(unittest.TestCase):
    def setUp(self):
        self.entry=test_lifecycle.NativeEntryTests()
        self.entry.setUp();self.addCleanup(self.entry.doCleanups)
        self.root=self.entry.root

    def test_real_native_cli_publishes_checks_rolls_back_and_preserves_state(self):
        first,args=self.entry.prepare(self.root/'first')
        package=Path(first['package']);digest=private_lease(package,self.root/'lease.lock')
        cli=package/'engine/q4t';state=self.root/'state'
        self.entry.invoke(cli,'init','--state',state)
        second,_=self.entry.prepare(self.root/'second',args=args)
        target=Path(second['package']);target_digest=private_lease(target,self.root/'lease.lock')
        root=self.root/'default';root.mkdir(mode=0o700)
        for name in ('q4t','q4t.release.json'):shutil.copyfile(package/'engine'/name,root/name)
        (root/'service').symlink_to(package)
        accepted(package,digest,state,root/'service.release.json')
        release=accepted(target,target_digest,state,self.root/'new-release.json')
        request=uuid.uuid4()
        before={str(path.relative_to(state)):path.read_bytes() for path in state.rglob('*.json')
                if path.parent.name.startswith('client-') or path.name == 'ledger.json'}
        result=self.entry.invoke(cli,'publish','--state',state,'--package',target,
            '--manifest-sha256',target_digest,'--default-root',root,'--service-release',release,
            '--expected-manifest-sha256',digest,'--expected-revision','0','--request-id',request)
        current=json.loads(result.stdout);self.assertTrue(current['published'])
        default=root/'q4t'
        self.assertTrue(json.loads(self.entry.invoke(default,'publish-status','--state',state).stdout)['ready'])
        self.entry.invoke(default,'check','--state',state)
        self.assertTrue(json.loads(self.entry.invoke(default,'publish-resume','--state',state,
            '--request-id',request).stdout)['replayed'])
        self.entry.invoke(default,'publish-rollback','--state',state,'--default-root',root,
            '--expected-manifest-sha256',target_digest,'--expected-revision',current['revision'],
            '--generation-sha256',current['history'][-1]['previous']['generation_sha256'],
            '--request-id',uuid.uuid4())
        self.entry.invoke(default,'check','--state',state)
        self.assertEqual(json.loads(self.entry.invoke(default,'status','--state',state).stdout)['state'],'STOPPED')
        self.assertEqual(before,{str(path.relative_to(state)):path.read_bytes() for path in state.rglob('*.json')
                if path.parent.name.startswith('client-') or path.name == 'ledger.json'})
        self.assertEqual(list((state/'runs').iterdir()),[])


if __name__ == '__main__':unittest.main()
