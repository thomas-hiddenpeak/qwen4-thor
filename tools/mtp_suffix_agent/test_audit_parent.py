"""Bounded CPU checks against a completed, immutable 90f HTTP service."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest

import audit_parent
import run_stage_compat


class ParentCompatibilityTest(unittest.TestCase):
    source = None
    scratch = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.scratch)
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'evidence'
        audit_parent.copy_evidence(self.source, self.output)
        step = self.read('step.json')
        self.count = step['http_requests']
        # The old completed stage predates the layout field. Its real command
        # and capacity log still independently identify max-seq=1.
        step.setdefault('layout', 'single')
        self.write('step.json', step)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def write(self, name, value):
        (self.output / name).write_text(json.dumps(value) + '\n')

    def reject(self):
        with self.assertRaises(RuntimeError):
            audit_parent.audit(self.output, self.count)

    def test_completed_parent_without_new_ledger(self):
        result = audit_parent.audit(self.output, self.count)
        self.assertTrue(result['passed'])
        self.assertFalse(result['independent_batch_work_audit'])
        self.assertEqual(result['emitted_batch_ledger'],
                         'UNAVAILABLE_IN_DELIVERED_PARENT')

    def test_stage_adapter_preserves_unavailable_marker(self):
        run_stage_compat.audit_server(self.output, self.count)
        result = self.read('agent-audit.json')
        self.assertTrue(result['passed'])
        self.assertFalse(result['independent_batch_work_audit'])

    def test_stage_adapter_keeps_candidate_batch_requirement(self):
        binary = Path(self.temp.name) / 'different-program'
        binary.write_bytes(b'not-the-delivered-parent')
        step = self.read('step.json')
        step['binary'] = str(binary)
        self.write('step.json', step)
        with self.assertRaisesRegex(RuntimeError, 'verify/acceptance work ledger'):
            run_stage_compat.audit_server(self.output, self.count)

    def test_wrong_program_identity(self):
        binary = Path(self.temp.name) / 'wrong-program'
        binary.write_bytes(b'not-the-delivered-parent')
        step = self.read('step.json')
        step['binary'] = str(binary)
        self.write('step.json', step)
        command = self.read('server-command.json')
        command['argv'][0] = str(binary)
        self.write('server-command.json', command)
        self.reject()

    def test_agent_layout_cannot_use_exception(self):
        step = self.read('step.json')
        step['layout'] = 'agent'
        self.write('step.json', step)
        self.reject()

    def test_multiple_slots_cannot_use_exception(self):
        command = self.read('server-command.json')
        command['argv'][command['argv'].index('--max-seq') + 1] = '4'
        self.write('server-command.json', command)
        self.reject()

    def test_missing_acceptance_record(self):
        path = self.output / 'server.log'
        lines = path.read_text().splitlines()
        index = next(i for i, line in enumerate(lines)
                     if line.startswith('[mtp-accept] '))
        del lines[index]
        path.write_text('\n'.join(lines) + '\n')
        self.reject()

    def test_unclean_exit(self):
        result = self.read('exit.json')
        result['server'] = 1
        self.write('exit.json', result)
        self.reject()

    def test_missing_end_record(self):
        path = self.output / 'server.log'
        lines = path.read_text().splitlines()
        index = next(i for i, line in enumerate(lines) if 'event=end ' in line)
        del lines[index]
        path.write_text('\n'.join(lines) + '\n')
        self.reject()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=False)
    ParentCompatibilityTest.source = args.source
    ParentCompatibilityTest.scratch = args.scratch
    unittest.main(argv=['test_audit_parent'], verbosity=2)
