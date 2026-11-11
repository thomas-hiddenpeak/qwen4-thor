"""CPU run archive/retirement tests in private work fixtures; no model operations."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, controller, journal, ledger, retention as r, service

ROOT = Path(__file__).resolve().parents[2]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.q4t-work/serve-native-observation-20261109/retention-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / 'state'; self.state.mkdir(mode=0o700)
        (self.state / 'runs').mkdir(mode=0o700)
        self.service_id = str(uuid.uuid4())
        package = self.directory / 'package'; package.mkdir(mode=0o700)
        self.write(package / 'manifest.json', dict(repository=str(ROOT)))
        manifest_hash = hashlib.sha256((package / 'manifest.json').read_bytes()).hexdigest()
        self.write(self.state / 'service.json', dict(version=1, service_id=self.service_id,
            package=str(self.directory / 'package'), manifest_sha256=manifest_hash))
        self.write(self.state / 'ledger.json', dict(protected='unchanged sentinel'))
        self.ledger = (self.state / 'ledger.json').read_bytes()

    def write(self, path, value):
        raw = value if isinstance(value, bytes) else ledger.canonical(value)
        path.write_bytes(raw); path.chmod(0o600)

    def run_fixture(self):
        run_id = str(uuid.uuid4())
        path = self.state / 'runs' / run_id; path.mkdir(mode=0o700)
        self.write(path / 'controller.json', service.controller_configuration(
            self.directory / 'package', 'b' * 64, self.state, path))
        self.write(path / 'guardian.json', dict(directory=str(self.state), service_id=self.service_id,
            launch=dict(parent_pid=os.getpid(), parent_start_ticks=journal.process_start(os.getpid())),
            controller=['python', '--config', str(path / 'controller.json')],
            gateway=['python', '--config', str(path / 'gateway.json')]))
        self.write(path / 'gateway.json', dict(private_token='fixture-only'))
        (path / 'model-1').mkdir(mode=0o700)
        self.write(path / 'model-1/model.log', b'failure evidence\n' * 100)
        return run_id, path

    def archive(self, run_id):
        return r.archive(self.state, run_id, self.directory / ('archive-' + str(uuid.uuid4())), pins=set())

    def plan(self, **options):
        return r.plan(self.state, pins=set(), **options)

    def test_retention_provenance_is_advisory_and_old_configs_remain_accepted(self):
        cfg = service.controller_configuration('/package', 'a' * 64, '/state', '/run')
        self.assertEqual(cfg['version'], 3)
        controller.validate_configuration(cfg)
        context = cfg['retention_context']
        self.assertEqual(context['boot_id'], r.native.stamp()['boot_id'])
        with mock.patch.object(service.journal, 'process_start', side_effect=OSError('missing proc')):
            missing = service.controller_configuration('/package', 'a' * 64, '/state', '/run')
        self.assertIsNone(missing['retention_context'])
        controller.validate_configuration(missing)
        old = dict(cfg, version=2); old.pop('retention_context')
        controller.validate_configuration(old)
        old['version'] = 1; old.pop('state_directory')
        controller.validate_configuration(old)


    def test_unverified_epoch_is_protected_without_disabling_core_launch(self):
        run_id, path = self.run_fixture()
        cfg = json.loads((path / 'controller.json').read_text())
        cfg['retention_context']['boot_id'] = str(uuid.uuid4())
        self.write(path / 'controller.json', cfg)
        controller.validate_configuration(cfg)
        with self.assertRaisesRegex(ValueError, 'epoch'):
            self.archive(run_id)
        # No current-boot deletion eligibility is inferred from an old tick value.
        with mock.patch.object(r, 'references', return_value=set()):
            plan = self.plan(limits=r.policy(max_bytes=1))
        self.assertEqual(plan['protected'][0]['reason'], 'owner_epoch_unverified')
        self.assertEqual(plan['remove'], [])
        cfg['retention_context'] = None
        self.write(path / 'controller.json', cfg)
        controller.validate_configuration(cfg)
        with self.assertRaisesRegex(ValueError, 'epoch'):
            self.archive(run_id)

    def test_managed_v4_and_profile_v5_runs_archive_original_state_binding(self):
        binding = access_store.binding(self.state,self.service_id)
        for version in (4,5):
            with self.subTest(version=version):
                run_id,path = self.run_fixture()
                cfg = json.loads((path/'controller.json').read_text())
                cfg.update(version=version,limits=binding)
                if version == 5:cfg['profiles'] = binding
                controller.validate_configuration(cfg)
                self.write(path/'controller.json',cfg)
                receipt = self.archive(run_id)
                actual,manifest = r.verified_archive(self.state,run_id)
                self.assertEqual(actual,receipt)
                self.assertEqual(manifest['original'],r.snapshot(path))
                self.assertEqual((Path(receipt['archive'])/'files/controller.json').read_bytes(),
                                 (path/'controller.json').read_bytes())
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.ledger)

    def test_managed_run_cannot_archive_foreign_limits_or_profile_binding(self):
        binding = access_store.binding(self.state,self.service_id)
        run_id,path = self.run_fixture()
        base = json.loads((path/'controller.json').read_text())
        base.update(version=5,limits=binding,profiles=binding)
        for name in ('limits','profiles'):
            bad = dict(base,**{name:dict(binding,service_id=str(uuid.uuid4()))})
            self.write(path/'controller.json',bad)
            with self.assertRaisesRegex(ValueError,'managed state binding'):
                self.archive(run_id)
        self.assertEqual((self.state/'ledger.json').read_bytes(),self.ledger)



    def test_archive_roundtrip_is_private_and_required_for_every_run(self):
        run_id, path = self.run_fixture()
        plan = self.plan(limits=r.policy(max_bytes=1))
        self.assertEqual(plan['remove'], [])
        self.assertEqual(plan['protected'][0]['reason'], 'unarchived_or_archive_changed')
        receipt = self.archive(run_id)
        original = r.snapshot(path)
        verified, manifest = r.verified_archive(self.state, run_id)
        self.assertEqual(verified, receipt)
        self.assertEqual(manifest['original'], original)
        for p in Path(receipt['archive']).rglob('*'):
            self.assertEqual(p.stat().st_mode & 0o077, 0)
        self.assertEqual((Path(receipt['archive']) / 'files/model-1/model.log').read_bytes(),
                         (path / 'model-1/model.log').read_bytes())
        self.assertEqual((self.state / 'ledger.json').read_bytes(), self.ledger)
        with self.assertRaises(ValueError):
            r.archive(self.state, run_id, self.state / 'illegal', pins=set())

    def test_live_process_reference_is_protected_without_signalling_it(self):
        run_id, path = self.run_fixture()
        child = subprocess.Popen([sys.executable, '-B', '-c',
            'import sys,time; f=open(sys.argv[1],"rb"); print("ready",flush=True); time.sleep(20)',
            str(path / 'model-1/model.log')], stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'ready\n')
            self.assertIn(run_id, r.references(self.state / 'runs'))
            with self.assertRaisesRegex(ValueError, 'active'):
                self.archive(run_id)
            self.assertIsNone(child.poll())
            plan = self.plan(limits=r.policy(max_bytes=1))
            self.assertEqual(plan['protected'][0]['reason'], 'active')
        finally:
            child.terminate(); child.wait(timeout=3); child.stdout.close()
        self.archive(run_id)

    def test_release_dependencies_and_protected_overage_are_not_deleted(self):
        protected, path = self.run_fixture()
        archived, other = self.run_fixture()
        self.archive(archived)
        anchor = self.directory / 'release.json'
        self.write(anchor, dict(previous_binary=str(path / 'model-1/model.log')))
        pins = r.dependencies(self.state, anchors=[anchor])
        self.assertIn(path / 'model-1/model.log', pins)
        plan = r.plan(self.state, pins=pins, limits=r.policy(max_runs=1, max_bytes=1))
        self.assertEqual(plan['protected'][0]['reason'], 'release_dependency')
        self.assertGreater(plan['protected_excess_known_bytes'], 0)
        self.assertEqual([row['run_id'] for row in plan['remove']], [archived])
        with self.assertRaisesRegex(ValueError, 'dependency'):
            r.archive(self.state, protected, self.directory / 'refused', pins=pins)
        self.assertTrue(path.exists()); self.assertTrue(other.exists())

    def test_limits_include_actual_allocated_blocks_and_age(self):
        old, path = self.run_fixture()
        before = time.time() - 3 * 86400
        os.utime(path / 'controller.json', (before, before))
        self.archive(old)
        recent, recent_path = self.run_fixture(); self.archive(recent)
        plan = self.plan(limits=r.policy(max_age_days=1, max_runs=100))
        self.assertEqual([row['run_id'] for row in plan['remove']], [old])
        self.assertEqual(plan['retain'], [recent])
        self.assertEqual(plan['retained_known_allocated_bytes'], r.snapshot(recent_path)['allocated_bytes'])
        plan = self.plan(limits=r.policy(max_runs=1))
        self.assertEqual(plan['retain'], [recent])

    def test_changed_archive_source_or_pin_invalidates_old_plan_before_delete(self):
        first, path = self.run_fixture(); receipt = self.archive(first)
        second, other = self.run_fixture(); self.archive(second)
        plan = self.plan(limits=r.policy(max_bytes=1))
        with self.assertRaises(ValueError):
            r.apply(self.state, plan, pins={path / 'model-1/model.log'})
        copied = Path(receipt['archive']) / 'files/model-1/model.log'
        copied.write_bytes(b'changed archive')
        with self.assertRaises(ValueError):
            r.apply(self.state, plan, pins=set())
        self.assertTrue(path.exists()); self.assertTrue(other.exists())
        self.archive(first)
        plan = self.plan(limits=r.policy(max_bytes=1))
        self.write(other / 'new-evidence', b'late fault')
        with self.assertRaises(ValueError):
            r.apply(self.state, plan, pins=set())
        self.assertTrue(path.exists()); self.assertTrue(other.exists())

    def test_actual_retirement_and_interrupted_resume_keep_archive(self):
        run_id, path = self.run_fixture(); receipt = self.archive(run_id)
        plan = self.plan(limits=r.policy(max_bytes=1))
        unlink = os.unlink
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('targeted CPU interruption')
            return unlink(*args, **kwargs)
        with mock.patch.object(r.os, 'unlink', side_effect=interrupted):
            with self.assertRaises(OSError):
                r.apply(self.state, plan, pins=set())
        trash = self.state / 'runs' / ('.retired-' + run_id)
        self.assertFalse(path.exists()); self.assertTrue(trash.exists())
        result = r.resume(self.state, run_id, pins=set())
        self.assertEqual(result['state'], 'RETIRED')
        self.assertFalse(trash.exists())
        self.assertTrue((Path(receipt['archive']) / 'files/model-1/model.log').exists())
        self.assertTrue(r.resume(self.state, run_id, pins=set())['resumed'])
        self.assertEqual((self.state / 'ledger.json').read_bytes(), self.ledger)
        second, other = self.run_fixture(); self.archive(second)
        applied = r.apply(self.state, self.plan(limits=r.policy(max_bytes=1)), pins=set())
        self.assertFalse(other.exists()); self.assertFalse(applied['model_signalled'])

    def test_links_and_replaced_directories_cannot_escape_scope(self):
        run_id, path = self.run_fixture()
        victim = self.directory / 'victim'; self.write(victim, b'preserve')
        link = path / 'foreign'; link.symlink_to(victim)
        with self.assertRaises(ValueError):self.archive(run_id)
        link.unlink();os.link(victim, link)
        with self.assertRaises(ValueError):self.archive(run_id)
        link.unlink();self.archive(run_id)
        plan = self.plan(limits=r.policy(max_bytes=1))
        moved = self.directory / 'moved';path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):r.apply(self.state, plan, pins=set())
        self.assertEqual(victim.read_bytes(), b'preserve')
        self.assertTrue((moved / 'model-1/model.log').exists())
        with self.assertRaises(ValueError):r.verified_archive(self.state, '../../ledger')


if __name__ == '__main__':
    unittest.main()
