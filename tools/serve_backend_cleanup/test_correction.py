"""Read-only correction custody and negative controls; no CUDA/HTTP execution."""
import unittest
from unittest.mock import patch

from tools.serve_backend_cleanup import correction as c


class Correction(unittest.TestCase):
    def test_original_passes_reused_without_reclassifying_failed_stage(self):
        audit = c.audit_original()
        self.assertEqual(audit['reused_passed_probes'], 16)
        self.assertTrue(audit['original_stage_remains_failed'])
        self.assertEqual(audit['old_generation_attempts'], 0)
        self.assertEqual(c.CUMULATIVE_PROBES, audit['old_probes']+c.LIMITS['probes'])

    def test_unknown_original_failure_is_not_the_approved_correction(self):
        read = c.h.read
        target = c.ORIGINAL_OUTPUT/'native-cleanup/unhanded-success/exit.json'
        def changed(path):
            return {'returncode': -9} if path == target else read(path)
        with patch.object(c.h, 'read', side_effect=changed):
            with self.assertRaises(RuntimeError): c.audit_original()

    def test_environment_abort_cannot_be_reused_as_valid_proof(self):
        read = c.h.read
        target = c.ORIGINAL_OUTPUT/'lease/exit.json'
        def changed(path):
            return dict(read(path), status='ENV_ABORT') if path == target else read(path)
        with patch.object(c.h, 'read', side_effect=changed):
            with self.assertRaises(RuntimeError): c.audit_original()

    def test_runtime_is_identical_and_only_one_old_tool_source_changes(self):
        c.a.verify_hashes(c.ROOT, c.h.read(c.original.PACKAGE/'candidate/runtime-source.json'))
        plan = c.h.read(c.original.PACKAGE/'plan.json')
        for key in ('binary', 'validation_binary', 'lifecycle_probe', 'mtp_lifecycle_probe'):
            c.h.check_identity(plan[key])
        sources = c.h.read(c.original.PACKAGE/'sources.json')
        changed = [p for p, digest in sources.items() if c.h.sha(c.ROOT/p) != digest]
        self.assertEqual(changed, [c.PROBE_SOURCE])


if __name__ == '__main__': unittest.main()
