"""Production model lifecycle under Guardian authority; never replays requests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import client_policy as cp, gateway as ingress
from tools.serve_ops import transport
from tools.serve_recovery import binding, bundle, child, control, core_http, jobs
from tools.serve_recovery import journal, ledger, monitor, observation_io, ownership, policy
from tools.serve_recovery import progress
from tools.serve_service import memory, package as engine, supervisor


def health_fault(value, status):
    """Only explicit core facts authorize recovery; a lost observer does not."""
    ledger.require(type(value) is dict and value.get('status') in ('ok', 'unhealthy') and
                   type(value.get('gpu_healthy')) is bool,
                   'invalid native health observation')
    if value['status'] == 'ok':
        ledger.require(status == 200 and value['gpu_healthy'], 'contradictory native health')
        return None
    ledger.require(status == 503, 'invalid unhealthy HTTP status')
    return policy.FaultKind.PLE_UNAVAILABLE if value['gpu_healthy'] else policy.FaultKind.CUDA_CONTEXT


def model_command(package, base, config, policy_path):
    command = engine.command(Path(package)/'engine', base,
                              state_directory=config.get('state_directory'))
    command[command.index('--client-policy-file')+1] = str(policy_path)
    return command


class Controller:
    def __init__(self, config, channel):
        version = config.get('version')
        ledger.require(type(version) is int and version in (1, 2),
                       'unsupported controller config')
        ledger.keys(config, ('version', 'package', 'manifest_sha256', 'directory') +
                    (('state_directory',) if version == 2 else ()))
        self.config, self.channel = config, channel
        self.package = Path(config['package'])
        self.root_pid = int(os.environ['Q4T_GUARDIAN_PID'])
        self.root_start = int(os.environ['Q4T_GUARDIAN_START'])
        self.lease_fd = int(os.environ['Q4T_LEASE_FD'])
        self.lease_path = os.environ['Q4T_LEASE_PATH']
        self.continuous = os.environ['Q4T_CONTINUOUS_MODEL_RECOVERY'] == '1'
        child.verify_lease(self.lease_fd, self.lease_path)
        self.stopping = False
        self.phase, self.reason = 'STARTING', 'initializing'
        self.heartbeat_at = 0
        self.monitor = monitor.Monitor(self.lease_fd)
        self.observations = observation_io.Observations()
        self.model, self.grant, self.probe_job = None, None, None
        self.ready = False
        self.frozen = False
        self.quarantined = False
        self.watchdog = None
        self.logger = None
        self.capture = None
        self.need_cuda_probe = False
        self.probe_passed = False
        self.last_fault = policy.FaultKind.MODEL_EXIT
        self.completed_self_checks = 0
        self.last_core_check = 0
        self.last_protect = 0
        self.ownership_unknown = False
        self.stable_at = None
        self.stable_reported = False
        self.progress = None
        self.progress_detector = progress.Detector()
        self.progress_fault = None
        self.directory = Path(config['directory'])/f'controller-{channel.generation}'
        self.directory.mkdir(mode=0o700)
        self.manifest = bundle.verify(self.package, config['manifest_sha256'])
        self.base = engine.verify(self.package/'engine', self.manifest['engine_manifest_sha256'])
        self.settings = self.base['settings']
        # Validate state binding before authorizing any model launch. The
        # directory is deployment-scoped, never controller/model-generation-scoped.
        engine.command(self.package/'engine', self.base,
                       state_directory=config.get('state_directory'))
        _, self.base_policy = cp.read(self.package/'engine/client-policy.json')
        self.check_client = core_http.self_check_client(self.base_policy)
        self.phase, self.reason = 'WAIT_DEPENDENCY', 'cold_preflight'

    def observation(self):
        if self.monitor.fatal:
            return 'DEVICE_FAILURE'
        if (self.monitor.visibility != 'AVAILABLE' or
                self.observations.unavailable or
                self.ownership_unknown or
                (self.ready and (self.progress is None or
                                 self.progress.snapshot() is None))):
            return 'UNKNOWN'
        return 'AVAILABLE'

    def tick(self, *, allow_query=True):
        # No detached heartbeat thread. Every receipt, HTTP wait and cleanup
        # progresses this same loop; jobs never block it on NVML or fsync.
        self.monitor.tick(allow_query=allow_query and not self.stopping and not self.frozen)
        if self.logger is not None:
            self.logger.tick()
        try:
            value = self.progress.poll() if self.progress is not None else None
            self.progress_fault = self.progress_detector.assess(value)
        except Exception:
            # Collector/decoder failures are auxiliary, including unexpected
            # Python errors. Never let an observer restart a healthy model.
            value, self.progress_fault = None, None
            self.progress_detector.pending.clear()
            self.progress_detector.confirmed = None
            if self.progress is not None:
                self.progress.unknown('progress_collector_failed')
        now = time.monotonic()
        if now >= self.heartbeat_at:
            reason = (self.monitor.category.lower() if self.phase == 'READY' and
                      self.monitor.visibility != 'AVAILABLE' else self.reason)
            if (self.phase == 'READY' and self.observations.unavailable and
                    not self.monitor.fatal):
                reason = 'auxiliary_io_unavailable'
            elif self.phase == 'READY' and value is None and not self.monitor.fatal:
                reason = 'progress_observation_unavailable'
            self.channel.health(self.phase, self.observation(), reason,
                                auxiliary_io=self.observations.snapshot(self.logger))
            self.channel.event('heartbeat')
            self.heartbeat_at = now+0.25
        return self.stopping or self.monitor.fatal

    def idle(self, seconds, *, allow_query=True):
        end = time.monotonic()+seconds
        while not self.stopping and time.monotonic() < end:
            self.tick(allow_query=allow_query)
            time.sleep(0.025)

    def transaction(self, operation, args):
        self.channel.begin(operation, args)
        end = time.monotonic()+7
        while time.monotonic() < end:
            reply = self.channel.poll()
            if reply is not None:
                return reply
            # No new auxiliary child may race an empty-tree authorization or
            # retirement check. Existing observation workers still advance.
            self.tick(allow_query=False)
            time.sleep(0.025)
        raise TimeoutError('Guardian model transaction deadline')

    def quarantine(self, reason):
        self.phase, self.reason, self.quarantined = 'QUARANTINED', reason, True
        self.transaction('quarantine', dict(reason=reason))

    def protect(self, *, force=False):
        if self.monitor.fatal:
            self.quarantine('device_failure')
            return True
        if not force and time.monotonic()-self.last_protect < 1:
            return False
        self.last_protect = time.monotonic()
        try:
            observed = ownership.observe(self.root_pid, self.root_start, self.monitor.pids)
            child.verify_lease(self.lease_fd, self.lease_path)
        except (OSError, ValueError):
            self.quarantine('identity_invalid')
            return True
        self.ownership_unknown = bool(observed['unconfirmed_compute'])
        if observed['conflicts']:
            self.quarantine('reconciliation_required')
            return True
        return False

    def preflight(self):
        self.tick()
        if self.protect(force=True):
            return None
        if self.ownership_unknown:
            self.reason = 'compute_exit_unconfirmed'
            return None
        if self.monitor.job is not None:
            self.reason = 'query_pending'
            return None
        try:
            if memory.read()['available_bytes'] < 4096*1024**2:
                self.reason = 'memory_headroom'
                return None
            bundle.check_assets(Path(self.base['model_dir']), self.manifest['model_assets'], read_edges=True)
            supervisor.preflight_ports(self.settings)
        except OSError:
            self.reason = 'dependency_unavailable'
            return None
        except ValueError:
            self.quarantine('identity_invalid')
            return None
        except RuntimeError:
            self.reason = 'listener_unavailable'
            return None
        usable = self.monitor.visibility == 'AVAILABLE' and not self.need_cuda_probe
        if not usable and self.probe_passed:
            usable = True
        if not usable:
            if not self.continuous:
                self.reason = 'cold_nvml_unavailable'
                return None
            proof = dict(cleanup_complete=True, resources_safe=True,
                         identity_valid=True, lease_continuous=True, device_usable=False)
            reply = self.transaction('probe', dict(role='model', proof=proof))
            if not reply['ok']:
                self.reason = reply['result']['reason']
                return None
            self.phase, self.reason = 'RECOVERING', 'cuda_probe'
            self.probe_job = jobs.Job([str(self.package/'bin/q4t-cuda-probe')],
                lease_fd=self.lease_fd, timeout=5, maximum=4096)
            result = jobs.finish(self.probe_job, lambda: self.tick(allow_query=False))
            if result is None:
                self.quarantine('cleanup_failed')
                return None
            self.probe_job = None
            try:
                value = json.loads(result['output'])
                ledger.keys(value, ('version', 'ok', 'cuda_code', 'stage'))
                ledger.require(type(value['version']) is int and value['version'] == 1 and
                    type(value['ok']) is bool and type(value['cuda_code']) is int,
                    'invalid CUDA probe receipt')
            except (ValueError, TypeError):
                self.reason = 'cuda_probe_invalid'
                return None
            if value['cuda_code'] == 214:  # CUDA 13.3 cudaErrorECCUncorrectable.
                self.quarantine('device_failure')
                return None
            usable = (result['returncode'] == 0 and result['reason'] is None and
                      value['ok'] and value['cuda_code'] == 0 and value['stage'] == 'complete')
            if not usable:
                self.reason = 'cuda_probe_failed'
                return None
            self.probe_passed = True
        evidence = policy.Evidence(cleanup_complete=True, resources_safe=True,
            identity_valid=True, lease_continuous=True, device_usable=usable,
            dependency_ready=True)
        decision = policy.decide(policy.Fault(self.last_fault, self.channel.generation, 0),
            policy.Context(policy.Desired.RUNNING, self.channel.generation, 0, evidence))
        if decision.action != policy.Action.RESTART_MODEL:
            self.reason = decision.reason
            return None
        proof = vars(evidence).copy()
        del proof['dependency_ready']
        return proof

    def load(self, proof):
        # Verify immutable deployment inputs again before requesting authority.
        try:
            bundle.verify(self.package, self.config['manifest_sha256'])
        except OSError:
            self.phase, self.reason = 'WAIT_DEPENDENCY', 'bundle_unavailable'
            return False
        except (ValueError, KeyError):
            self.quarantine('identity_invalid')
            return False
        reply = self.transaction('authorize', dict(role='model', proof=proof))
        if not reply['ok']:
            self.reason = reply['result']['reason']
            self.frozen = self.reason in ('ledger_unavailable', 'recovery_frozen')
            return False
        result = reply['result']
        if not result['allowed']:
            self.reason = result['reason']
            self.phase = 'CIRCUIT_OPEN' if result['reason'] == 'budget_exhausted' else 'WAIT_DEPENDENCY'
            self.idle(min(5, result.get('retry_after_ns', 5_000_000_000)/1e9))
            return False
        self.grant = result['grant']
        self.continuous = True
        self.probe_passed = self.need_cuda_probe = False
        self.phase, self.reason = 'STARTING', 'model_loading'
        target = self.directory/f"model-{self.grant['generation']}"
        target.mkdir(mode=0o700)
        token = secrets.token_hex(32)
        raw = binding.policy_bytes(self.base_policy, token)
        policy_path = target/'client-policy.json'
        ingress.save(policy_path, raw)
        command = model_command(self.package, self.base, self.config, policy_path)
        self.credentials = dict(client=self.check_client, token=token)
        self.current_binding = dict(port=self.settings['port'],
            operations_port=self.settings['operations_port'], gateway_token=token,
            client_policy_sha256=hashlib.sha256(raw).hexdigest(),
            binary_sha256=self.base['files']['q4t'], protocol_sha256=self.base['protocol_sha256'])
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        observed, publisher = None, None
        try:
            try:
                observed, publisher = socket.socketpair(socket.AF_UNIX,
                                                       socket.SOCK_SEQPACKET)
            except OSError:
                # Auxiliary allocation failure cannot prevent a model launch.
                pass
            progress_args = ([] if publisher is None else
                ['--progress-fd', str(publisher.fileno()),
                 '--service-id', self.channel.service_id])
            progress_fds = () if publisher is None else (publisher.fileno(),)
            self.model = subprocess.Popen([sys.executable, '-I', '-B',
                str(ROOT/'tools/serve_recovery/child.py'), '--fd', str(endpoint.fileno()),
                '--parent-pid', str(os.getpid()), '--parent-start', str(journal.process_start(os.getpid())),
                '--lease-fd', str(self.lease_fd), '--lease-path', self.lease_path,
                '--controller-generation', str(self.channel.generation),
                *progress_args, '--', *command],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                pass_fds=(endpoint.fileno(), self.lease_fd, *progress_fds),
                start_new_session=True)
            endpoint.close()
            if publisher is not None:
                publisher.close()
            self.logger = observation_io.Writer(target, self.lease_fd, self.observations)
            self.capture = observation_io.Capture(self.logger, self.model.stdout)
            parent.settimeout(2)
            self.owner = journal.receive(parent)
            ledger.require(self.owner.pop('version') == 1, 'invalid child identity receipt')
            if observed is not None:
                try:
                    identity = progress.Identity.owned(self.owner,
                        self.channel.service_id, self.channel.generation,
                        self.grant['generation'])
                    self.progress = progress.Reader(observed, identity)
                    observed = None  # Reader now owns this generation's FD.
                except (OSError, ValueError, KeyError):
                    # Missing namespace/observer state is not a core fault.
                    pass
            reply = self.transaction('spawned', dict(role='model', ticket=self.grant['ticket'], owner=self.owner))
            if not reply['ok']:
                self.frozen = True
                return False
            journal.send(parent, dict(version=1, ticket=self.grant['ticket'],
                controller_generation=self.channel.generation, model_generation=self.grant['generation']))
        finally:
            endpoint.close()
            parent.close()
            if observed is not None:
                observed.close()
            if publisher is not None:
                publisher.close()
        self.watchdog = memory.Watchdog(self.settings,
            lambda owner: supervisor.signal_owner(owner, signal.SIGTERM))
        self.watchdog.attach(self.owner)
        self.watchdog.start()
        end = time.monotonic()+300
        while not self.stopping and time.monotonic() < end:
            self.tick()
            if self.model.poll() is not None or self.watchdog.tripped.is_set() or self.protect():
                return False
            try:
                reply = transport.get(self.settings['operations_port'], '/readyz',
                                      stopped=self.tick)
                if reply['http_status'] == 200:
                    value = supervisor.probe(self.model.pid, self.settings['port'],
                        self.settings['operations_port'], stopped=self.tick, include_metrics=False)
                    try:
                        state = supervisor.classify(value, self.base['protocol_sha256'],
                            self.base['files']['q4t'], cp.binding(raw), self.settings['operations_port'])
                    except (ValueError, KeyError):
                        self.quarantine('identity_invalid')
                        return False
                    if state == 'READY':
                        break
            except (OSError, ValueError, RuntimeError):
                pass
            self.idle(0.25)
        else:
            self.reason = 'model_startup_deadline'
            return False
        self.phase, self.reason = 'STARTING', 'generation_self_check'
        self.completed_self_checks += 1
        check = core_http.self_check(self.settings['port'], self.credentials, stopped=self.tick)
        self.logger.save_self_check(check)
        # Load can overlap the quota window; readiness cannot bypass it.
        deadline = result['quota_not_before_monotonic_ns']
        self.reason = 'quota_window'
        while time.monotonic_ns() < deadline and not self.stopping:
            self.tick()
            if self.model.poll() is not None or self.protect():
                return False
            time.sleep(0.025)
        if self.stopping:
            return False
        for operation, extra in (('bind', dict(binding=self.current_binding)), ('ready', dict(verified=True))):
            reply = self.transaction(operation, dict(role='model', ticket=self.grant['ticket'], **extra))
            if not reply['ok']:
                self.frozen = True
                return False
        self.ready = True
        self.phase, self.reason = 'READY', 'serving'
        self.stable_at, self.stable_reported = time.monotonic(), False
        if self.progress is not None:
            self.progress.mark_ready(time.monotonic_ns())
        return True

    def close_model(self):
        if self.grant is not None:
            self.channel.admission(self.grant['generation'], False)
        self.ready = False
        if self.progress is not None:
            self.progress.close()
            self.progress = None
        self.progress_detector = progress.Detector()
        self.progress_fault = None
        if self.watchdog is not None:
            self.watchdog.close()
            self.watchdog = None
        if self.logger is not None:
            self.logger.stop()
        excluded = (() if self.monitor.job is None else (self.monitor.job.process.pid,))
        if not ownership.cleanup(lambda: self.tick(allow_query=False), exclude=excluded):
            self.quarantine('cleanup_failed')
            return False
        if self.model is not None:
            self.model.poll()
            self.model = None
        if ((self.capture is not None and not self.capture.stop()) or
                (self.logger is not None and not self.logger.reaped)):
            self.quarantine('cleanup_failed')
            return False
        self.capture = None
        self.logger = None
        end = time.monotonic()+7
        while self.monitor.job is not None and time.monotonic() < end:
            self.tick(allow_query=False)
            time.sleep(0.025)
        if self.monitor.job is not None:
            self.quarantine('cleanup_failed')
            return False
        if self.grant is not None:
            reply = self.transaction('retired', dict(role='model', ticket=self.grant['ticket'], verified=True))
            if not reply['ok']:
                self.frozen = True
                return False
            self.grant = None
        self.monitor.refresh_after_cleanup()
        return True

    def record_fault(self, kind):
        self.last_fault = kind
        self.need_cuda_probe = kind in (policy.FaultKind.CUDA_CONTEXT,
                                        policy.FaultKind.EXECUTION_DEADLINE)
        self.phase, self.reason = 'RECOVERING', kind.value.lower()
        if self.grant is None:
            return
        fingerprint = ledger.digest(dict(binary=self.base['files']['q4t'],
            assets=self.manifest['model_assets'], configuration=self.settings, cause=kind.value))
        reply = self.transaction('fault', dict(generation=self.grant['generation'],
            fingerprint=fingerprint, unknown=kind == policy.FaultKind.UNKNOWN_MODEL_EXIT))
        if not reply['ok']:
            self.frozen = True
        elif reply['result'].get('quarantine') is not None:
            self.quarantined = True
            self.phase, self.reason = 'QUARANTINED', 'repeated_model_fault'

    def supervise(self):
        self.tick()
        if self.protect():
            return policy.FaultKind.DEVICE_FAILURE
        if self.watchdog.tripped.is_set():
            return policy.FaultKind.RESOURCE_EXHAUSTED
        if self.model.poll() is not None:
            return policy.FaultKind.UNKNOWN_MODEL_EXIT
        if self.progress_fault is not None:
            # Only per-request, fresh native facts can reach this branch.
            # Retirement, cleanup and a device probe still precede a new grant.
            return policy.FaultKind.EXECUTION_DEADLINE
        now = time.monotonic()
        if now-self.last_core_check < 2:
            return None
        self.last_core_check = now
        try:
            health = core_http.request(self.settings['operations_port'], 'GET', '/healthz', stopped=self.tick)
            failure = health_fault(json.loads(health['body']), health['http_status'])
            if failure is not None:
                return failure
            ready = transport.get(self.settings['operations_port'], '/readyz', stopped=self.tick)
            opened = ready['http_status'] == 200 and json.loads(ready['body']).get('ready') is True
            self.channel.admission(self.grant['generation'], opened)
            self.phase, self.reason = ('READY', 'serving') if opened else ('WAIT_DEPENDENCY', 'core_not_ready')
        except (OSError, ValueError, RuntimeError, KeyError):
            self.channel.admission(self.grant['generation'], False)
            self.phase, self.reason = 'WAIT_DEPENDENCY', 'core_observation_unavailable'
        if (not self.stable_reported and self.phase == 'READY' and
                self.progress is not None and self.progress.stable()):
            reply = self.transaction('stable', dict(role='model', ticket=self.grant['ticket'], successful_request=True))
            self.stable_reported = reply['ok']
        return None

    def run(self):
        self.channel.event('ready')
        while not self.stopping:
            if self.quarantined or self.frozen:
                self.phase = 'QUARANTINED' if self.quarantined else 'WAIT_DEPENDENCY'
                self.idle(0.25)
                continue
            if self.ready:
                kind = self.supervise()
                if kind is None:
                    self.idle(0.05)
                    continue
                if not self.quarantined:
                    self.record_fault(kind)
                self.close_model()
                continue
            proof = self.preflight()
            if proof is None:
                self.idle(0.25)
                continue
            try:
                loaded = self.load(proof)
            except (OSError, ValueError, RuntimeError, KeyError):
                loaded = False
            if not loaded and self.grant is not None:
                if self.monitor.fatal and not self.quarantined:
                    self.quarantine('device_failure')
                elif not self.quarantined and not self.stopping:
                    self.record_fault(policy.FaultKind.UNKNOWN_MODEL_EXIT)
                self.close_model()
            self.idle(0.1)
        self.monitor.cancel()
        self.close_model()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    channel = control.ControllerChannel()
    controller = Controller(ingress.parse(ingress.read_file(args.config, private=True)), channel)
    def stop(_signal, _frame):
        controller.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        controller.run()
    finally:
        channel.close()


if __name__ == '__main__':
    main()
