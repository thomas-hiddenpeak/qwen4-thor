"""CPU contracts using real child lifetimes; no model or HTTP listener."""
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import package as pkg, service, supervisor as sup
from bounded_log import BoundedLog, open_log


class Case(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'build/managed-service-tests'
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def observation(self, pid=42, ready=True):
        return dict(process=dict(pid=pid, executable_sha256='binary',
                                 listener_scope='loopback'), protocol='protocol',
                    ready=ready, metrics={'q4t_gpu_healthy': 1},
                    configuration=sup.expected_configuration())

    def run_supervisor(self, observer, *, restarts=0, script=None, patch=None,
                       read_memory=None, config_overrides=None):
        out = self.root/'runtime'
        out.mkdir()
        cfg = pkg.settings(18999, restarts)
        cfg.update(poll_seconds=.01, startup_seconds=.1,
                   unavailable_seconds=.1, restart_delay_seconds=.15,
                   stop_seconds=.2, kill_seconds=.5, memory_poll_seconds=.01,
                   memory_low_seconds=.02, memory_unknown_seconds=.04)
        if config_overrides:
            cfg.update(config_overrides)
        read_memory = read_memory or (lambda: dict(
            total_bytes=8*1024**3, available_bytes=4*1024**3))
        manifest = dict(settings=cfg, repository=str(ROOT),
                        protocol_sha256='protocol', files={'q4t': 'binary'})
        command = [sys.executable, '-c', script or 'import time; time.sleep(30)']
        with mock.patch.object(pkg, 'verify', return_value=manifest), \
             mock.patch.object(pkg, 'command', return_value=command), \
             mock.patch.object(sup, 'backend_command', return_value=command):
            if patch:
                with patch:
                    rc = sup.run(self.root, 'digest', out, observe=observer,
                                 read_memory=read_memory)
            else:
                rc = sup.run(self.root, 'digest', out, observe=observer,
                             read_memory=read_memory)
        return rc, pkg.read(out/'status.json')

    def test_temporary_unready_keeps_pid(self):
        seen = []
        def observe(pid, _port):
            seen.append(pid)
            if len(seen) == 4:
                pkg.save(self.root/'runtime/stop.request.json', {})
            return self.observation(pid, ready=len(seen) in (1, 4))
        rc, state = self.run_supervisor(observe)
        self.assertEqual(rc, 0)
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual((state['attempts'], state['restarts']), (1, 0))
        self.assertTrue(state['cleanup_confirmed'])
        self.assertIn('DEGRADED', (self.root/'runtime/events.log').read_text())

    def test_signal_exit_restarts_after_group_cleanup(self):
        seen = []
        def observe(pid, _port):
            if pid not in seen:
                if seen:
                    self.assertFalse(sup.processes.group_members(seen[0]))
                    pkg.save(self.root/'runtime/stop.request.json', {})
                else:
                    os.kill(pid, signal.SIGKILL)
                seen.append(pid)
            return self.observation(pid)
        rc, state = self.run_supervisor(observe, restarts=1)
        self.assertEqual((rc, state['attempts'], state['restarts']), (0, 2, 1))
        self.assertEqual(len(seen), 2)

    def test_total_restart_budget_is_not_reset(self):
        def observe(pid, _port):
            os.kill(pid, signal.SIGKILL)
            return self.observation(pid)
        rc, state = self.run_supervisor(observe, restarts=2)
        self.assertEqual((rc, state['attempts'], state['restarts']), (1, 3, 2))
        self.assertTrue(state['cleanup_confirmed'])

    def test_default_never_restarts(self):
        def observe(pid, _port):
            os.kill(pid, signal.SIGKILL)
            return self.observation(pid)
        rc, state = self.run_supervisor(observe)
        self.assertEqual((rc, state['attempts']), (1, 1))

    def test_stop_during_backoff_blocks_reactivation(self):
        errors = []
        def stopper():
            try:
                until = time.monotonic()+5
                path = self.root/'runtime/status.json'
                while time.monotonic() < until:
                    if path.exists() and pkg.read(path)['state'] == 'RESTART_WAIT':
                        pkg.save(self.root/'runtime/stop.request.json', {})
                        return
                    time.sleep(.005)
                raise RuntimeError('missing restart wait')
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=stopper)
        thread.start()
        try:
            def observe(pid, _port):
                os.kill(pid, signal.SIGKILL)
                return self.observation(pid)
            rc, state = self.run_supervisor(observe, restarts=2)
        finally:
            thread.join(6)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertEqual((rc, state['attempts']), (0, 1))

    def test_gpu_failure_is_terminal_even_with_restart_budget(self):
        def observe(pid, _port):
            value = self.observation(pid)
            value['metrics']['q4t_gpu_healthy'] = 0
            return value
        rc, state = self.run_supervisor(observe, restarts=3)
        self.assertEqual((rc, state['attempts']), (1, 1))
        self.assertEqual(state['error'], 'GPU unhealthy')
        self.assertTrue(state['cleanup_confirmed'])

    def test_permanent_model_failure_stops_without_poisoning_gpu_or_restart(self):
        seen = []
        def observe(pid, _port):
            seen.append(pid)
            value = self.observation(pid, ready=len(seen) == 1)
            value['health_status'] = 'ready' if len(seen) == 1 else 'unhealthy'
            self.assertEqual(value['metrics']['q4t_gpu_healthy'], 1)
            return value
        rc, state = self.run_supervisor(observe, restarts=3)
        self.assertEqual((rc, state['attempts'], state['restarts']), (1, 1, 0))
        self.assertEqual(state['error'], 'Backend unhealthy')
        self.assertTrue(state['cleanup_confirmed'])
        self.assertEqual(len(set(seen)), 1)
        self.assertFalse(sup.processes.group_members(seen[0]))

    def test_memory_unready_is_distinct_from_permanent_backend_failure(self):
        value = self.observation(ready=False)
        for status in ('memory_pressure', 'memory_observation_unavailable'):
            value['health_status'] = status
            self.assertEqual(sup.classify(value, 'protocol', 'binary'), 'DEGRADED')
        value['health_status'] = 'unhealthy'
        self.assertEqual(sup.classify(value, 'protocol', 'binary'),
                         'BACKEND_UNHEALTHY')
        value['metrics']['q4t_gpu_healthy'] = 0
        self.assertEqual(sup.classify(value, 'protocol', 'binary'), 'GPU_UNHEALTHY')

    def test_probe_preserves_unhealthy_readiness_when_cuda_is_healthy(self):
        health = dict(status='unhealthy', live=True, ready=False)
        def get(_port, path, **_options):
            if path in ('/livez', '/readyz'):
                return dict(http_status=200 if path == '/livez' else 503,
                            body=json.dumps(health))
            if path == '/v1/config':
                return dict(http_status=200, body=json.dumps(
                    dict(observability_version=4)))
            return dict(http_status=200, body='metrics\n')
        config = dict(sup.expected_configuration(), protocol_sha256='protocol')
        with mock.patch.object(sup.identity, 'process', return_value={}), \
             mock.patch.object(sup.transport, 'get', side_effect=get), \
             mock.patch.object(sup.contracts, 'configuration', return_value=config), \
             mock.patch.object(sup.contracts, 'parse_metrics',
                               return_value=({'q4t_gpu_healthy': 1}, 0)):
            result = sup.probe(42, 18999)
        self.assertFalse(result['ready'])
        self.assertEqual(result['health_status'], 'unhealthy')
        self.assertEqual(result['metrics']['q4t_gpu_healthy'], 1)

    def test_protocol_mismatch_fails_closed(self):
        def observe(pid, _port):
            value = self.observation(pid)
            value['protocol'] = 'different'
            return value
        rc, state = self.run_supervisor(observe, restarts=3)
        self.assertEqual((rc, state['attempts']), (1, 1))
        self.assertIn('protocol mismatch', state['error'])

    def test_effective_mtp_or_admission_mismatch_fails_closed(self):
        for key, value in (('mtp_enabled', False), ('structured_backend', False),
                           ('max_sequences', 2), ('memory_enabled', False)):
            observation = self.observation()
            observation['configuration'][key] = value
            with self.assertRaisesRegex(RuntimeError, 'configuration differs'):
                sup.classify(observation, 'protocol', 'binary')

    def test_observation_loss_has_bounded_grace(self):
        count = 0
        def observe(pid, _port):
            nonlocal count
            count += 1
            if count == 1:
                return self.observation(pid)
            raise sup.contracts.InvalidObservation('missing_required_metric')
        rc, state = self.run_supervisor(observe, restarts=3)
        self.assertEqual((rc, state['attempts']), (1, 1))
        self.assertEqual(state['error'], 'observation deadline exceeded')

    def test_startup_deadline_does_not_restart(self):
        def observe(_pid, _port):
            raise sup.contracts.InvalidObservation('connection_refused')
        rc, state = self.run_supervisor(observe, restarts=3)
        self.assertEqual((rc, state['attempts']), (1, 1))
        self.assertEqual(state['error'], 'startup deadline exceeded')

    def test_normal_nonzero_exit_is_not_signal_recovery(self):
        rc, state = self.run_supervisor(lambda pid, _: self.observation(pid),
                                        restarts=3, script='raise SystemExit(1)')
        self.assertEqual((rc, state['attempts']), (1, 1))

    def test_orphan_grammar_like_child_is_reaped(self):
        pid_file = self.root/'grandchild.pid'
        script = ('import os,time,pathlib\n'
                  'pid=os.fork()\n'
                  f'pathlib.Path({str(pid_file)!r}).write_text(str(pid)) if pid else None\n'
                  'time.sleep(30)\n')
        seen, orphan = [], []
        def observe(pid, _port):
            if pid not in seen:
                seen.append(pid)
                if len(seen) == 2:
                    self.assertFalse(Path(f'/proc/{orphan[0]}').exists())
                    pkg.save(self.root/'runtime/stop.request.json', {})
            if len(seen) == 1 and pid_file.exists() and not orphan:
                value = pid_file.read_text()
                if value:
                    orphan.append(int(value))
                    os.kill(pid, signal.SIGKILL)
            return self.observation(pid)
        rc, state = self.run_supervisor(observe, restarts=1, script=script)
        self.assertEqual((rc, state['attempts']), (0, 2))
        self.assertTrue(orphan)

    def test_log_write_error_stops_and_is_reported(self):
        original = sup.RotatingLog.write
        def write(logger, data):
            if logger.path.name == 'backend.log':
                raise OSError('fixed disk failure')
            return original(logger, data)
        rc, state = self.run_supervisor(lambda pid, _: self.observation(pid),
            restarts=3, script='import time; print("log",flush=True); time.sleep(30)',
            patch=mock.patch.object(sup.RotatingLog, 'write', write))
        self.assertEqual((rc, state['attempts']), (1, 1))
        self.assertIn('log capture failed', state['error'])
        self.assertFalse(sup.processes.group_members(state['backend']['pid']))

    def test_logs_have_actual_byte_bound(self):
        for name, logger in (
                ('backend.log', sup.RotatingLog(self.root/'backend.log', 31)),
                ('guard.jsonl', BoundedLog(self.root/'guard.jsonl', 31))):
            data = b'x'*100
            logger.write(data if name == 'backend.log' else data.decode())
            path = self.root/name
            previous = (path.with_suffix('.previous') if name == 'backend.log'
                        else path.with_name(path.name+'.previous'))
            self.assertEqual(path.stat().st_size, 7)
            self.assertEqual(previous.stat().st_size, 31)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path = self.root/'full.log'
        with open_log(path, 'w') as stream:
            stream.write('x'*100)
        self.assertEqual(path.stat().st_size, 100)

    def test_wrong_start_or_boot_never_signals_reused_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            owner = sup.process_identity(child.pid)
            owner['start_ticks'] += 1
            self.assertFalse(sup.signal_owner(owner, signal.SIGTERM))
            owner = sup.process_identity(child.pid)
            owner['boot_id'] = 'wrong'
            self.assertFalse(sup.signal_owner(owner, signal.SIGTERM))
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(3)

    def test_stop_persists_intent_when_controller_absent(self):
        pkg.save(self.root/'controller.json', dict(owner=dict(
            pid=2**30, start_ticks=1, boot_id='absent')))
        result = service.stop(self.root)
        self.assertEqual(result['state'], 'OWNER_ABSENT')
        self.assertFalse(result['cleanup_confirmed'])
        self.assertTrue((self.root/'stop.request.json').exists())

    def test_terminal_run_is_not_reactivated_or_signalled(self):
        pkg.save(self.root/'controller.json', dict(owner={}))
        pkg.save(self.root/'terminal.json', dict(state='STOPPED',
                                               automatic_reactivation=False))
        with mock.patch.object(sup, 'signal_owner') as send:
            self.assertEqual(service.stop(self.root)['state'], 'STOPPED')
            send.assert_not_called()
        with self.assertRaises(FileExistsError):
            pkg.private_directory(self.root)

    def test_internal_worker_needs_real_controller_parent(self):
        pkg.save(self.root/'controller.json', dict(owner=sup.process_identity(os.getpid())))
        with self.assertRaisesRegex(RuntimeError, 'live lease controller child'):
            service.verify_worker(self.root)

    def test_busy_port_rejects_before_backend_launch(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 18999))
            listener.listen(1)
            rc, state = self.run_supervisor(lambda pid, _: self.observation(pid))
        self.assertEqual((rc, state['attempts']), (1, 0))
        self.assertTrue(state['cleanup_confirmed'])

    def test_guard_retention_is_opt_in_and_preserves_terminal_receipt(self):
        out, lock = self.root/'guard', self.root/'lease.lock'
        snapshot = dict(conflicts=[], detail='fixed-record'*9000)
        with mock.patch.object(service.guard, 'inspect', return_value=snapshot):
            rc = service.guard.run_guard([sys.executable, '-c',
                'import time; time.sleep(.04)'], out, lock, interval=.01,
                compute_backend='nvidia-smi', log_max_bytes=65536)
        self.assertEqual(rc, 0)
        self.assertLessEqual((out/'events.jsonl').stat().st_size, 65536)
        self.assertEqual((out/'events.jsonl.previous').stat().st_size, 65536)
        terminal = pkg.read(out/'exit.json')
        self.assertEqual(terminal['status'], 'COMPLETED')
        self.assertFalse(terminal['retention']['complete_history_retained'])

    def test_parent_death_across_exec_keeps_lease_until_backend_exit(self):
        # A real CPU-only controller, inherited flock, exec and SIGKILL. The
        # mocked guard scan never invokes NVML or starts a GPU/HTTP payload.
        fixture = self.root/'child.py'
        fixture.write_text(f'''import json,os,signal,sys,time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tools.serve_service import supervisor as s
root=Path({str(self.root)!r})
if len(sys.argv)==1:
    s.arm_parent_death(s.process_identity(os.getppid()))
    os.set_inheritable(int(os.environ['Q4T_MANAGED_LEASE_FD']), True)
    os.execv(sys.executable, [sys.executable, '-B', __file__, 'execed'])
signal.signal(signal.SIGTERM, lambda *_: (root/'parent-death').write_text('received'))
(root/'child.json').write_text(json.dumps(s.process_identity(os.getpid())))
while not (root/'finish').exists(): time.sleep(.01)
''')
        code = f'''import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT/'tools/evalscope')!r})
import gpu_exclusive as g
g.inspect=lambda *args: {{'conflicts': []}}
raise SystemExit(g.run_guard([sys.executable, '-B', {str(fixture)!r}],
    Path({str(self.root/'guard')!r}), Path({str(self.root/'lease.lock')!r}),
    interval=.02, compute_backend='nvidia-smi', inherit_lease=True))
'''
        owner = None
        with sup.processes.child_subreaper():
            control = subprocess.Popen([sys.executable, '-B', '-c', code],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                def wait_file(path):
                    until = time.monotonic()+5
                    while not path.exists() and time.monotonic() < until:
                        time.sleep(.01)
                    self.assertTrue(path.exists(), str(path))
                wait_file(self.root/'child.json')
                owner = pkg.read(self.root/'child.json')
                control.kill()
                control.wait(3)
                wait_file(self.root/'parent-death')
                with (self.root/'lease.lock').open('r+') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                (self.root/'finish').touch()
                until = time.monotonic()+3
                while time.monotonic() < until:
                    waited, _ = os.waitpid(owner['pid'], os.WNOHANG)
                    if waited:
                        break
                    time.sleep(.01)
                self.assertEqual(waited, owner['pid'])
                owner = None
                with (self.root/'lease.lock').open('r+') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if control.poll() is None:
                    control.kill()
                control.communicate(timeout=3)
                if owner is not None:
                    sup.signal_owner(owner, signal.SIGKILL)
                    os.waitpid(owner['pid'], 0)

    def test_policy_matrix(self):
        for code in (0, 1, 75, -9, -15):
            for ready in (False, True):
                for used in (0, 1, 2):
                    for stopping in (False, True):
                        self.assertEqual(sup.restart_allowed(code, ready, used, 2, stopping),
                            code < 0 and ready and used < 2 and not stopping)

    def test_operator_stop_never_masks_environment_or_cleanup_failure(self):
        for reason in ('competing workload during lease',
                       'monitor failed', 'KeyboardInterrupt()'):
            self.assertFalse(service.normal_stop(True, True, dict(
                status='ENV_ABORT', reason=reason, child_returncode=0)))
        interrupt = dict(status='ENV_ABORT',
            reason="KeyboardInterrupt('managed service stopped')", child_returncode=0)
        self.assertTrue(service.normal_stop(True, True, interrupt))
        self.assertFalse(service.normal_stop(True, False, interrupt))
        self.assertFalse(service.normal_stop(False, True, interrupt))
        self.assertTrue(service.normal_stop(True, True, dict(status='COMPLETED')))

    def test_memory_gate_refuses_launch_on_low_or_unknown_sample(self):
        for available in (0, None):
            with self.subTest(available=available):
                reader = lambda: dict(total_bytes=8*1024**3,
                                      available_bytes=available)
                with mock.patch.object(sup.subprocess, 'Popen') as launch:
                    rc, state = self.run_supervisor(
                        lambda *_: self.fail('unexpected probe'),
                        read_memory=reader, restarts=3)
                launch.assert_not_called()
                self.assertEqual((rc, state['attempts'], state['restarts']),
                                 (1, 0, 0))
                self.assertTrue(state['cleanup_confirmed'])
                import shutil
                shutil.rmtree(self.root/'runtime')

    def test_memory_trip_interrupts_blocked_probe_and_prevents_restart(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                armed = threading.Event()
                def reader():
                    if armed.is_set() and unknown:
                        raise OSError('observation failed')
                    return dict(total_bytes=8*1024**3,
                        available_bytes=0 if armed.is_set() else 4*1024**3)
                def observe(pid, _port):
                    armed.set()
                    # Deliberately block this HTTP-equivalent callback. The
                    # independent watchdog must terminate the real CPU child.
                    until = time.monotonic()+1
                    while time.monotonic() < until:
                        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(
                            ')', 1)[1].split()
                        if fields[0] == 'Z':
                            return self.observation(pid)
                        time.sleep(.005)
                    self.fail('blocked probe prevented protective termination')
                rc, state = self.run_supervisor(observe, restarts=3,
                                                read_memory=reader)
                self.assertEqual((rc, state['attempts'], state['restarts']),
                                 (1, 1, 0))
                self.assertEqual(state['error'], 'memory_observation_unavailable'
                    if unknown else 'critical_memory_pressure')
                self.assertTrue(state['memory']['signal_sent'])
                self.assertTrue(state['cleanup_confirmed'])
                self.assertFalse(sup.processes.group_members(state['backend']['pid']))
                import shutil
                shutil.rmtree(self.root/'runtime')

    def test_operator_stop_cannot_hide_memory_trip_during_cleanup(self):
        armed = threading.Event()
        def reader():
            return dict(total_bytes=8*1024**3,
                        available_bytes=0 if armed.is_set() else 4*1024**3)
        def observe(pid, _port):
            pkg.save(self.root/'runtime/stop.request.json', {})
            armed.set()
            return self.observation(pid)
        real_cleanup = sup.processes.stop_children
        def slow_cleanup(*args, **kwargs):
            time.sleep(.08)
            return real_cleanup(*args, **kwargs)
        rc, state = self.run_supervisor(observe, read_memory=reader,
            patch=mock.patch.object(sup.processes, 'stop_children', slow_cleanup))
        self.assertEqual(rc, 1)
        self.assertEqual(state['error'], 'critical_memory_pressure')
        self.assertTrue(state['cleanup_confirmed'])


class PackageTests(unittest.TestCase):
    setUp = Case.setUp

    def fixture(self):
        model = self.root/'model'
        model.mkdir()
        for name in ('config.json', 'tokenizer.json', 'tokenizer_config.json'):
            pkg.save(model/name, {})
        binary, cache, accepted, release = [self.root/n for n in
            ('fake-q4t', 'cache', 'accepted.json', 'release.json')]
        binary.write_text('CPU fixture, never executed')
        cache.write_text('CPU fixture cache')
        pkg.save(accepted, dict(status='PASS_CPU_FIXTURE'))
        pkg.save(release, dict(binary_sha256=pkg.sha(binary),
            build_cache=str(cache), build_cache_sha256=pkg.sha(cache),
            acceptance='ACCEPT_CPU_FIXTURE', acceptance_decision=str(accepted),
            acceptance_decision_sha256=pkg.sha(accepted),
            chat_protocol=dict(protocol_sha256='a'*64)))
        metadata = pkg.read(release)
        bundle = self.root/'declarations'
        (bundle/'schemas').mkdir(parents=True)
        caps = {key: {} for key in pkg.DECLARATION_SECTIONS}
        caps.update(model='cpu-fixture', protocol_source_sha256='a'*64,
                    observability=dict(version=5))
        config = dict(model='cpu-fixture', protocol_source_sha256='a'*64,
                      observability_version=5)
        pkg.save(bundle/'capabilities.json', caps)
        pkg.save(bundle/'configuration.json', config)
        for name in pkg.DECLARATION_SCHEMAS:
            path = bundle/'schemas'/(name+'.json')
            pkg.save(path, {'type': 'object'})
            metadata['chat_protocol'][name+'_schema'] = str(path)
            metadata['chat_protocol'][name+'_schema_sha256'] = pkg.sha(path)
        metadata['chat_protocol'].update(
            {key: caps[key] for key in pkg.DECLARATION_SECTIONS})
        metadata['observed_configuration'] = config
        metadata['release_declarations'] = dict(profile=pkg.DECLARATION_PROFILE,
            directory=str(bundle), binary_sha256=pkg.sha(binary),
            protocol_sha256='a'*64,
            files={name: pkg.sha(bundle/name) for name in pkg.DECLARATION_FILES})
        pkg.save(release, metadata)
        return binary, release, model

    def test_snapshot_is_independent_of_worktree_and_original_pair(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        binary.write_text('subsequent default replacement')
        release.unlink()
        manifest = pkg.verify(target, receipt['manifest_sha256'])
        self.assertEqual(manifest['settings']['restart_limit'], 0)
        self.assertEqual(manifest['files']['q4t'], receipt['binary_sha256'])
        args = pkg.command(target, manifest)
        self.assertIn('--mtp', args)
        self.assertNotIn('--no-mtp', args)
        self.assertIn('127.0.0.1', args)
        self.assertFalse(manifest['managed_service_accepted'])

    def test_package_or_model_changes_are_rejected(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        script = target/'source/tools/serve_service/supervisor.py'
        old = script.read_bytes()
        script.chmod(0o600)
        script.write_bytes(old+b'\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(target, receipt['manifest_sha256'])
        script.write_bytes(old)
        (model/'config.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(RuntimeError, 'model metadata changed'):
            pkg.verify(target, receipt['manifest_sha256'])

    def test_inconsistent_acceptance_rejected_before_output_creation(self):
        binary, release, model = self.fixture()
        metadata = pkg.read(release)
        metadata['acceptance'] = 'PENDING'
        pkg.save(release, metadata)
        target = self.root/'package'
        with self.assertRaisesRegex(RuntimeError, 'accepted release required'):
            pkg.prepare(binary, release, model, target)
        self.assertFalse(target.exists())

    def test_unknown_config_or_inventory_rejected(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        original = pkg.read(target/'manifest.json')
        changed = copy.deepcopy(original)
        changed['settings']['profile']['host'] = '0.0.0.0'
        pkg.save(target/'manifest.json', changed)
        with self.assertRaisesRegex(RuntimeError, 'unknown managed configuration'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))
        changed = copy.deepcopy(original)
        del changed['files']['source/tools/serve_service/supervisor.py']
        pkg.save(target/'manifest.json', changed)
        with self.assertRaisesRegex(RuntimeError, 'inventory differs'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_unit_has_no_automatic_activation_and_binds_snapshot(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        text = service.unit_text(receipt['package'], receipt['manifest_sha256'],
                                 self.root/'runtime % $')
        self.assertIn('Restart=no\n', text)
        self.assertIn('KillMode=control-group\n', text)
        self.assertNotIn('[Install]', text)
        self.assertIn(receipt['manifest_sha256'], text)
        self.assertIn('runtime %% $$', text)
        path = self.root/'q4t-managed-test.service'
        path.write_text(text)
        result = subprocess.run(['systemd-analyze', '--user', 'verify', str(path)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_port_or_unbounded_restart_rejected(self):
        for port, limit in ((0, 0), (8080, 4), (True, 0), (8080, True)):
            with self.assertRaises(RuntimeError):
                pkg.settings(port, limit)

    def test_critical_floor_is_explicit_bounded_and_pinned(self):
        for invalid in (0, -1, True, 1024**2+1, 1.0):
            with self.assertRaises(RuntimeError):
                pkg.settings(8080, 0, invalid)
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              critical_memory_mib=30720)
        manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertEqual(manifest['version'], 3)
        self.assertEqual(manifest['settings']['critical_memory_mib'], 30720)

    def test_legacy_package_inventory_keeps_legacy_configuration(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.read(target/'manifest.json')
        manifest['version'] = 1
        del manifest['files']['source/tools/serve_service/memory.py']
        del manifest['files']['source/tools/serve_service/inspection.py']
        for key in ('critical_memory_mib', 'memory_poll_seconds',
                    'memory_low_seconds', 'memory_unknown_seconds'):
            del manifest['settings'][key]
        pkg.save(target/'manifest.json', manifest)
        old = pkg.verify(target, pkg.sha(target/'manifest.json'))
        self.assertNotIn('critical_memory_mib', old['settings'])

    def test_v2_inventory_retains_memory_without_inspection(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package')
        target = Path(receipt['package'])
        manifest = pkg.read(target/'manifest.json')
        manifest['version'] = 2
        del manifest['files']['source/tools/serve_service/inspection.py']
        pkg.save(target/'manifest.json', manifest)
        old = pkg.verify(target, pkg.sha(target/'manifest.json'))
        self.assertEqual(old['settings']['critical_memory_mib'], 1024)


if __name__ == '__main__':
    unittest.main()
