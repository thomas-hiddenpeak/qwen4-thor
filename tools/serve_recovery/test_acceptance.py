"""CPU bounds of the GPU acceptance driver; never invokes a GPU/service."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery.acceptance import (Budget, LIMITS, COMPLETION_LIMITS,
    OBSERVATION_LIMITS, OBSERVATION_COMPLETION_LIMITS, await_drain, limits_for)


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

    def test_continuation_cannot_reset_consumed_gpu_or_chat_allowance(self):
        path=self.directory/'continuation'
        path.mkdir()
        budget=Budget(path,initialize=True,baseline=dict(starts=1,chats=5,probes=0,controls=166))
        for _ in range(3): budget.reserve('starts','remaining model')
        for _ in range(15): budget.reserve('chats','remaining request')
        with self.assertRaises(RuntimeError): budget.reserve('starts','over total')
        with self.assertRaises(RuntimeError): budget.reserve('chats','over total')
        with self.assertRaises(ValueError):
            Budget(self.directory/'invalid',initialize=True,baseline=dict(starts=-1,chats=0,probes=0,controls=0))

    def test_prepared_completion_keeps_prior_counts_and_exact_remaining_bound(self):
        path=self.directory/'completion'
        path.mkdir()
        budget=Budget(path,initialize=True,limits=COMPLETION_LIMITS,
                      baseline=dict(starts=3,chats=15,probes=1,controls=571))
        for _ in range(2): budget.reserve('starts','completion model')
        for _ in range(6): budget.reserve('chats','completion request')
        for kind in ('starts','chats','probes'):
            with self.assertRaises(RuntimeError): budget.reserve(kind,'exhausted')

    def test_observation_scenario_cannot_restart_model_or_use_probe(self):
        path = self.directory/'observation'
        path.mkdir()
        budget = Budget(path, initialize=True, limits=limits_for('observation_isolation'))
        budget.reserve('starts', 'only model')
        for _ in range(4):
            budget.reserve('chats', 'self-check or client')
        for kind in ('starts', 'chats', 'probes'):
            with self.assertRaises(RuntimeError):
                budget.reserve(kind, 'beyond affected-behavior scope')
        self.assertEqual(OBSERVATION_LIMITS['seconds'], 600)
        with self.assertRaises(KeyError):
            limits_for('unreviewed_scenario')

    def test_observation_completion_keeps_failed_start_and_self_check_charged(self):
        path = self.directory/'observation-completion'
        path.mkdir()
        budget = Budget(path, initialize=True, limits=OBSERVATION_COMPLETION_LIMITS,
            baseline=dict(starts=1, chats=1, probes=0, controls=157))
        budget.reserve('starts', 'single completion')
        for _ in range(3):
            budget.reserve('chats', 'self-check and JSON/SSE')
        for kind in ('starts', 'chats', 'probes'):
            with self.assertRaises(RuntimeError):
                budget.reserve(kind, 'no reset')


class PackagedImportTests(unittest.TestCase):
    def setUp(self):
        from tools.serve_recovery.bundle import SOURCES
        output = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/recovery-import-tests'))
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name)
        for name in SOURCES:
            target = self.package/'source'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, target)

    def invoke(self, prefix=''):
        code = ('import sys,runpy; from pathlib import Path; '
            'root=Path(sys.argv[1]); sys.path.insert(0,str(root)); '+prefix+
            'driver=runpy.run_path(str(root/"tools/serve_recovery/acceptance.py")); '
            'package=Path(sys.argv[2]); '
            'mods=driver["load_modules"]({"package":{"package":str(package)}}); '
            'from tools.serve_recovery import controller,observation_io; '
            'assert all(Path(m.__file__).is_relative_to(package) '
            'for m in (*mods,controller,observation_io)); '
            'assert observation_io.ROOT==package/"source"; print("SEALED")')
        return subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(ROOT), str(self.package)], capture_output=True, text=True, timeout=5)

    def test_workspace_regular_package_cannot_shadow_sealed_namespace(self):
        self.assertFalse((self.package/'source/tools/serve_recovery/__init__.py').exists())
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'SEALED')

    def test_missing_sealed_dependency_cannot_fall_back_to_checkout(self):
        (self.package/'source/tools/serve_recovery/control.py').unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ImportError', result.stderr)

    def test_preloaded_checkout_module_fails_before_any_service_start(self):
        result = self.invoke('from tools.serve_recovery import control; ')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already loaded outside sealed source', result.stderr)


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.good=dict(q4t_requests_total=5,q4t_requests_success_total=5,
            q4t_seq_slots_free=1,q4t_seq_slots_total=1,
            q4t_structured_workers_started_total=3,q4t_structured_workers_reaped_total=3)

    def sleep(self,value): self.now+=value

    def wait(self,observe):
        return await_drain(observe,5,clock=lambda:self.now,sleep=self.sleep)

    def test_response_then_worker_reap_is_allowed_within_deadline(self):
        samples=[]
        def observe(_timeout):
            result=dict(self.good,q4t_structured_workers_reaped_total=2 if not samples else 3)
            samples.append(result)
            return result
        self.assertEqual(self.wait(observe),self.good)
        self.assertEqual(len(samples),2)

    def test_unreaped_worker_is_not_hidden_by_polling(self):
        pending=dict(self.good,q4t_structured_workers_reaped_total=2)
        with self.assertRaises(TimeoutError): self.wait(lambda _:pending)
        self.assertEqual(self.now,5)

    def test_replay_or_late_success_cannot_pass(self):
        with self.assertRaises(RuntimeError):
            self.wait(lambda _:dict(self.good,q4t_requests_total=6))
        def late(_timeout):
            self.now=6
            return self.good
        with self.assertRaises(TimeoutError): self.wait(late)


if __name__=='__main__': unittest.main()
