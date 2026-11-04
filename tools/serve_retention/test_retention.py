"""Private disk operations and real child lifetimes; no listeners or GPU."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.serve_retention import store as s, runner as r

BASE=s.ROOT/'.q4t-work/serve-retention-20261104'


class Store(unittest.TestCase):
    def setUp(self):
        self.umask=os.umask(0o077)
        parent=BASE/'fixtures';parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root=Path(tempfile.mkdtemp(dir=parent))
        self.store=self.root/'store';s.init(self.store)
        self.fds=[]

    def tearDown(self):
        for fd in self.fds:
            try:os.close(fd)
            except OSError:pass
        os.umask(self.umask)

    def generation(self,*,kind='backend',sealed=False,bytes_=32):
        folder,fd=s.reserve(self.store,kind,{},r.sup.process_identity(os.getpid()));self.fds.append(fd)
        with (folder/'log.txt').open('wb') as stream:stream.truncate(bytes_)
        if sealed:
            s.seal(folder,dict(cleanup_confirmed=True),fd)
            os.close(fd);self.fds.remove(fd)
        return folder,fd

    def expire(self):
        return s.plan(self.store,now=time.time()+20*86400)

    def test_fresh_private_store_and_policy(self):
        self.assertEqual(self.store.stat().st_mode&0o777,0o700)
        with self.assertRaises(FileExistsError):s.init(self.store)
        with self.assertRaises(RuntimeError):s.init(self.root/'bad',max_bytes=1)
        value=s.read(self.store/'store.json');value['policy']['max_generations']=True
        s.save(self.store/'store.json',value)
        with self.assertRaisesRegex(RuntimeError,'invalid store policy'):s.plan(self.store)

    def test_reservation_refuses_before_second_generation(self):
        self.store=self.root/'small';s.init(self.store,max_bytes=128*1024**2)
        self.generation()
        with self.assertRaisesRegex(RuntimeError,'capacity exhausted'):self.generation()
        self.assertEqual(len(list((self.store/'generations').iterdir())),1)

    def test_count_capacity_never_prunes_implicitly(self):
        self.store=self.root/'one';s.init(self.store,max_generations=1)
        old,_=self.generation(sealed=True)
        with self.assertRaisesRegex(RuntimeError,'capacity exhausted'):self.generation()
        self.assertTrue(old.exists())
        result=s.apply(self.store,s.plan(self.store))
        self.assertEqual(result['removed'],[old.name]);self.generation()

    def test_actual_sealed_bytes_count_against_next_reservation(self):
        self.store=self.root/'bytes';s.init(self.store,max_bytes=128*1024**2)
        old,_=self.generation(sealed=True,bytes_=40*1024**2)
        with self.assertRaisesRegex(RuntimeError,'capacity exhausted'):self.generation()
        plan=s.plan(self.store);self.assertEqual(plan['remove'][0]['name'],old.name)
        s.apply(self.store,plan);self.generation()

    def test_active_and_unsealed_are_not_expired(self):
        active,_=self.generation()
        unknown,fd=self.generation();os.close(fd);self.fds.remove(fd)
        value=self.expire()
        self.assertFalse(value['remove'])
        self.assertEqual({v['name']:v['state'] for v in value['inventory']},
                         {active.name:'ACTIVE',unknown.name:'UNSEALED'})

    def test_expired_sealed_generations_have_bounded_prune_batch(self):
        for _ in range(3):self.generation(sealed=True)
        first=self.expire();self.assertEqual(len(first['remove']),2)
        s.apply(self.store,first)
        second=self.expire();self.assertEqual(len(second['remove']),1)
        s.apply(self.store,second);self.assertFalse(list((self.store/'generations').iterdir()))

    def test_stale_plan_changes_nothing(self):
        old,_=self.generation(sealed=True);plan=self.expire()
        self.generation(kind='gateway')
        with self.assertRaisesRegex(RuntimeError,'changed since prune'):s.apply(self.store,plan)
        self.assertTrue(old.exists());self.assertFalse((self.store/'gc.json').exists())

    def test_changed_sealed_file_is_preserved(self):
        folder,_=self.generation(sealed=True);plan=self.expire()
        (folder/'log.txt').write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'sealed generation changed'):s.apply(self.store,plan)
        self.assertTrue(folder.exists())

    def test_symlink_and_hardlink_are_never_collected(self):
        folder,_=self.generation();outside=self.root/'outside';outside.write_bytes(b'keep')
        (folder/'alias').symlink_to(outside)
        with self.assertRaises(RuntimeError):s.tree(folder,2**20)
        (folder/'alias').unlink();os.link(outside,folder/'alias')
        with self.assertRaisesRegex(RuntimeError,'hardlinked'):s.tree(folder,2**20)
        self.assertEqual(outside.read_bytes(),b'keep')

    def test_unknown_generation_or_replaced_root_refuses(self):
        (self.store/'generations'/'unowned').mkdir()
        with self.assertRaisesRegex(RuntimeError,'unknown generation'):s.plan(self.store)
        (self.store/'generations'/'unowned').rmdir()
        meta=s.read(self.store/'store.json');meta['root_identity'][1]+=1;s.save(self.store/'store.json',meta)
        with self.assertRaisesRegex(RuntimeError,'identity/contract'):s.plan(self.store)

    def test_live_claim_can_be_inherited_without_reopening_lock(self):
        folder,fd=self.generation()
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'],pass_fds=(fd,))
        os.close(fd);self.fds.remove(fd)
        try:self.assertTrue(s.busy(folder))
        finally:child.wait(3)
        self.assertFalse(s.busy(folder));self.assertEqual(self.expire()['inventory'][0]['state'],'UNSEALED')

    def interrupted_prune(self):
        folder,_=self.generation(sealed=True)
        def interrupted(path):
            (path/'log.txt').unlink()
            raise OSError('injected interruption after one authorized unlink')
        with mock.patch.object(s.shutil,'rmtree',side_effect=interrupted) as fn:
            fn.avoids_symlink_attacks=True
            with self.assertRaisesRegex(OSError,'injected interruption'):s.apply(self.store,self.expire())
        self.assertTrue((self.store/'gc.json').exists())
        return folder,self.store/'generations'/('.trash-'+folder.name)

    def test_interrupted_prune_resumes_only_authorized_subset(self):
        folder,trash=self.interrupted_prune()
        with self.assertRaisesRegex(RuntimeError,'unfinished prune'):self.generation()
        self.assertTrue(trash.exists())
        result=s.resume_gc(self.store)
        self.assertEqual(result['removed'],[folder.name]);self.assertFalse(trash.exists())
        self.assertFalse((self.store/'gc.json').exists());self.generation()

    def test_modified_partial_prune_is_preserved(self):
        _,trash=self.interrupted_prune();(trash/'foreign').write_bytes(b'untouched')
        with self.assertRaisesRegex(RuntimeError,'prune file changed'):s.resume_gc(self.store)
        self.assertTrue((trash/'foreign').exists())

    def test_private_tail_is_bounded_and_never_overwrites(self):
        folder,fd=self.generation();(folder/'log.txt').write_bytes(b'private-client-id\n'*100)
        with self.assertRaisesRegex(RuntimeError,'only sealed'):s.tail(self.store,folder.name,'log.txt',self.root/'tail')
        s.seal(folder,dict(cleanup_confirmed=True),fd);os.close(fd);self.fds.remove(fd)
        value=s.tail(self.store,folder.name,'log.txt',self.root/'tail',17)
        self.assertEqual(value['bytes'],17);self.assertFalse(value['redacted'])
        self.assertEqual((self.root/'tail').stat().st_mode&0o777,0o600)
        with self.assertRaises(FileExistsError):s.tail(self.store,folder.name,'log.txt',self.root/'tail')
        with self.assertRaises(RuntimeError):s.tail(self.store,folder.name,'../store.json',self.root/'no')

    def test_duplicate_metadata_and_foreign_plan_refused(self):
        path=self.root/'duplicate';path.write_text('{"a":1,"a":2}')
        with self.assertRaisesRegex(RuntimeError,'duplicate'):s.read(path)
        self.generation(sealed=True);plan=self.expire();plan['store_id']='different'
        with self.assertRaisesRegex(RuntimeError,'different store'):s.apply(self.store,plan)


FAKE='''import argparse,json,os,signal,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action')
for n in ('package','manifest-sha256','output','run','backend-run'):p.add_argument('--'+n)
a=p.parse_args()
def save(path,value):path.write_text(json.dumps(value))
def owner():
 f=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
 return dict(pid=os.getpid(),start_ticks=int(f[19]),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
if a.action=='stop':
 save(Path(a.run)/'stop.request.json',{});raise SystemExit(0)
out=Path(a.output);out.mkdir()
save(out/'controller.json',dict(owner=owner(),package=a.package,manifest_sha256=a.manifest_sha256))
mode=Path(a.package,'mode').read_text()
if mode=='unknown':raise SystemExit(7)
stopped=False
def signal_stop(*_):
 global stopped
 stopped=True
signal.signal(signal.SIGTERM,signal_stop)
print('x'*600000,flush=True)
if mode!='fail':
 while not stopped and not (out/'stop.request.json').exists():time.sleep(.01)
save(out/'terminal.json',dict(state='FAILED' if mode=='fail' else 'STOPPED',cleanup_confirmed=True,
 backend_stopped_by_gateway=False,nginx=None))
raise SystemExit(1 if mode=='fail' else 0)
'''


class Runner(Store):
    # Only new lifecycle tests; do not inherit/repeat Store's test methods.
    def fixture(self,mode='normal'):
        entry=self.root/'fake.py';entry.write_text(FAKE)
        (self.root/'mode').write_text(mode)
        return dict(kind='gateway',package=str(self.root),manifest_sha256='0'*64,
            entry=str(entry),entry_sha256=s.pkg.sha(entry),
            python=dict(path=sys.executable,sha256=s.pkg.sha(sys.executable)),backend_run=str(self.root))

    def test_run_real_child_stop_and_rotation_seals(self):
        bound=self.fixture();errors=[]
        def stopper():
            try:
                until=time.monotonic()+10
                while time.monotonic()<until:
                    paths=list((self.store/'generations').glob('*/run/controller.json'))
                    if paths:
                        # Let the actual bounded logger consume an oversized write.
                        time.sleep(.1)
                        r.stop(self.store,paths[0].parent.parent.name);return
                    time.sleep(.01)
                raise TimeoutError('fixture never started')
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=stopper);thread.start()
        result=r.run(self.store,bound);thread.join(12)
        self.assertFalse(errors);self.assertFalse(thread.is_alive())
        self.assertEqual(result['state'],'STOPPED',result);self.assertTrue(result['sealed'])
        folder=Path(result['generation'])
        self.assertNotIn('sealed',s.read(folder/'wrapper-terminal.json'))
        self.assertLessEqual(sum(p.stat().st_size for p in folder.glob('wrapper.*') if p.suffix in ('.log','.previous')),512*1024)
        self.assertTrue((folder/'wrapper.previous').exists())
        self.assertEqual(s.plan(self.store)['inventory'][0]['state'],'SEALED')

    def test_failed_but_clean_terminal_remains_failed_when_sealed(self):
        value=r.run(self.store,self.fixture('fail'))
        self.assertEqual(value['state'],'FAILED');self.assertTrue(value['sealed'],value)
        terminal=json.loads((Path(value['generation'])/'run/terminal.json').read_text())
        self.assertEqual(terminal['state'],'FAILED')

    def test_unknown_exit_is_not_sealed_or_pruned(self):
        value=r.run(self.store,self.fixture('unknown'))
        self.assertEqual(value['state'],'FAILED');self.assertFalse(value['sealed'])
        self.assertEqual(self.expire()['inventory'][0]['state'],'UNSEALED')
        self.assertFalse(self.expire()['remove'])

    def test_wrapper_death_keeps_claim_until_managed_exit_then_explicit_reconcile(self):
        bound=self.fixture();config=self.root/'bound.json';s.save(config,bound)
        adoption=r.sup.processes.child_subreaper();adoption.__enter__()
        self.addCleanup(adoption.__exit__,None,None,None)
        launcher=self.root/'run.py'
        launcher.write_text('import sys,json\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(s.ROOT))+')\n'
            'from tools.serve_retention import runner\nrunner.run(Path('+repr(str(self.store))+'),json.loads(Path('
            +repr(str(config))+').read_text()))\n')
        log=(self.root/'launcher.log').open('wb')
        parent=subprocess.Popen([sys.executable,'-B',str(launcher)],stdout=log,stderr=subprocess.STDOUT)
        child=None
        try:
            until=time.monotonic()+10
            while time.monotonic()<until:
                paths=list((self.store/'generations').glob('*/run/controller.json'))
                if paths and list((paths[0].parent.parent).glob('wrapper.*')):
                    child=s.pkg.read(paths[0])['owner'];folder=paths[0].parent.parent
                    if (folder/'wrapper.previous').exists():break
                time.sleep(.01)
            else:raise AssertionError('managed fixture did not enter running state')
            parent.kill();parent.wait(3)
            until=time.monotonic()+5
            while time.monotonic()<until and (r.owner_live(child) or s.busy(folder)):
                try:os.waitpid(child['pid'],os.WNOHANG)
                except ChildProcessError:pass
                time.sleep(.02)
            self.assertFalse(r.owner_live(child));self.assertFalse(s.busy(folder))
            self.assertFalse((folder/'seal.json').exists())
            before=(folder/'run/terminal.json').read_bytes()
            self.assertEqual(r.reconcile(self.store,folder.name)['signals_sent'],0)
            self.assertEqual(before,(folder/'run/terminal.json').read_bytes())
        finally:
            if parent.poll() is None:parent.kill();parent.wait(3)
            if child and r.owner_live(child):r.sup.signal_owner(child,signal.SIGTERM)
            log.close()

    def test_current_fixed_packages_bind_without_starting(self):
        base=s.ROOT/'.q4t-work/serve-clients-managed-20261104'
        plan=s.pkg.read(base/'short/plan.json')
        backend=r.binding('backend',plan['package']['package'],plan['package']['manifest_sha256'])
        front=plan['gateways'][0]
        gateway=r.binding('gateway',front['package'],front['manifest_sha256'],base/'short/execution/managed-run')
        self.assertEqual(backend['entry_sha256'],s.pkg.sha(backend['entry']))
        self.assertEqual(gateway['entry_sha256'],s.pkg.sha(gateway['entry']))
        self.assertFalse(list((self.store/'generations').iterdir()))


def suite():
    loader=unittest.TestLoader()
    cases=loader.loadTestsFromTestCase(Store)
    cases.addTests(Runner(name) for name in Runner.__dict__ if name.startswith('test_'))
    return cases


if __name__=='__main__':unittest.TextTestRunner(verbosity=2).run(suite())
