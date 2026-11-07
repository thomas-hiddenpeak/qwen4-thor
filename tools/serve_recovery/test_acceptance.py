"""CPU bounds of the GPU acceptance driver; never invokes a GPU/service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery.acceptance import Budget,LIMITS


class BudgetTests(unittest.TestCase):
    def setUp(self):
        base=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',ROOT/'build/recovery-acceptance'))
        base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.directory=Path(self.temp.name)
        self.budget=Budget(self.directory,initialize=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_model_and_self_check_share_persistent_nonrefundable_bounds(self):
        for kind in ('starts','chats','probes'):
            for _ in range(LIMITS[kind]): self.budget.reserve(kind,'before dispatch')
            with self.assertRaises(RuntimeError): Budget(self.directory).reserve(kind,'replacement actor')
        value=json.loads((self.directory/'counts.json').read_text())
        self.assertEqual(value['counts']['starts'],4)
        self.assertEqual(value['counts']['chats'],20)
        self.assertEqual(value['counts']['probes'],1)

    def test_parallel_actors_cannot_overwrite_chat_reservations(self):
        code=('import sys; from tools.serve_recovery.acceptance import Budget; '
              'b=Budget(sys.argv[1]); [b.reserve("chats",sys.argv[2]) for _ in range(10)]')
        children=[subprocess.Popen([sys.executable,'-B','-c',code,str(self.directory),label],cwd=ROOT,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                  for label in ('controller','client')]
        for child in children:
            out,err=child.communicate(timeout=5)
            self.assertEqual(child.returncode,0,(out,err))
        value=json.loads((self.directory/'counts.json').read_text())
        self.assertEqual(value['counts']['chats'],20)
        self.assertEqual(sorted(e['ordinal'] for e in value['events']),list(range(1,21)))

    def test_uninitialized_and_unknown_budget_cannot_create_new_allowance(self):
        with self.assertRaises(FileNotFoundError): Budget(self.directory/'absent').reserve('chats','no reset')
        with self.assertRaises(ValueError): self.budget.reserve('unlimited','invalid')
        with self.assertRaises(FileExistsError): Budget(self.directory,initialize=True)


if __name__=='__main__': unittest.main()
