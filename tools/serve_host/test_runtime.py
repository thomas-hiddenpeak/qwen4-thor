"""Real owned child cleanup without nginx, HTTP, model or GPU activity."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_host import observer as o,cpu


class Runtime(unittest.TestCase):
    def setUp(self):
        parent=cpu.BASE/'runtime-fixtures';parent.mkdir(exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=parent));self.root.chmod(0o700)
        child=self.root/'source/tools/serve_host/child.py';child.parent.mkdir(parents=True)
        child.write_text('import time\nprint("child-started",flush=True)\nwhile True: time.sleep(.01)\n')
        self.policy=dict(python=dict(path=sys.executable),port=19448,files=dict(nginx='unused'))

    def invoke(self,listener):
        with patch.object(o,'verify',return_value=self.policy),patch.object(o,'evaluate'), \
                patch.object(o.m,'listener',side_effect=listener):
            return o.watch(self.root,'fixture')

    def check_failed_cleanup(self):
        terminal=o.pkg.read(self.root/'observer-terminal.json')
        self.assertEqual(terminal['state'],'FAILED');self.assertTrue(terminal['cleanup_confirmed'])
        self.assertFalse(terminal['model_signalled']);self.assertFalse(terminal['automatic_reactivation'])
        with o.Directory(self.root) as directory,directory.claim('observer.lock',0):pass

    def test_identity_failure_after_spawn_still_reaps_child(self):
        original=o.sup.process_identity;children=[]
        def identity(pid):
            value=original(pid)
            if pid!=os.getpid():children.append(value);raise OSError('injected proc observation failure')
            return value
        with patch.object(o.sup,'process_identity',side_effect=identity):
            self.assertEqual(self.invoke(lambda *_:{}),1)
        self.check_failed_cleanup();self.assertEqual(len(children),1)
        self.assertFalse(o.sup.processes.group_members(children[0]['pid']))

    def test_log_failure_still_stops_owned_child(self):
        with patch.object(o.sup.RotatingLog,'write',side_effect=OSError('injected full log')):
            self.assertEqual(self.invoke(lambda *_:{}),1)
        self.check_failed_cleanup()

    def test_normal_stop_does_not_signal_backend_and_has_terminal_under_claim(self):
        def listener(*_):os.kill(os.getpid(),signal.SIGTERM);return {}
        self.assertEqual(self.invoke(listener),0)
        terminal=o.pkg.read(self.root/'observer-terminal.json')
        self.assertEqual(terminal['state'],'STOPPED');self.assertTrue(terminal['cleanup_confirmed'])
        self.assertFalse(terminal['model_signalled'])

    def test_contender_does_not_replace_active_owner_terminal(self):
        with o.Directory(self.root) as directory,directory.claim('observer.lock',0):
            directory.write('observer.json',b'{"active":"unchanged"}\n')
            directory.write('observer-terminal.json',b'{"previous":"unchanged"}\n')
            with patch.object(o.subprocess,'Popen') as child:self.assertEqual(self.invoke(lambda *_:{}),1)
            child.assert_not_called()
            self.assertEqual(directory.read('observer.json'),b'{"active":"unchanged"}\n')
            self.assertEqual(directory.read('observer-terminal.json'),b'{"previous":"unchanged"}\n')


if __name__=='__main__':unittest.main()
