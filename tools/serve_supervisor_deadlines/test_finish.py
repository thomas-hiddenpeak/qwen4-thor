"""Reproduce driver PID/PGID mismatch and verify the single-root recheck."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tools.serve_supervisor_deadlines import finish as f


class Correction(unittest.TestCase):
    def test_pid_is_not_a_group_and_normal_controller_owns_detached_children(self):
        records={100:dict(pgid=99,ppid=1,start_ticks=10,argv=['python'],executable='python'),
            200:dict(pgid=200,ppid=100,start_ticks=20,argv=['python'],executable='python'),
            201:dict(pgid=201,ppid=200,start_ticks=21,argv=['python'],executable='python'),
            202:dict(pgid=202,ppid=201,start_ticks=22,argv=['q4t','serve'],executable='q4t')}
        guard=f.s.service.guard
        with patch.object(guard,'processes',return_value=records):
            wrong=guard.inspect(100,{},compute_query=lambda:{202})
            right=guard.inspect(200,{},compute_query=lambda:{202})
        self.assertEqual([r['pid'] for r in wrong['conflicts']],[202])
        self.assertEqual(right['conflicts'],[])

    def test_single_controller_root_does_not_adopt_foreign_or_reused_pid(self):
        records={200:dict(pgid=200,ppid=100,start_ticks=20,argv=['python'],executable='python'),
            202:dict(pgid=202,ppid=1,start_ticks=99,argv=['q4t','serve'],executable='q4t'),
            300:dict(pgid=300,ppid=1,start_ticks=30,argv=['q4t','serve'],executable='q4t')}
        history={202:dict(start_ticks=22)}
        with patch.object(f.s.service.guard,'processes',return_value=records):
            value=f.s.service.guard.inspect(200,history,compute_query=lambda:{202,300})
        self.assertEqual({r['pid'] for r in value['conflicts']},{202,300})

    def test_cleanup_failure_still_checks_gpu_and_finishes_custody_as_unconfirmed(self):
        run=object.__new__(f.Run);run.directory=Path('/not-created')
        process=Mock();process.poll.return_value=None
        process.terminate.side_effect=RuntimeError('stop failed')
        run.process=process;run.custody=Mock()
        with patch.object(Path,'exists',return_value=False), \
                patch.object(f.s.service.guard,'stop_owned') as drain, \
                patch.object(f.s.base.client.a,'require_idle') as gpu, \
                patch.object(f.s.base.inspection,'listeners',return_value=[]):
            with self.assertRaisesRegex(RuntimeError,'targeted cleanup'):
                run.cleanup()
        drain.assert_called_once_with(process,2)
        gpu.assert_called_once()
        run.custody.finish.assert_called_once_with(cleanup_confirmed=False)


if __name__=='__main__':unittest.main()
