"""Lease bridge and acceptance cleanup checks without network or GPU work."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tools.serve_supervisor_deadlines import stage as s
from tools.serve_supervisor_deadlines.borrowed_controller import borrowed_run


class Driver(unittest.TestCase):
    def test_bridge_preserves_guard_options_and_restores_binding(self):
        original=Mock(return_value=19)
        guard=SimpleNamespace(run_guard=original,common_lock=lambda:'lock',
            borrowed_lock=Mock(return_value=nullcontext()))
        args=SimpleNamespace(lease_fd=17,package='package',digest='digest',output='out')
        def run(package,digest,output):
            self.assertEqual((package,digest,output),('package','digest','out'))
            return guard.run_guard(['sealed-worker'],'out/lease','lock',
                                   log_max_bytes=4096,inherit_lease=True)
        service=SimpleNamespace(guard=guard,run=run)
        self.assertEqual(borrowed_run(service,args),19)
        guard.borrowed_lock.assert_called_once_with(17,'lock')
        original.assert_called_once_with(['sealed-worker'],'out/lease','lock',
            lease_fd=17,log_max_bytes=4096,inherit_lease=True)
        self.assertIs(guard.run_guard,original)
        service.run=lambda *_:guard.run_guard([],None,None,lease_fd=18)
        with self.assertRaisesRegex(RuntimeError,'nested lease'):
            borrowed_run(service,args)
        self.assertIs(guard.run_guard,original)

    def test_bridge_rejects_unowned_lease_before_service(self):
        service=SimpleNamespace(guard=SimpleNamespace(common_lock=lambda:'lock',
            borrowed_lock=Mock(side_effect=ValueError('unowned'))),run=Mock())
        with self.assertRaisesRegex(ValueError,'unowned'):
            borrowed_run(service,SimpleNamespace(lease_fd=17))
        service.run.assert_not_called()

    def test_startup_requires_observed_unready_attempt_and_real_connection(self):
        state=dict(attempts=1,restarts=0,state='STARTING')
        self.assertTrue(s.startup_snapshot(state,'loading',['owned']))
        for changes in ({'attempts':0},{'restarts':1},{'state':'READY'},
                        {'state':'DEGRADED'},{'state':'FAILED'}):
            self.assertFalse(s.startup_snapshot(dict(state,**changes),'loading',['owned']))
        self.assertFalse(s.startup_snapshot(state,'loading',[]))
        self.assertFalse(s.startup_snapshot(state,'[q4t] serving on port 18132',['owned']))

    def test_tcp_evidence_requires_worker_fd_established_and_matching_peer(self):
        target=f'0100007F:{s.base.OPS_PORT:04X}'
        rows=['sl local remote state other',
              f'0: 0100007F:1111 {target} 01 0 0 0 0 0 100',
              f'1: 0100007F:1112 {target} 02 0 0 0 0 0 100',
              f'2: 0100007F:1113 {target} 01 0 0 0 0 0 200',
              '3: 0100007F:1114 0100007F:FFFF 01 0 0 0 0 0 100']
        with patch.object(Path,'iterdir',return_value=[Path('/proc/55/fd/9')]), \
                patch.object(Path,'read_text',return_value='\n'.join(rows)), \
                patch.object(s.os,'readlink',return_value='socket:[100]'):
            actual=s.established_probe(55,s.base.OPS_PORT)
        self.assertEqual(actual,[dict(local='0100007F:1111',remote=target,state='01',inode='100')])

    def test_helper_registers_child_and_only_explicitly_passes_owned_fd(self):
        with tempfile.TemporaryDirectory(dir=s.BASE) as directory:
            run=object.__new__(s.Run)
            run.logs=[];run.processes={};run.custody=SimpleNamespace(fd=17)
            child=SimpleNamespace(pid=999999999)
            try:
                with patch.object(s,'OUT',Path(directory)), \
                        patch.object(s.subprocess,'Popen',return_value=child) as popen, \
                        patch.object(s.sup,'process_identity',side_effect=ProcessLookupError):
                    for name,lease in (('with-lease',True),('without-lease',False)):
                        with self.assertRaises(ProcessLookupError):
                            run.helper(name,['not-executed'],lease=lease)
                        self.assertIs(run.processes[name],child)
                        self.assertEqual(popen.call_args.kwargs['pass_fds'],(17,) if lease else ())
                        self.assertTrue(popen.call_args.kwargs['start_new_session'])
            finally:
                for stream in run.logs:stream.close()

    def test_helper_failure_does_not_skip_remaining_or_base_cleanup(self):
        run=object.__new__(s.Run)
        broken=Mock(pid=999999998);broken.poll.return_value=None
        broken.terminate.side_effect=RuntimeError('helper failure')
        later=Mock(pid=999999999);later.poll.return_value=None
        run.processes={'startup-controller':broken,'transport-probes':later}
        with patch.object(s.service.guard,'processes',return_value={}), \
                patch.object(s.service.guard,'owned_processes'), \
                patch.object(s.service.guard,'stop_owned') as fallback, \
                patch.object(s.base.Run,'cleanup') as base_cleanup, \
                patch.object(Path,'exists',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'helper cleanup failure'):
                run.cleanup()
        fallback.assert_called_once_with(broken,2,{})
        later.terminate.assert_called_once()
        later.wait.assert_called_once_with(3)
        base_cleanup.assert_called_once()


if __name__=='__main__':unittest.main()
