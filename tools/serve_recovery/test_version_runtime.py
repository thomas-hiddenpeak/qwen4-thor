"""CPU-only sealed payload preparation, real journals and CLI wiring."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, deployment, ledger, profile_store, service
from tools.serve_recovery import test_version_registration, version_journal, version_launch
from tools.serve_recovery import version_policy, version_runtime, version_store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.base = test_version_registration.RegistrationTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.install()
        self.state = self.base.state;self.binding = self.base.binding
        self.core_before = {n:(self.state/n).read_bytes() for n in
            ['ledger.json', *(writer.directory_name+'/policy.json'
                              for writer in deployment.FAMILIES.values())]}

    def args(self,payload='fallback'):
        return dict(root_manifest_sha256=self.base.root['manifest_sha256'],payload=payload,
                    cleanup_complete=True,lease_continuous=True,roles_retired=True)

    def profiles(self):
        return profile_store.execute(self.binding,dict(sequence=1,op='load'))

    def preserve(self):
        for n,raw in self.core_before.items():self.assertEqual((self.state/n).read_bytes(),raw,n)

    def test_actual_profile_migration_keeps_pending_history_clock_and_retry(self):
        selected = self.base.consume();before = self.profiles()
        result = version_runtime.prepare(self.binding,selected,self.args())
        after = self.profiles()
        self.assertNotEqual(before['registry_sha256'],after['registry_sha256'])
        self.assertEqual(after['registry_sha256'],self.base.registration[
            'payloads']['fallback']['binding']['profile_registry_sha256'])
        for name in ('clock','pending','history','selected','not_before_ns','light_since_ns'):
            self.assertEqual(before[name],after[name],name)
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(version_runtime.prepare(self.binding,selected,self.args()),result)
        self.assertEqual(self.profiles(),after)
        self.preserve()

    def test_cleanup_retirement_and_registered_selection_required_before_migration(self):
        selected = self.base.consume();before = self.profiles()
        for name in ('cleanup_complete','lease_continuous','roles_retired'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                version_runtime.prepare(self.binding,selected,dict(self.args(),**{name:False}))
        with self.assertRaises(ValueError):
            version_runtime.prepare(self.binding,selected,self.args('primary'))
        with self.assertRaises(ValueError):
            version_runtime.prepare(self.binding,selected,dict(self.args(),root_manifest_sha256='f'*64))
        self.assertEqual(self.profiles(),before);self.preserve()

    def test_real_profile_rename_failure_resumes_same_switch_without_new_time(self):
        selected = self.base.consume();before = self.profiles();original = os.replace
        def fail(src,dst,**kwargs):
            fd=kwargs.get('dst_dir_fd')
            if str(src)=='policy.pending' and fd is not None and Path(
                    os.readlink('/proc/self/fd/'+str(fd))).name=='profile-selection':
                raise OSError('runtime migration before rename')
            return original(src,dst,**kwargs)
        with mock.patch.object(os,'replace',side_effect=fail),self.assertRaises(OSError):
            version_runtime.prepare(self.binding,selected,self.args())
        with self.assertRaises(ValueError):self.profiles()
        version_runtime.prepare(self.binding,selected,self.args())
        after = self.profiles()
        self.assertEqual(after['clock'],before['clock'])
        self.assertEqual(after['revision'],before['revision']+1)
        self.preserve()

    def test_profile_commit_then_lost_version_receipt_replays_without_reset(self):
        selected = self.base.consume()
        request=dict(id=str(uuid.uuid4()),expected_revision=selected['revision'],
                     operation='prepare',args=self.args())
        original = os.replace
        def fail(src,dst,**kwargs):
            fd=kwargs.get('dst_dir_fd')
            if str(src)=='policy.pending' and fd is not None and Path(
                    os.readlink('/proc/self/fd/'+str(fd))).name=='version-selection':
                raise OSError('version receipt after profile commit')
            return original(src,dst,**kwargs)
        with version_store.open_bound(self.binding) as writer:
            with mock.patch.object(os,'replace',side_effect=fail),self.assertRaises(OSError):
                version_journal.transact(writer,request,binding=self.binding)
        after = self.profiles()
        recovered = self.base.load()
        with version_store.open_bound(self.binding) as writer:
            receipt = version_journal.transact(writer,dict(request,id=str(uuid.uuid4()),
                expected_revision=recovered['revision']),binding=self.binding)
        self.assertEqual(receipt['result']['state']['switch'],selected['switch'])
        self.assertEqual(self.profiles(),after);self.preserve()

    def test_qualified_two_commands_share_current_state_and_separate_run_paths(self):
        run = self.state/'runs'/str(uuid.uuid4());run.mkdir(mode=0o700)
        cfg = version_launch.configuration(self.state,deployment.read_descriptor(self.state),run)
        self.assertEqual(cfg['registry'],self.base.registration['registry'])
        for name in ('primary','fallback'):
            args = cfg['payloads'][name]
            self.assertEqual(args['controller'][-1],str(run/name/'controller.json'))
            self.assertEqual(args['gateway'][-1],str(run/name/'gateway.json'))
            controller=json.loads((run/name/'controller.json').read_bytes())
            gateway=json.loads((run/name/'gateway.json').read_bytes())
            self.assertEqual(controller['limits'],self.binding)
            self.assertEqual(controller['profiles'],self.binding)
            self.assertEqual(gateway['access'],self.binding)
            self.assertEqual(args['binding'],self.base.registration['payloads'][name]['binding'])
        self.preserve()

    def call(self,action,*args):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=service.main([action,'--state',str(self.state),*args],
                package=self.base.root['package'],digest=self.base.root['manifest_sha256'])
        value=json.loads(output.getvalue());self.assertEqual(code,0,value)
        return value

    def test_cli_register_exact_file_and_status_keep_original_state(self):
        path=self.base.base.fixture.directory/'registration.json'
        self.base.base.fixture.save(path,self.base.registration)
        self.call('version-register','--registration',str(path))
        value=self.call('version-status')
        self.assertEqual(value['launch_payload'],'primary')
        self.assertFalse(value['fallback_consumed']);self.preserve()

    def test_cli_prepares_frozen_registration_without_starting_or_changing_cycle(self):
        path=self.base.base.fixture.directory/'prepared-registration.json'
        cycle=str(uuid.uuid4());before=self.base.load()
        value=self.call('version-prepare','--registration',str(path),'--cycle-id',cycle,
            '--fallback-package',self.base.fallback['package'],
            '--fallback-manifest-sha256',self.base.fallback['manifest_sha256'],
            '--fallback-release',str(self.base.release),'--fallback-decision',str(self.base.decision))
        self.assertEqual(value['cycle_id'],cycle)
        self.assertFalse(value['automatic_start'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.base.load(),before);self.preserve()

    def test_terminal_cycle_never_prepares_or_resets_a_payload(self):
        selected = self.base.consume()
        signal = copy.deepcopy(selected['switch']['fault'])
        signal.update(generation=2,payload_sha256=selected['registry']['payloads']['fallback']['manifest_sha256'])
        proof=dict(desired='RUNNING',boot_id=selected['boot_id'],termination_requested=False,
            payload_sha256=signal['payload_sha256'],generations={r:2 for r in version_policy.ROLES},
            **{k:True for k in version_policy.GUARDS})
        terminal=version_policy.observe(selected,signal,proof,str(uuid.uuid4()))
        version_store.execute(self.binding,dict(sequence=1,op='commit',request_id=str(uuid.uuid4()),
            expected_revision=selected['revision'],candidate=terminal))
        before=self.profiles()
        with self.assertRaisesRegex(ValueError,'exhausted'):
            version_runtime.prepare(self.binding,terminal,self.args())
        with self.assertRaisesRegex(ValueError,'exhausted'):
            version_launch.configuration(self.state,deployment.read_descriptor(self.state),
                                         self.state/'unused-run')
        self.assertFalse((self.state/'unused-run').exists())
        self.assertEqual(self.profiles(),before);self.preserve()


if __name__=='__main__':unittest.main()
