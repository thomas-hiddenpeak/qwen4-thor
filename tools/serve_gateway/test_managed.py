"""CPU contracts with real child cleanup and nginx syntax; no HTTP/model."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools.serve_gateway import managed as m
from tools.serve_gateway.test_gateway import certificate,example,VENDOR


class ManagedTests(unittest.TestCase):
    def setUp(self):
        parent=m.ROOT/'build/managed-gateway-tests';parent.mkdir(exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=parent))

    def fake(self,observe,*,dependency=None,script=None,patch=None,stop_before=False):
        out=self.root/'run';out.mkdir(mode=0o700)
        cfg=dict(m.SETTINGS,poll_seconds=.01,startup_seconds=.15,
            drain_seconds=.3,term_seconds=.2,kill_seconds=.5,log_bytes=128)
        child=self.root/'child-ready'
        code=("import pathlib,signal,sys,time\n"
              "signal.signal(signal.SIGQUIT,lambda *_:sys.exit(0))\n"
              "signal.signal(signal.SIGTERM,lambda *_:sys.exit(12))\n"
              f"pathlib.Path({str(child)!r}).write_text('ready')\n"+
              (script or 'time.sleep(30)\n'))
        gateway=dict(policy=dict(upstream_port=1,listen_host='127.0.0.1',listen_port=2),
                     nginx=dict(sha256='fixture'),files={})
        if stop_before:m.intent(out,'fixture')
        def observer(*args):
            if not child.exists():raise ValueError('fixture starting')
            return observe(out,*args)
        command=[sys.executable,'-c',code]
        (self.root/'gateway/runtime').mkdir(parents=True)
        with mock.patch.object(m,'child_command',return_value=command):
            context=patch or mock.patch.object(m,'__doc__',m.__doc__)
            with context,m.claim(self.root) as fd:
                rc=m.supervise(self.root,'fixture',out,dict(settings=cfg),gateway,{},fd,
                    observe=observer,dependency=dependency or (lambda *_:None))
        state=m.read(out/'terminal.json')
        if state.get('nginx'):self.assertFalse(m.sup.processes.group_members(state['nginx']['pid']))
        return rc,state,out

    def test_operator_stop_drains_owned_group(self):
        count=0
        def observe(out,owner,*_):
            nonlocal count
            count+=1
            if count==2:m.intent(out,'operator')
            return dict(owner=owner,listener_inode=1)
        rc,state,_=self.fake(observe)
        self.assertEqual((rc,state['state'],state['nginx_returncode']),(0,'STOPPED',0))
        self.assertTrue(state['cleanup_confirmed'])
        self.assertFalse(state['backend_stopped_by_gateway'])

    def test_stop_before_launch_never_spawns(self):
        rc,state,_=self.fake(lambda *_:None,stop_before=True)
        self.assertEqual((rc,state['starts'],state['state']),(0,0,'STOPPED'))

    def test_changed_listener_fails_and_cleans(self):
        count=0
        def observe(out,owner,*_):
            nonlocal count
            count+=1;return dict(owner=owner,listener_inode=count)
        rc,state,_=self.fake(observe)
        self.assertEqual((rc,state['state'],state['restarts']),(1,'FAILED',0))
        self.assertIn('listener changed',state['error']);self.assertTrue(state['cleanup_confirmed'])

    def test_lost_backend_closes_gateway_only(self):
        count=0
        def dependency(*_):
            nonlocal count
            count+=1
            if count==5:raise ValueError('backend identity changed')
        rc,state,_=self.fake(lambda out,owner,*_:dict(owner=owner),dependency=dependency)
        self.assertEqual(rc,1);self.assertTrue(state['cleanup_confirmed'])
        self.assertIn('backend identity changed',state['error'])

    def test_master_death_reaps_orphan_worker_without_restart(self):
        worker=self.root/'orphan'
        script=(f"p=__import__('subprocess').Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
                f"pathlib.Path({str(worker)!r}).write_text(str(p.pid))\n"
                "time.sleep(.04)\nsys.exit(9)\n")
        rc,state,_=self.fake(lambda out,owner,*_:dict(owner=owner),script=script)
        self.assertEqual((rc,state['starts'],state['restarts']),(1,1,0))
        self.assertTrue(state['cleanup_confirmed']);self.assertEqual(state['nginx_returncode'],9)
        self.assertFalse(Path('/proc',worker.read_text()).exists())

    def test_startup_deadline_is_terminal(self):
        def observe(*_):raise ValueError('not yet listening')
        rc,state,_=self.fake(observe)
        self.assertEqual((rc,state['state']),(1,'FAILED'))
        self.assertTrue(state['cleanup_confirmed'])
        self.assertIn('not yet listening',state['error'])

    def test_log_failure_stops_and_does_not_hide_cleanup(self):
        original=m.sup.RotatingLog.write
        def write(logger,data):
            if logger.path.name=='gateway.log':raise OSError('fixture disk full')
            return original(logger,data)
        patch=mock.patch.object(m.sup.RotatingLog,'write',write)
        rc,state,_=self.fake(lambda out,owner,*_:dict(owner=owner),patch=patch,
                            script="print('log',flush=True)\ntime.sleep(30)\n")
        self.assertEqual(rc,1);self.assertTrue(state['cleanup_confirmed'])
        self.assertIn('log capture failed',state['error'])

    def test_graceful_timeout_still_reaps(self):
        def observe(out,owner,*_):
            m.intent(out,'operator');return dict(owner=owner)
        rc,state,_=self.fake(observe,
            script='signal.signal(signal.SIGQUIT,signal.SIG_IGN)\ntime.sleep(30)\n')
        self.assertEqual(rc,1);self.assertTrue(state['cleanup_confirmed'])
        self.assertIn('graceful drain',state['error'])

    def test_log_rotation_has_fixed_physical_byte_bound(self):
        logger=m.sup.RotatingLog(self.root/'gateway.log',128)
        data=bytes(range(256))*20
        logger.write(data)
        paths=(self.root/'gateway.previous',self.root/'gateway.log')
        self.assertEqual(b''.join(p.read_bytes() for p in paths),data[-256:])
        self.assertTrue(all(p.stat().st_size<=128 and p.stat().st_mode & 0o077==0 for p in paths))

    def test_claim_contention_does_not_release_owner(self):
        folder=self.root/'gateway/runtime';folder.mkdir(parents=True)
        with m.claim(self.root):
            with self.assertRaises(BlockingIOError):
                with m.claim(self.root):pass
            with self.assertRaises(BlockingIOError):
                with m.claim(self.root):pass
        with m.claim(self.root):pass

    def test_claim_refuses_symlink(self):
        folder=self.root/'gateway/runtime';folder.mkdir(parents=True)
        target=self.root/'foreign';target.write_text('untouched')
        (folder/'managed.lock').symlink_to(target)
        with self.assertRaises(OSError):
            with m.claim(self.root):pass
        self.assertEqual(target.read_text(),'untouched')

    def test_absent_owner_stop_is_durable_and_not_cleanup_proof(self):
        m.pkg.save(self.root/'controller.json',dict(owner=dict(pid=99999999,start_ticks=1,boot_id='missing')))
        result=m.stop(self.root)
        self.assertEqual(result['state'],'OWNER_ABSENT')
        self.assertFalse(result['cleanup_confirmed']);self.assertTrue((self.root/'stop.request.json').exists())
        before=(self.root/'stop.request.json').read_bytes()
        self.assertEqual(m.stop(self.root),result)
        self.assertEqual(before,(self.root/'stop.request.json').read_bytes())

    def test_terminal_stop_does_not_signal_or_rewrite(self):
        m.pkg.save(self.root/'controller.json',dict(owner=m.sup.process_identity(os.getpid())))
        m.pkg.save(self.root/'terminal.json',dict(state='FAILED',cleanup_confirmed=True))
        with mock.patch.object(m.sup,'signal_owner') as signal_owner:
            self.assertEqual(m.stop(self.root),m.read(self.root/'terminal.json'))
            signal_owner.assert_not_called()
        self.assertFalse((self.root/'stop.request.json').exists())

    def test_recycled_pid_never_receives_stop(self):
        owner=m.sup.process_identity(os.getpid());owner['start_ticks']+=1
        m.pkg.save(self.root/'controller.json',dict(owner=owner))
        with mock.patch.object(signal,'pidfd_send_signal') as send:
            self.assertFalse(m.stop(self.root)['signal_sent']);send.assert_not_called()

    def test_backend_mismatch_and_busy_states(self):
        control=self.root/'controller.json';m.pkg.save(control,dict(test=True))
        owner=m.sup.process_identity(os.getpid())
        binding=dict(run=str(self.root),controller_sha256=m.pkg.sha(control),controller=owner,
                     worker=owner,backend=owner,process=dict(test=True))
        state=dict(controller_live=True,state='DEGRADED',worker=dict(owner=owner,backend=owner))
        with mock.patch.object(m.service,'status',return_value=state), \
             mock.patch.object(m.identity,'process',return_value=binding['process']):
            m.check_backend(binding,1)
            for status in ('STOPPING','STOPPED','OWNER_ABSENT'):
                state['state']=status
                with self.assertRaises(ValueError):m.check_backend(binding,1)
            state['state']='READY';state['worker']['backend']=dict(owner,pid=123)
            with self.assertRaises(ValueError):m.check_backend(binding,1)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent=m.ROOT/'build/managed-gateway-package-tests';parent.mkdir(exist_ok=True)
        cls.root=Path(tempfile.mkdtemp(dir=parent))
        cls.cert,cls.key=certificate(cls.root)
        cls.policy=cls.root/'policy-private.json';m.g.save(cls.policy,json.dumps(example()).encode())
        cls.nginx=m.pkg.read(VENDOR)['binary']

    def package(self,label):
        return m.prepare(self.policy,self.nginx,self.cert,self.key,self.root/label)

    def test_snapshot_checks_copied_nginx_and_sources(self):
        receipt=self.package('snapshot')
        package=Path(receipt['package'])
        manifest,gateway=m.verify(package,receipt['manifest_sha256'])
        self.assertEqual(gateway['nginx']['path'],str(package/'nginx'))
        self.assertEqual(m.pkg.sha(package/'nginx'),m.pkg.sha(self.nginx))
        for entry in ('managed.py','managed_child.py'):
            value=subprocess.run([manifest['python']['path'],'-I','-B',
                str(package/'source/tools/serve_gateway'/entry),'--help'],capture_output=True,timeout=10)
            self.assertEqual(value.returncode,0,value.stderr.decode())
        source=package/'source/tools/serve_gateway/managed_child.py'
        source.chmod(0o600);source.write_text(source.read_text()+'\n')
        with self.assertRaisesRegex(ValueError,'source changed'):m.verify(package,receipt['manifest_sha256'])

    def test_unit_is_real_and_has_no_automatic_activation(self):
        receipt=self.package('unit')
        text=m.unit(receipt['package'],receipt['manifest_sha256'],self.root/'backend',self.root/'run')
        self.assertIn('Restart=no',text);self.assertIn('KillMode=mixed',text)
        self.assertNotIn('[Install]',text)
        unit=self.root/'q4t-managed-gateway-cpu.service';unit.write_text(text)
        value=subprocess.run(['systemd-analyze','--user','verify',str(unit)],capture_output=True,timeout=10)
        (self.root/'unit-verify.log').write_bytes(value.stdout+value.stderr)
        self.assertEqual(value.returncode,0,value.stderr.decode())

    def test_original_package_and_run_directories_cannot_be_reused(self):
        receipt=self.package('no-overwrite')
        with self.assertRaises(FileExistsError):self.package('no-overwrite')
        output=self.root/'existing-run';output.mkdir(mode=0o700)
        with self.assertRaises(FileExistsError):
            m.run(receipt['package'],receipt['manifest_sha256'],self.root/'backend',output)


if __name__=='__main__':unittest.main()
