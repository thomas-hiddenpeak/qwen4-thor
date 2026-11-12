"""Outer observer death must not become an operator stop of healthy CPU work."""
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

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import control, outer_instance, outer_tree, store
from tools.serve_recovery import test_runtime as fixture


def until(predicate, seconds=5):
    end=time.monotonic()+seconds
    while True:
        value=predicate()
        if value:return value
        if time.monotonic()>=end:raise TimeoutError('CPU outer parent fixture')
        time.sleep(.01)


def launcher(output):
    original=subprocess.Popen;held=[]
    def spawn(argv,**options):
        if argv[0]==str(fixture.BINARY):
            path=Path(argv[2]);config=json.loads(path.read_text())
            instance=outer_instance.Instance(config['directory'],config['service_id']).__enter__()
            held.append(instance)
            config.update(version=5,usage_handoff=False,version_recovery=None,
                          cold_admission=None,outer_instance_fd=instance.fd)
            path.write_text(json.dumps(config))
            options['pass_fds']=(*options['pass_fds'],instance.fd)
        return original(argv,**options)
    with mock.patch.object(subprocess,'Popen',side_effect=spawn):
        service=fixture.Service(output,models=True,gateway=True)
    value=service.wait(lambda row:row['business_ready'])
    (output/'ready.json').write_text(json.dumps(dict(status=value,directory=str(service.directory))))
    end=time.monotonic()+10
    while time.monotonic()<end and not (output/'exit').exists():time.sleep(.01)
    os._exit(0)


class ParentTests(unittest.TestCase):
    def setUp(self):
        base=Path(os.environ['Q4T_TEST_OUTPUT_DIR']);base.mkdir(parents=True,exist_ok=True)
        self.output=Path(tempfile.mkdtemp(prefix='outer-parent-',dir=base))
        self.tree=outer_tree.Tree().__enter__()
        self.tree.launch([sys.executable,'-I','-B',__file__,'--launcher',str(self.output)],
                         env=dict(os.environ))
        self.process=self.tree.root
        self.value=None;self.reaped=False;self.addCleanup(self.cleanup)
        until(lambda:(self.output/'ready.json').exists())
        self.value=json.loads((self.output/'ready.json').read_text())
        self.sid=self.value['status']['service_id'];self.pid=self.value['status']['guardian_pid']

    def cleanup(self):
        try:
            (self.output/'exit').touch()
            self.process.wait(timeout=3)
            if self.value is not None and not self.reaped:
                try:control.request(self.sid,'stop',timeout=4)
                except OSError:pass
                self.wait_guardian()
        finally:
            self.tree.drain(term_seconds=.2,kill_seconds=1)
            self.tree.close()

    def wait_guardian(self):
        result=until(lambda:(value if (value:=os.waitpid(self.pid,os.WNOHANG))[0] else None))
        self.reaped=True;self.assertEqual(os.waitstatus_to_exitcode(result[1]),0)

    def leave_parent(self):
        (self.output/'exit').touch();self.assertEqual(self.process.wait(timeout=3),0)

    def core(self):return json.loads((Path(self.value['directory'])/store.STATE).read_text())['state']

    def test_outer_death_preserves_same_healthy_core_and_original_running_intent(self):
        before=self.value['status'];core=self.core();self.leave_parent()
        def lost():
            value=control.request(self.sid,'status')
            return value if value['outer_owner_lost'] else None
        after=until(lost)
        self.assertEqual(after['state'],'READY_DEGRADED')
        self.assertTrue(after['business_ready']);self.assertFalse(after['stop_requested'])
        for key in ('guardian_pid','controller_pid','gateway_pid','model_pid','model_generation'):
            self.assertEqual(before[key],after[key])
        current=self.core();self.assertEqual(current['desired'],'RUNNING')
        for key in ('generations','attempts','quarantine','failures'):
            self.assertEqual(current[key],core[key])
        self.assertTrue(control.request(self.sid,'stop',timeout=4)['cleanup_confirmed'])
        self.wait_guardian();self.assertEqual(self.core()['desired'],'STOPPED')

    def test_signal_while_parent_is_alive_cannot_invent_owner_loss(self):
        before=self.value['status']
        fixture.signal_identity(self.pid,before['guardian_start_ticks'],signal.SIGUSR1)
        until(lambda:control.request(self.sid,'status')['controller_events']>before['controller_events'])
        after=control.request(self.sid,'status')
        self.assertTrue(after['business_ready']);self.assertFalse(after['outer_owner_lost'])
        self.assertEqual(after['model_pid'],before['model_pid'])

    def test_explicit_stop_before_parent_exit_remains_stopped(self):
        self.assertTrue(control.request(self.sid,'stop',timeout=4)['cleanup_confirmed'])
        self.leave_parent();self.wait_guardian()
        value=self.core();self.assertEqual(value['desired'],'STOPPED')
        self.assertFalse(any(value['active'].values()))


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--launcher':launcher(Path(sys.argv[2]))
    else:unittest.main(module=None)
