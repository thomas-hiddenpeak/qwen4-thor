"""Actual isolated CPU trees; no GPU or foreign-process signals."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import outer_tree


def until(predicate):
    end = time.monotonic()+3
    while not predicate():
        if time.monotonic() >= end: raise TimeoutError('CPU fixture readiness')
        time.sleep(.01)


def scenario(name, directory):
    if name == 'existing':
        existing = subprocess.Popen(['/bin/sleep','10'])
        try:
            try:
                with outer_tree.Tree(): raise AssertionError('adopted existing child')
            except ValueError: pass
            assert existing.poll() is None
        finally: existing.terminate(); existing.wait(timeout=2)
        return dict(existing_child_preserved=True)
    if name == 'orphan':
        code = '''import os,signal,sys,time
from pathlib import Path
child=os.fork()
if child == 0:
 os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN)
 Path(sys.argv[1]).write_text(str(os.getpid()))
 time.sleep(10);os._exit(0)
while not Path(sys.argv[1]).exists():time.sleep(.005)
os._exit(17)
'''
    elif name == 'normal': code = 'import sys;sys.exit(19)'
    else: code = 'import time;time.sleep(10)'
    with outer_tree.Tree() as tree:
        owner = tree.launch([sys.executable,'-I','-B','-c',code,str(directory/'orphan')],env={})
        if name in ('orphan','normal'): until(lambda:tree.poll() is not None)
        if name == 'orphan':
            try: tree.witness(owner); raise AssertionError('live orphan reported empty')
            except ValueError: pass
        if name == 'timeout':
            with mock.patch.object(tree,'signal_children'):
                try: tree.drain(term_seconds=0,kill_seconds=.03);raise AssertionError('fake timeout success')
                except TimeoutError: pass
            assert tree.entered and tree.root.poll() is None
            try: tree.witness(owner);raise AssertionError('timeout granted cleanup')
            except ValueError: pass
        if name == 'close':
            try: tree.close();raise AssertionError('abandoned live children')
            except ValueError: pass
            assert tree.entered and tree.root.poll() is None
        result = tree.drain(term_seconds=.05,kill_seconds=1)
        try: tree.witness(dict(owner,pid=owner['pid']+1));raise AssertionError('foreign root accepted')
        except ValueError: pass
        if name == 'orphan':
            orphan = int((directory/'orphan').read_text())
            assert any(row['pid']==orphan for row in result['reaped'])
            assert not Path('/proc/'+str(orphan)).exists()
            assert result['root_exit_code']==17
        if name == 'normal': assert result['root_exit_code']==19
    assert not tree.entered and outer_tree.no_children()
    return result


class TreeTests(unittest.TestCase):
    def test_disappearing_foreign_records_never_produce_signal_candidates(self):
        actual = (Path('/proc')/str(os.getpid())/'stat').read_bytes()
        self.assertIsNotNone(outer_tree.direct_candidate(actual, os.getppid()))
        for raw in (b'', b'123 (short) S', b'x'*4097, b'123 (bad) S no_parent'):
            self.assertIsNone(outer_tree.direct_candidate(raw, os.getppid()))
        self.assertIsNone(outer_tree.direct_candidate(actual, os.getpid()))

    def run_case(self, name):
        base = ROOT/'.q4t-work/serve-outer-recovery-20261112/tree-fixtures'
        base.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            # This independent sibling is not a child of the custody worker.
            foreign = subprocess.Popen(['/bin/sleep','10'])
            try:
                result = subprocess.run([sys.executable,'-I','-B',__file__,name,temporary],
                    capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIsNone(foreign.poll())
                return json.loads(result.stdout)
            finally: foreign.terminate();foreign.wait(timeout=2)

    def test_normal_exit_keeps_exact_wait_status_and_root_identity(self):
        value = self.run_case('normal');self.assertTrue(value['kernel_echild'])
        self.assertEqual(value['root_exit_code'],19);self.assertFalse(value['device_authorized'])

    def test_setsid_orphan_is_adopted_reaped_and_not_confused_with_sibling(self):
        value = self.run_case('orphan');self.assertTrue(value['kernel_echild'])
        self.assertEqual(len(value['reaped']),1)

    def test_timeout_retains_custody_until_later_actual_cleanup(self):
        self.assertTrue(self.run_case('timeout')['kernel_echild'])

    def test_cannot_close_custody_or_claim_foreign_root_while_live(self):
        self.assertTrue(self.run_case('close')['kernel_echild'])

    def test_preexisting_children_are_never_taken_over(self):
        self.assertTrue(self.run_case('existing')['existing_child_preserved'])


if __name__ == '__main__':
    if len(sys.argv)==3 and sys.argv[1] in ('normal','orphan','timeout','close','existing'):
        print(json.dumps(scenario(sys.argv[1],Path(sys.argv[2]))))
    else:unittest.main()
