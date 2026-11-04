"""Pinned in-memory TLS trust, explicit durable handoff and interrupted commits."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.project_tasks import engine as e,store,migration as m,retention as r,runner,transport,backup
from tools.project_tasks import test_tasks as old,test_archive,test_errors,migration_fixtures as f
from tools.serve_gateway import tls_fixture as pki

BASE=store.ROOT/'.q4t-work/serve-task-trust-20261104'


def handshake(context,cert,key,host=pki.HOST):
    server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server.load_cert_chain(cert,key)
    si,so,ci,co=[ssl.MemoryBIO() for _ in range(4)]
    ss=server.wrap_bio(si,so,server_side=True);cs=context.wrap_bio(ci,co,server_hostname=host);done=set()
    for _ in range(64):
        for label,sock in (('server',ss),('client',cs)):
            if label in done:continue
            try:sock.do_handshake();done.add(label)
            except ssl.SSLWantReadError:pass
        if data:=so.read():ci.write(data)
        if data:=co.read():si.write(data)
        if len(done)==2:return hashlib.sha256(cs.getpeercert(binary_form=True)).hexdigest()
    raise AssertionError('bounded MemoryBIO handshake')


class Materials(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        BASE.mkdir(exist_ok=True);parent=BASE/'material-fixtures';parent.mkdir(exist_ok=True)
        cls.root=Path(tempfile.mkdtemp(dir=parent));now=int(time.time())
        cls.authorities=[pki.ca(cls.root/f'ca-{i}') for i in range(2)]
        cls.leaves=[pki.issue(cls.root/f'leaf-{i}','server',ca,now-60,now+86400) for i,ca in enumerate(cls.authorities)]

    def setUp(self):
        self.directory=Path(tempfile.mkdtemp(dir=self.root));self.ca=self.directory/'ca.pem'
        store.write_new(self.ca,self.authorities[0][0].read_bytes())
        self.path=self.directory/'profile.json'
        self.input={k:v for k,v in old.BINDING.items() if k not in ('ca_sha256','key_sha256')}
        self.input.update(api_key='private-cpu-fixture-'+'x'*40,ca_file=str(self.ca))
        store.write_new(self.path,store.encode(self.input))

    def test_snapshot_used_after_replacement_and_deletion(self):
        value,binding=transport.profile(self.path);raw=self.ca.read_bytes()
        replacement=self.directory/'next.pem';store.write_new(replacement,self.authorities[1][0].read_bytes());os.replace(replacement,self.ca)
        after,new=transport.profile(self.path);self.ca.unlink()
        self.assertEqual(binding['ca_sha256'],hashlib.sha256(raw).hexdigest());self.assertNotEqual(binding['ca_sha256'],new['ca_sha256'])
        with patch.dict(os.environ,{'SSL_CERT_FILE':str(self.authorities[1][0]),'SSLKEYLOGFILE':str(self.directory/'keys.log')}):
            prior=transport.Client(value);current=transport.Client(after)
            handshake(prior.context,*self.leaves[0]);handshake(current.context,*self.leaves[1])
            with self.assertRaises(ssl.SSLCertVerificationError):handshake(prior.context,*self.leaves[1])
            with self.assertRaises(ssl.SSLCertVerificationError):handshake(current.context,*self.leaves[0])
        self.assertFalse((self.directory/'keys.log').exists())
        self.assertEqual(prior.context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(prior.context.check_hostname)

    def test_hostname_still_verified_and_profile_snapshot_immutable(self):
        value,_=transport.profile(self.path)
        with self.assertRaises(TypeError):value['base_url']='https://other/v1'
        with self.assertRaises(TypeError):value.data['api_key']='different'
        with self.assertRaises(ValueError):transport.Client(dict(value))
        with self.assertRaises(ssl.SSLCertVerificationError):handshake(transport.Client(value).context,*self.leaves[0],host='other.test')

    def test_malformed_oversize_private_key_leaf_only_and_symlink_rejected(self):
        for raw in (b'',b'not PEM',b'x'*65537,self.authorities[0][1].read_bytes(),self.leaves[0][0].read_bytes(),
                self.authorities[0][0].read_bytes()*9,self.authorities[0][0].read_bytes()+b'extra data'):
            self.ca.write_bytes(raw)
            with self.assertRaises(store.ContractError) as caught:transport.profile(self.path)
            self.assertEqual(caught.exception.code,'invalid_profile')
        self.ca.unlink();self.ca.symlink_to(self.authorities[0][0])
        with self.assertRaises(store.ContractError):transport.profile(self.path)

    def test_ca_change_during_open_is_rejected_without_network(self):
        original=os.open
        def opened(path,*args,**kwargs):
            fd=original(path,*args,**kwargs)
            if Path(path)==self.ca:
                self.ca.write_bytes(self.authorities[1][0].read_bytes())
            return fd
        with patch.object(transport.os,'open',side_effect=opened),self.assertRaises(store.ContractError):transport.profile(self.path)

    def test_fifo_never_blocks_and_multi_ca_bundle_is_explicit(self):
        self.ca.unlink();os.mkfifo(self.ca,0o600)
        with self.assertRaises(store.ContractError):transport.profile(self.path)
        self.ca.unlink();store.write_new(self.ca,b''.join(v[0].read_bytes() for v in self.authorities))
        value,binding=transport.profile(self.path);context=transport.Client(value).context
        self.assertEqual(binding['ca_sha256'],hashlib.sha256(self.ca.read_bytes()).hexdigest())
        handshake(context,*self.leaves[0]);handshake(context,*self.leaves[1])


class Trust(unittest.TestCase):
    setUp=old.Tasks.setUp;execute=old.Tasks.execute;status=old.Tasks.status
    count=old.Tasks.count;break_at=old.Tasks.break_at

    def plan(self,binding=None,label='trust'):
        self.target=binding or dict(self.binding,ca_sha256='e'*64)
        self.path=self.directory/(label+'.json')
        self.prepared=m.prepare(self.root,self.target,'key',self.path,'explicit reviewed CA handoff',trust=True)
        return self.prepared

    def apply(self):return m.apply(self.root,self.target,'key',self.path,self.prepared['plan_sha256'],trust=True)

    def test_saved_tool_budget_history_and_duplicates_preserved(self):
        self.break_at('tool_result')
        with store.open_task(self.root,self.name) as ledger:meta=deepcopy(ledger.meta);events=ledger.events();raw=ledger.wire(1).read_bytes()
        self.plan();moved=self.apply();self.assertTrue(self.prepared['ca_changed']);self.assertFalse(self.prepared['key_changed'])
        with store.open_task(self.root,self.name) as ledger:
            self.assertEqual(ledger.meta,meta);self.assertEqual(ledger.events()[:-1],events);self.assertEqual(ledger.wire(1).read_bytes(),raw)
        with self.assertRaises(store.ContractError):self.status()
        self.binding=self.target;client=old.FakeClient();result=self.execute(client)
        self.assertEqual((result['state'],result['requests'],result['saved_tool_results'],self.count()),('COMPLETED',2,1,1))
        self.assertEqual((len(client.calls),len(client.preflight_calls)),(1,1))
        duplicate=runner.submit(self.root,self.binding,'key','read current',client,stream=False)
        self.assertFalse(duplicate['created']);self.assertEqual(len(client.calls),1)
        self.assertTrue(self.apply()['already_applied']);self.assertEqual(self.status()['requests'],2)

    def test_unknown_request_and_tool_remain_unknown(self):
        for point in ('request','tool_started'):
            self.setUp();self.break_at(point);self.plan();moved=self.apply();client=old.FakeClient()
            value=runner.resume(self.root,self.target,'key',client)
            self.assertEqual(value['state'],'REQUEST_UNKNOWN' if point=='request' else 'TOOL_UNKNOWN')
            self.assertEqual(value['revision'],moved['revision']);self.assertEqual((client.cap_calls,client.calls,self.count()),(0,[],0))

    def test_preflight_is_rechecked_and_terminal_answer_never_reexecuted(self):
        self.break_at('preflight_result');self.plan();self.apply();self.binding=self.target
        client=old.FakeClient();done=self.execute(client);self.assertEqual(len(client.preflight_calls),2)
        self.plan(dict(self.binding,ca_sha256='f'*64),'terminal');moved=self.apply();self.binding=self.target
        client=old.FakeClient();after=self.execute(client)
        self.assertEqual((after['answer'],after['requests'],after['revision']),(done['answer'],done['requests'],moved['revision']))
        self.assertEqual((client.cap_calls,client.calls,self.count()),(0,[],1))

    def test_ca_only_and_old_key_route_stays_strict(self):
        changed=dict(self.binding,ca_sha256='e'*64)
        with self.assertRaises(store.ContractError):m.same_identity(self.binding,changed)
        for field,value in (('client_id','beta'),('base_url','https://other/v1'),('connect_host','127.0.0.2'),
                ('model','other'),('protocol_sha256','f'*64),('template_sha256','f'*64),('key_sha256','f'*64),('ca_sha256','bad')):
            with self.subTest(field=field),self.assertRaises(store.ContractError):m.same_trust_identity(self.binding,dict(changed,**{field:value}))
        with self.assertRaises(store.ContractError):m.same_trust_identity(self.binding,self.binding)

    def test_mixed_plan_types_digest_and_stale_revision_rejected(self):
        self.plan()
        with self.assertRaises(store.ContractError):m.apply(self.root,self.target,'key',self.path,self.prepared['plan_sha256'])
        with self.assertRaises(store.ContractError):m.apply(self.root,self.target,'key',self.path,'0'*64,trust=True)
        path=self.directory/'key.json';key=dict(self.binding,key_sha256='f'*64);p=m.prepare(self.root,key,'key',path,'key rotation')
        with self.assertRaises(store.ContractError):m.apply(self.root,key,'key',path,p['plan_sha256'],trust=True)
        self.execute()
        with self.assertRaises(store.ContractError) as caught:self.apply()
        self.assertEqual(caught.exception.code,'plan_stale')

    def test_plan_budget_source_files_and_exclusive_owner_bound(self):
        self.plan();original=json.loads(self.path.read_bytes())
        for key,value in (('max_tokens',64),('max_requests',8),('question','different')):
            edited=deepcopy(original);edited['target_spec'][key]=value;self.path.write_bytes(store.encode(edited))
            with self.assertRaises(store.ContractError):m.apply(self.root,self.target,'key',self.path,store.digest(edited),trust=True)
        self.path.write_bytes(store.encode(original))
        with store.open_task(self.root,self.name),self.assertRaises(BlockingIOError):self.apply()
        with patch.object(e,'sources',return_value={}),self.assertRaises(store.ContractError):self.apply()

    def test_accepted_11f_ledger_can_be_explicitly_upgraded_with_ca(self):
        root=self.directory/'old';store.init(root);item=f.clone_history(5,root,'key','tool_result')
        target=dict(item['binding'],ca_sha256='e'*64);path=self.directory/'old.json'
        plan=m.prepare(root,target,'key',path,'registered 11f handoff',trust=True)
        self.assertEqual(plan['source_release'],'srv11f-225e82bd')
        value=m.apply(root,target,'key',path,plan['plan_sha256'],trust=True)
        self.assertEqual((value['requests'],value['saved_tool_results']),(1,1));self.assertTrue(value['no_execution'])
        with store.open_task(root,item['name']) as ledger:self.assertEqual(e.replay(ledger)['execution_spec']['runtime_sources'],e.sources())

    def test_shared_migration_budget_cannot_be_reset_by_alternating_modes(self):
        for i in range(8):
            trust=bool(i%2);field='ca_sha256' if trust else 'key_sha256';target=dict(self.binding,**{field:hashlib.sha256(str(i).encode()).hexdigest()})
            path=self.directory/f'plan-{i}.json';p=m.prepare(self.root,target,'key',path,'bounded rotation',trust=trust)
            value=m.apply(self.root,target,'key',path,p['plan_sha256'],trust=trust);self.binding=target
            self.assertEqual((value['migrations'],value['runs']),(i+1,0))
        with self.assertRaises(store.ContractError) as caught:self.plan(dict(self.binding,ca_sha256='e'*64),'limit')
        self.assertEqual(caught.exception.code,'task_budget')

    def test_sigkill_before_and_after_active_commit(self):
        for point in ('before_commit','after_commit'):
            self.setUp();self.break_at('tool_result');self.plan();marker=self.directory/'marker.json'
            script="""import os,sys,threading
from pathlib import Path
from tools.project_tasks import store,migration
root,path,digest,point,marker=sys.argv[1:]
plan=__import__('json').loads(Path(path).read_bytes())
def hook(kind,ledger):
 if kind==point:
  store.write_new(Path(marker),store.encode(dict(pid=os.getpid(),point=point)))
  threading.Event().wait()
migration.apply(root,plan['target_spec']['binding'],'key',path,digest,trust=True,checkpoint=hook)
"""
            child=subprocess.Popen([sys.executable,'-B','-c',script,str(self.root),str(self.path),self.prepared['plan_sha256'],point,str(marker)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=store.ROOT)
            try:
                end=time.monotonic()+5
                while not marker.exists() and child.poll() is None and time.monotonic()<end:time.sleep(.01)
                self.assertTrue(marker.exists());child.kill();out,err=child.communicate(timeout=5);self.assertEqual(child.returncode,-signal.SIGKILL)
                store.write_new(self.directory/('killed-trust-'+point+'.json'),store.encode(dict(point=point,pid=child.pid,returncode=child.returncode)))
                value=self.apply();self.assertEqual(value['already_applied'],point=='after_commit');self.assertEqual(value['saved_tool_results'],1)
            finally:
                if child.poll() is None:child.kill();child.communicate(timeout=5)


class ArchivedTrust(Trust):
    # Only the archive-specific tests are loaded below, without repeating the
    # active-state suite inherited for common fixture/plan helpers.
    def archived(self,prune=False):
        done=self.execute();r.enable(self.root,min_retain_seconds=0)
        for kind in (('archive','prune') if prune else ('archive',)):
            path=self.directory/(kind+'.json');p=r.prepare(self.root,self.binding,'key',kind,path,'CPU terminal fixture')
            r.apply(self.root,self.binding,'key',path,p['plan_sha256'])
        return done

    def test_retained_and_purged_permanent_keys_preserve_business_contract(self):
        for prune in (False,True):
            self.setUp();done=self.archived(prune);path=self.root/'retention';_,meta=store.checked_root(self.root)
            original,state=r.load_key(path,meta,self.name)
            before=backup.regular_tree(path/'payloads')
            self.plan();value=self.apply();self.assertEqual(value['payload_state'],'purged' if prune else 'retained')
            current,_=r.load_key(path,meta,self.name);self.assertEqual(original['base'],current['base'])
            self.assertEqual(before,backup.regular_tree(path/'payloads'))
            client=old.FakeClient();duplicate=runner.submit(self.root,self.target,'key','read current',client,stream=False)
            self.assertFalse(duplicate['created']);self.assertEqual(client.calls,[]);self.assertTrue(self.apply()['already_applied'])
            with self.assertRaises(store.ContractError):runner.resume(self.root,self.binding,'key',client)
            with self.assertRaises(store.ContractError):runner.submit(self.root,self.target,'key','changed',client,stream=False)
            answer=r.lookup(self.root,self.target,'key',include_answer=True)
            if not prune:self.assertEqual(answer['answer'],done['answer'])
            self.assertEqual(self.count(),1)

    def test_archive_plan_type_budget_and_backup_restore(self):
        self.archived();self.plan()
        with self.assertRaises(store.ContractError):m.apply(self.root,self.target,'key',self.path,self.prepared['plan_sha256'])
        self.apply();self.binding=self.target
        snapshot=self.directory/'snapshot';saved=backup.snapshot(self.root,snapshot)
        path=self.directory/'restore.json';dest=self.directory/'restored'
        planned=backup.restore_plan(self.root,snapshot,saved['snapshot_sha256'],dest,path,'trust history restore')
        backup.restore_apply(path,planned['plan_sha256'])
        self.assertEqual(r.lookup(dest,self.binding,'key',include_answer=True)['payload_state'],'retained')
        self.root=dest
        for i in range(7):
            self.plan(dict(self.binding,ca_sha256=hashlib.sha256(str(i).encode()).hexdigest()),'again-'+str(i));self.apply();self.binding=self.target
        with self.assertRaises(store.ContractError) as caught:self.plan(dict(self.binding,ca_sha256='f'*64),'exhausted')
        self.assertEqual(caught.exception.code,'task_budget')

    def test_real_sigkill_archive_trust_transaction_then_explicit_finish(self):
        for point in ('metadata_before_commit','key_published'):
            self.setUp();self.archived();self.plan();self.plan_path=self.path
            marker=test_archive.Archives.kill(self,point)
            if point=='metadata_before_commit':self.apply()
            else:
                with self.assertRaises(store.ContractError):r.lookup(self.root,self.target,'key')
                r.finish(self.root,marker['operation_sha256'])
            self.assertTrue(self.apply()['already_applied']);self.assertEqual(r.lookup(self.root,self.target,'key')['payload_state'],'retained')


class Commands(test_errors.Errors):
    def test_trust_commands_redact_errors_and_never_execute(self):
        ca=self.directory/'new-ca.pem';ca.write_bytes(test_errors.CA.read_bytes()+b'\n');value=json.loads(self.profile.read_bytes());value['ca_file']=str(ca)
        self.profile.write_bytes(store.encode(value));target=transport.profile(self.profile)[1];before=backup.regular_tree(self.root)
        self.cli('plan-trust','invalid_arguments',extra=('--plan',self.directory/'missing-reason'))
        self.assertEqual(backup.regular_tree(self.root),before)
        path=self.directory/'trust.json';p=json.loads(self.cli('plan-trust',extra=('--plan',path,'--reason','reviewed trust'),expected=0).stdout)
        self.cli('apply-migration','plan_changed',extra=('--plan',path,'--plan-sha256',p['plan_sha256']))
        applied=json.loads(self.cli('apply-trust',extra=('--plan',path,'--plan-sha256',p['plan_sha256']),expected=0).stdout)
        self.assertTrue(applied['no_execution']);self.assertEqual(applied['requests'],0)
        self.assertEqual(runner.inspect(self.root,target,'key')['state'],'READY');self.assertEqual(self.count(),0)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(c) for c in (Materials,Trust))
    for cls in (ArchivedTrust,Commands):
        for name in cls.__dict__:
            if name.startswith('test_'):suite.addTest(cls(name))
    return suite
