"""CLI retention transactions with real CPU journals and synthetic packages."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import maintenance, service
from tools.serve_recovery import test_publication_archive as copies
from tools.serve_recovery import test_publication_intent_archive as intents
from tools.serve_recovery import test_version_archive as versions
from tools.serve_recovery import test_native_bundle


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        output = os.environ.get('Q4T_TEST_OUTPUT_DIR', str(
            test_native_bundle.ROOT/'.q4t-work/maintenance-cpu'))
        self.env = mock.patch.dict(os.environ, Q4T_TEST_OUTPUT_DIR=output)
        self.env.start()
        self.addCleanup(self.env.stop)

    def fixture(self, kind='generations'):
        cls = {'generations': copies.ArchiveTests, 'intents': intents.IntentArchiveTests,
               'versions': versions.ArchiveTests}[kind]
        f = cls()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.f, self.kind = f, kind
        self.state, self.output, self.source = f.state, f.output, f.source
        self.desc = maintenance.generations.deployment.read_descriptor(self.state)
        self.selected = f.hash if kind == 'generations' else f.retired
        return f

    def call(self, operation, *options, success=True, state=None,
             package=None, digest=None):
        out, err = io.StringIO(), io.StringIO()
        args = [operation, '--state', str(state or self.state),
            '--package', str(package or self.desc['package']),
            '--manifest-sha256', digest or self.desc['manifest_sha256'], *map(str, options)]
        with redirect_stdout(out), redirect_stderr(err):
            code = service.main(args)
        self.assertEqual(code, 0 if success else 1, out.getvalue()+err.getvalue())
        return json.loads(out.getvalue() if success else err.getvalue())

    def plan(self, **kwargs):
        return self.call('maintenance-plan', '--kind', self.kind, '--select',
                         self.selected, '--output', self.output, **kwargs)

    def action(self, plan, *, restore=False, kind=None, **kwargs):
        return self.call('maintenance-restore' if restore else 'maintenance-apply',
            '--kind', kind or self.kind, '--plan', plan['plan'],
            '--plan-sha256', plan['sha256'], **kwargs)

    def state_bytes(self):
        return {str(p.relative_to(self.state)): p.read_bytes()
                for p in self.state.rglob('*.json')}

    def round_trip(self, kind):
        self.fixture(kind)
        before = self.state_bytes()
        status = self.call('maintenance-status')
        self.assertFalse(status['model_actions'])
        self.assertEqual(status['deleted_files'], 0)
        self.assertEqual(status['pools'][kind]['capacity'],
                         {'intents': 128, 'generations': 32, 'versions': 16}[kind])
        self.assertIn(self.selected, status['pools'][kind]['candidate_ids'])
        self.assertEqual(self.state_bytes(), before)
        plan = self.plan()
        self.assertTrue(self.source.exists())
        self.assertFalse(plan['files_moved'])
        with mock.patch.object(service, 'launch', side_effect=AssertionError('no model')):
            retired = self.action(plan)
            self.assertFalse(self.source.exists())
            self.assertEqual(retired['status'], 'RETIRED')
            self.assertEqual(self.action(plan), retired)
            current = self.call('maintenance-status')
            self.assertEqual(current['pools'][kind]['available'],
                             status['pools'][kind]['available']+1)
            restored = self.action(plan, restore=True)
            self.assertEqual(restored['status'], 'RESTORED')
            self.assertEqual(self.action(plan, restore=True), restored)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.call('maintenance-status'), status)

    def test_cli_intent_status_plan_archive_restore_preserves_state(self):
        self.round_trip('intents')

    def test_cli_generation_status_plan_archive_restore_preserves_state(self):
        self.round_trip('generations')

    def test_cli_version_status_plan_archive_restore_preserves_consumed_budget(self):
        self.round_trip('versions')

    def test_wrong_package_or_digest_rejects_before_planning(self):
        self.fixture()
        with mock.patch.object(maintenance.generations, 'locked') as lock:
            self.plan(package=self.f.package, success=False)
            self.plan(digest='f'*64, success=False)
        lock.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertTrue(self.source.exists())

    def test_plan_for_another_state_or_kind_cannot_move_files(self):
        self.fixture()
        plan = self.plan()
        value = maintenance.native.private_json(plan['plan'])
        value['state'] = str(self.state.parent/'other-state')
        maintenance.engine.save(Path(plan['plan']), value)
        changed = dict(plan, sha256=maintenance.engine.sha(plan['plan']))
        self.action(changed, success=False)
        self.action(changed, kind='intents', success=False)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.output/self.selected).exists())

    def test_changed_plan_hash_rejected_by_public_command(self):
        self.fixture()
        plan = dict(self.plan(), sha256='f'*64)
        self.action(plan, success=False)
        self.assertTrue(self.source.exists())

    def test_selected_deployment_changed_between_capture_and_lock_is_rejected(self):
        f = self.fixture()
        locked = maintenance.generations.locked

        @contextmanager
        def replace(*args, **kwargs):
            # A real publication wins the race before maintenance takes locks.
            copies.fixture.p.execute(self.state, request_id=str(uuid.uuid4()),
                root=f.root, package=f.package, digest=f.f.digest, release=f.release,
                expected=f.f.digest, revision=f.current['revision'])
            with locked(*args, **kwargs) as result:
                yield result

        with mock.patch.object(maintenance.generations, 'locked', replace):
            self.plan(success=False)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.source.exists())

    def test_query_and_apply_respect_actual_gpu_and_publication_locks(self):
        f = self.fixture()
        plan = self.plan()
        before = self.state_bytes()
        for path in (f.f.lease, self.state/'publication.lock'):
            with path.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.call('maintenance-status', success=False)
                self.action(plan, success=False)
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(self.source.exists())

    def test_partial_intent_and_unfinished_registration_are_reported_as_failure(self):
        self.fixture('versions')
        path = self.source.with_name(self.source.name+'.new')
        path.write_bytes(self.source.read_bytes())
        path.chmod(0o600)
        self.call('maintenance-status', success=False)
        self.plan(success=False)
        path.unlink()
        directory = self.state/maintenance.intents.publication.INTENTS
        path = directory/(str(uuid.uuid4())+'.json.new')
        path.write_bytes(b'{')
        self.call('maintenance-status', success=False)
        self.plan(success=False)
        self.assertTrue(self.source.exists())

    def test_status_candidates_still_require_reference_and_protection_checks(self):
        self.fixture()
        reference = self.state/'still-needed.json'
        maintenance.engine.save(reference, dict(path=str(self.source)))
        status = self.call('maintenance-status')
        row = status['pools']['generations']
        self.assertIn(self.selected, row['candidate_ids'])
        self.assertTrue(row['candidates_require_plan'])
        self.plan(success=False)
        reference.unlink()
        self.selected = row['protected_ids'][0]
        self.plan(success=False)
        self.assertFalse(self.output.exists())

    def test_source_bound_old_plan_is_not_resigned_or_silently_upgraded(self):
        self.fixture()
        plan = self.plan()
        value = maintenance.native.private_json(plan['plan'])
        value['source_sha256'] = 'f'*64
        maintenance.engine.save(Path(plan['plan']), value)
        self.action(dict(plan, sha256=maintenance.engine.sha(plan['plan'])), success=False)
        self.assertTrue(self.source.exists())

    def test_relocated_v20_imports_and_exposes_all_public_commands(self):
        self.fixture()
        source = self.state.parent/'relocated-source'
        shutil.copytree(Path(self.desc['package'])/'source', source)
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from tools.serve_recovery import bundle, service, maintenance
from tools.serve_recovery import publication_archive, publication_intent_archive, version_archive
from pathlib import Path
assert Path(maintenance.__file__).resolve().is_relative_to(Path(sys.argv[1]))
assert set(maintenance.KINDS) == {'intents', 'generations', 'versions'}
service.main([sys.argv[2], '--help'])
'''
        for command in ('maintenance-status', 'maintenance-plan',
                        'maintenance-apply', 'maintenance-restore'):
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                str(source), command], cwd=source, env={}, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(command.encode(), result.stdout)
            self.assertIn(b'--manifest-sha256', result.stdout)


if __name__ == '__main__':
    unittest.main()
