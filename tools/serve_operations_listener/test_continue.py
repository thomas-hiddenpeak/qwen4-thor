"""Continuation scope checks; no listeners or model."""
import unittest
from unittest.mock import patch
from tools.serve_operations_listener import continue_stage as c

class Continuation(unittest.TestCase):
    def test_absolute_ancestor_and_lease_path_match_deployment_guard(self):
        args=c.guard_command()
        self.assertIn(str(c.ROOT/'tools/evalscope/gpu_exclusive.py'),args)
        self.assertEqual(args[args.index('--output')+1],str(c.OUTPUT/'lease'))
        self.assertIn(str(c.ROOT/c.SOURCES[0]),args)
        self.assertNotEqual(c.ORIGINAL,c.OUTPUT)

    def test_only_zero_work_command_failure_can_continue(self):
        c.original_gate()
        for status in ('COMPLETED','ENV_ABORT','LEASE_BUSY'):
            with patch.object(c.cpu,'read',return_value=dict(status=status,child_returncode=1)),self.assertRaises(RuntimeError):
                c.original_gate()
        with patch.object(c.cpu,'read',return_value=dict(status='COMMAND_FAILED',child_returncode=0)),self.assertRaises(RuntimeError):
            c.original_gate()

if __name__=='__main__':unittest.main()
