"""Exact resource observation backport for separately retained native payloads.

The accepted origin is read-only. Only reviewed resource hooks, the existing
cold/profile-history shims and the inline auxiliary client change. Unrelated
controller behavior, including model fault handling, remains the origin's.
"""
import copy
import hashlib
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from tools.serve_recovery import bundle, cold_compat, ledger, profile_compat, profile_policy
from tools.serve_recovery import resource_client
from tools.serve_service import package as engine

PREFIX = 'source/tools/serve_recovery/'
MARKER = '\n\n# Q4T_RESOURCE_CLIENT_BACKPORT_V1\n'
BOOTSTRAP = ("import sys as _q4t_resource_sys\n"
    "from pathlib import Path as _q4t_resource_path\n"
    "_q4t_resource_root = str(_q4t_resource_path(__file__).resolve().parents[2])\n"
    "if _q4t_resource_root not in _q4t_resource_sys.path: _q4t_resource_sys.path.insert(0, _q4t_resource_root)\n\n")

# Exact, reviewable fragments: no complete Controller/Profiles replacement.
PATCHES = {'controller.py': [('def validate_configuration(config):\n'
                    "    version = config.get('version')\n"
                    '    ledger.require(type(version) is int and version in (1, 2, 3, 4, 5),\n'
                    "                   'unsupported controller config')\n"
                    "    ledger.keys(config, ('version', 'package', 'manifest_sha256', 'directory') +\n",
                    'def validate_configuration(config):\n'
                    "    version = config.get('version')\n"
                    '    ledger.require(type(version) is int and version in (1, 2, 3, 4, 5, 6),\n'
                    "                   'unsupported controller config')\n"
                    "    ledger.keys(config, ('version', 'package', 'manifest_sha256', 'directory') +\n"),
                   ("        (('retention_context',) if version >= 3 else ()) +\n"
                    "        (('limits',) if version >= 4 else ()) +\n"
                    "        (('profiles',) if version >= 5 else ()))\n"
                    '    # Retention-only provenance never participates in model/recovery decisions.\n'
                    '\n',
                    "        (('retention_context',) if version >= 3 else ()) +\n"
                    "        (('limits',) if version >= 4 else ()) +\n"
                    "        (('profiles',) if version >= 5 else ()) +\n"
                    "        (('resources', 'resource_worker') if version >= 6 else ()))\n"
                    '    if version >= 6:\n'
                    '        from tools.serve_recovery import resource_client\n'
                    "        resource_client.validate_binding(config['resources'])\n"
                    "        resource_client.validate_worker(config['resource_worker'])\n"
                    '    # Retention-only provenance never participates in model/recovery decisions.\n'
                    '\n'),
                   ('        self.usage = None\n'
                    '        self.profiles = None\n'
                    "        if self.manifest['version'] >= 8 and self.manifest.get('ingress_mode') == "
                    "'native':\n"
                    '            from tools.serve_recovery import quota_policy, quota_runtime, '
                    'usage_runtime\n',
                    '        self.usage = None\n'
                    '        self.profiles = None\n'
                    '        self.resource_memory = None\n'
                    "        if config['version'] >= 6:\n"
                    '            from tools.serve_recovery import resource_client as resource_memory\n'
                    "            ledger.require(config['resources']['publication']['service_id'] == "
                    'channel.service_id,\n'
                    "                           'resource memory service binding changed')\n"
                    "            self.resource_memory = resource_memory.Observer(config['resources'], "
                    'self.lease_fd,\n'
                    "                worker=config['resource_worker'])\n"
                    "        if self.manifest['version'] >= 8 and self.manifest.get('ingress_mode') == "
                    "'native':\n"
                    '            from tools.serve_recovery import quota_policy, quota_runtime, '
                    'usage_runtime\n'),
                   ("        if self.manifest.get('recovery_profiles') is not None:\n"
                    '            from tools.serve_recovery import profile_controller\n'
                    "            ledger.require(config['version'] == 5 and config['profiles'] == "
                    "config['limits'],\n"
                    "                           'profile state service binding changed')\n"
                    '            self.profiles = profile_controller.Profiles(self,\n',
                    "        if self.manifest.get('recovery_profiles') is not None:\n"
                    '            from tools.serve_recovery import profile_controller\n'
                    "            ledger.require(config['version'] >= 5 and config['profiles'] == "
                    "config['limits'],\n"
                    "                           'profile state service binding changed')\n"
                    '            self.profiles = profile_controller.Profiles(self,\n'),
                   ('        if self.monitor.fatal:\n'
                    "            return 'DEVICE_FAILURE'\n"
                    "        if (self.monitor.visibility != 'AVAILABLE' or\n"
                    '                self.observations.unavailable or\n'
                    '                self.ownership_unknown or\n',
                    '        if self.monitor.fatal:\n'
                    "            return 'DEVICE_FAILURE'\n"
                    "        resources = getattr(self, 'resource_memory', None)\n"
                    '        if (resources is not None and resources.snapshot() is None or\n'
                    "                self.monitor.visibility != 'AVAILABLE' or\n"
                    '                self.observations.unavailable or\n'
                    '                self.ownership_unknown or\n'),
                   ('        # progresses this same loop; jobs never block it on NVML or fsync.\n'
                    '        self.monitor.tick(allow_query=allow_query and not self.stopping and not '
                    'self.frozen)\n'
                    '        if self.logger is not None:\n'
                    '            self.logger.tick()\n',
                    '        # progresses this same loop; jobs never block it on NVML or fsync.\n'
                    '        self.monitor.tick(allow_query=allow_query and not self.stopping and not '
                    'self.frozen)\n'
                    "        resources = getattr(self, 'resource_memory', None)\n"
                    '        if resources is not None:\n'
                    '            try: resources.tick(allow_query=allow_query and not self.stopping and not '
                    'self.frozen)\n'
                    "            except Exception: resources.unknown('resource_collector_failed')\n"
                    '        if self.logger is not None:\n'
                    '            self.logger.tick()\n'),
                   ("            elif self.phase == 'READY' and value is None and not self.monitor.fatal:\n"
                    "                reason = 'progress_observation_unavailable'\n"
                    '            self.channel.health(self.phase, self.observation(), reason,\n'
                    '                                auxiliary_io=self.observations.snapshot(self.logger))\n',
                    "            elif self.phase == 'READY' and value is None and not self.monitor.fatal:\n"
                    "                reason = 'progress_observation_unavailable'\n"
                    "            if (self.phase == 'READY' and resources is not None and\n"
                    '                    resources.snapshot() is None and not self.monitor.fatal):\n'
                    "                reason = 'resource_observation_unavailable'\n"
                    '            self.channel.health(self.phase, self.observation(), reason,\n'
                    '                                '
                    'auxiliary_io=self.observations.snapshot(self.logger))\n'),
                   ("            self.reason = 'compute_exit_unconfirmed'\n"
                    '            return None\n'
                    '        if self.monitor.job is not None:\n'
                    "            self.reason = 'query_pending'\n"
                    '            return None\n',
                    "            self.reason = 'compute_exit_unconfirmed'\n"
                    '            return None\n'
                    "        resources = getattr(self, 'resource_memory', None)\n"
                    '        if self.monitor.job is not None or resources is not None and resources.job is '
                    'not None:\n'
                    "            self.reason = 'query_pending'\n"
                    '            return None\n'),
                   ('        if self.logger is not None:\n'
                    '            self.logger.stop()\n'
                    '        excluded = (() if self.monitor.job is None else '
                    '(self.monitor.job.process.pid,))\n'
                    '        if not ownership.cleanup(lambda: self.tick(allow_query=False), '
                    'exclude=excluded):\n'
                    "            self.quarantine('cleanup_failed')\n",
                    '        if self.logger is not None:\n'
                    '            self.logger.stop()\n'
                    "        resources = getattr(self, 'resource_memory', None)\n"
                    '        if resources is not None: resources.cancel()\n'
                    '        observers = [self.monitor, *(() if resources is None else (resources,))]\n'
                    '        excluded = tuple(o.job.process.pid for o in observers if o.job is not None)\n'
                    '        if not ownership.cleanup(lambda: self.tick(allow_query=False), '
                    'exclude=excluded):\n'
                    "            self.quarantine('cleanup_failed')\n"),
                   ('        self.logger = None\n'
                    '        end = time.monotonic()+7\n'
                    '        while self.monitor.job is not None and time.monotonic() < end:\n'
                    '            self.tick(allow_query=False)\n'
                    '            time.sleep(0.025)\n'
                    '        if self.monitor.job is not None:\n'
                    "            self.quarantine('cleanup_failed')\n"
                    '            return False\n',
                    '        self.logger = None\n'
                    '        end = time.monotonic()+7\n'
                    '        while any(o.job is not None for o in observers) and time.monotonic() < end:\n'
                    '            self.tick(allow_query=False)\n'
                    '            time.sleep(0.025)\n'
                    '        if any(o.job is not None for o in observers):\n'
                    "            self.quarantine('cleanup_failed')\n"
                    '            return False\n')],
 'profile_controller.py': [('from tools.serve_recovery import ledger, policy, profile_contract, '
                            'profile_memory\n'
                            'from tools.serve_recovery import profile_policy, profile_runtime\n'
                            'from tools.serve_service import supervisor\n'
                            '\n',
                            'from tools.serve_recovery import ledger, policy, profile_contract, '
                            'profile_memory\n'
                            'from tools.serve_recovery import profile_policy, profile_runtime\n'
                            'from tools.serve_recovery import resource_client as resource_memory\n'
                            'from tools.serve_service import supervisor\n'
                            '\n'),
                           ("            configuration=controller.manifest['recovery_profile_policy'])\n"
                            '        self.detector = None\n'
                            '        self.launch = None\n'
                            '        self.memory_fault = False\n',
                            "            configuration=controller.manifest['recovery_profile_policy'])\n"
                            '        self.detector = None\n'
                            '        self.resource_pressure = resource_memory.Pressure()\n'
                            '        self.launch = None\n'
                            '        self.memory_fault = False\n'),
                           ('        self.memory_fault = False\n'
                            '        self.detector = None\n'
                            '        self.selection.unpark()\n'
                            '\n',
                            '        self.memory_fault = False\n'
                            '        self.detector = None\n'
                            '        self.resource_pressure.reset()\n'
                            '        self.selection.unpark()\n'
                            '\n'),
                           ("                    controller.reason = runtime.error or 'profile_state_wait'\n"
                            '                if self.detector is not None: self.detector.reset()\n'
                            '                return False\n'
                            '            state = runtime.state\n',
                            "                    controller.reason = runtime.error or 'profile_state_wait'\n"
                            '                if self.detector is not None: self.detector.reset()\n'
                            '                self.resource_pressure.reset()\n'
                            '                return False\n'
                            '            state = runtime.state\n'),
                           ('            if self.detector is None:\n'
                            '                self.detector = profile_memory.Detector(self.registry, '
                            "state['configuration'])\n"
                            '            result = self.detector.assess(self.launch, '
                            'controller.watchdog.snapshot(),\n'
                            "                runtime.nanos(), ready=controller.phase == 'READY')\n"
                            '            if result is not None:\n'
                            "                runtime.prepare(result['target'], result['reason'], "
                            "controller.grant['generation'])\n",
                            '            if self.detector is None:\n'
                            '                self.detector = profile_memory.Detector(self.registry, '
                            "state['configuration'])\n"
                            '            now = runtime.nanos()\n'
                            "            resources = getattr(controller, 'resource_memory', None)\n"
                            '            observed = None if resources is None else resources.snapshot()\n'
                            "            q = self.registry['qualifications']\n"
                            '            extra = '
                            "q['primary']['startup_required_bytes']-q['light']['startup_required_bytes']\n"
                            '            recover = resources is None or '
                            'resource_memory.recovery_clear(observed, now, extra)\n'
                            '            result = self.detector.assess(self.launch, '
                            'controller.watchdog.snapshot(), now,\n'
                            "                ready=controller.phase == 'READY' and (self.launch != 'light' "
                            'or recover))\n'
                            "            if self.launch == 'primary' and controller.phase == 'READY':\n"
                            '                if self.resource_pressure.assess(observed, now, '
                            "state['configuration']['pressure_seconds']):\n"
                            "                    result = dict(target='light', "
                            "reason='sustained_memory_pressure')\n"
                            '            else: self.resource_pressure.reset()\n'
                            '            if result is not None:\n'
                            "                runtime.prepare(result['target'], result['reason'], "
                            "controller.grant['generation'])\n"),
                           ("            runtime.error = 'profile_state_unavailable'\n"
                            '            if self.detector is not None: self.detector.reset()\n'
                            '        return False\n'
                            '\n',
                            "            runtime.error = 'profile_state_unavailable'\n"
                            '            if self.detector is not None: self.detector.reset()\n'
                            '            self.resource_pressure.reset()\n'
                            '        return False\n'
                            '\n'),
                           ('        self.launch = None\n'
                            '        self.detector = None\n'
                            '        self.selection.park()\n'
                            '\n',
                            '        self.launch = None\n'
                            '        self.detector = None\n'
                            '        self.resource_pressure.reset()\n'
                            '        self.selection.park()\n'
                            '\n')]}


def apply(raw, parts):
    ledger.require(type(raw) is bytes and len(raw) <= 1024**2, 'resource adapter source bound')
    text = raw.decode('utf-8')
    for old, new in parts:
        ledger.require(text.count(old) == 1 and new not in text, 'retained resource hook requires review')
        text = text.replace(old, new)
    return text.encode()


def legacy_imports(raw):
    return raw.replace(b'from tools.serve_recovery import resource_client as resource_memory',
        b'from tools.serve_recovery import profile_memory as resource_memory').replace(
        b'from tools.serve_recovery import resource_client\n',
        b'from tools.serve_recovery import profile_memory as resource_client\n')


def changes(original, *, profile_history):
    original = Path(original)
    control = (original/cold_compat.SOURCE).read_bytes()
    if not cold_compat.aware(original): control = cold_compat.adapted(control)
    helper = Path(resource_client.__file__).read_bytes()
    memory = (original/(PREFIX+'profile_memory.py')).read_bytes()
    ledger.require(MARKER.encode() not in memory, 'resource adapter cannot reapply to its own output')
    result = {PREFIX+'controller.py': legacy_imports(apply(control, PATCHES['controller.py'])),
        PREFIX+'profile_controller.py': legacy_imports(apply(
            (original/(PREFIX+'profile_controller.py')).read_bytes(), PATCHES['profile_controller.py'])),
        PREFIX+'profile_memory.py': BOOTSTRAP.encode()+memory+MARKER.encode()+helper}
    if profile_history: result[profile_compat.SOURCE] = Path(profile_policy.__file__).read_bytes()
    return result


def aware(package):
    """Recognize the actual hooks and exact client, not a capability flag."""
    package = Path(package)
    try:
        for name, fragments in PATCHES.items():
            text = (package/(PREFIX+name)).read_bytes()
            native = all(text.count(new.encode()) == 1 for _,new in fragments)
            legacy = all(text.count(legacy_imports(new.encode())) == 1 for _,new in fragments)
            if not (native or legacy): return False
        helper = Path(resource_client.__file__).read_bytes()
        direct = package/(PREFIX+'resource_client.py')
        if direct.exists(): return direct.read_bytes() == helper
        memory = (package/(PREFIX+'profile_memory.py')).read_bytes()
        return memory.startswith(BOOTSTRAP.encode()) and memory.count(MARKER.encode()) == 1 and \
            memory.split(MARKER.encode(),1)[1] == helper
    except (OSError, ValueError, UnicodeError): return False


def verify(original, original_sha, target, target_sha):
    original, target = Path(original).resolve(strict=True), Path(target).resolve(strict=True)
    first = bundle.verify(original, original_sha); second = bundle.verify(target, target_sha)
    ledger.require(12 <= first['version'] <= 16, 'resource adapter needs a retained qualified service')
    profile_history = first['files'][profile_compat.SOURCE] != second['files'].get(profile_compat.SOURCE)
    replacements = changes(original, profile_history=profile_history)
    expected = copy.deepcopy(first)
    for name,data in replacements.items():
        expected['files'][name] = hashlib.sha256(data).hexdigest()
        ledger.require((target/name).read_bytes() == data, 'resource adapter bytes differ')
    ledger.require(second == expected and aware(target), 'resource adapter changed another payload contract')
    return dict(format='q4t-resource-controller-compatibility-v1', original=str(original),
        original_manifest_sha256=original_sha, payload=str(target), payload_manifest_sha256=target_sha,
        changed_sources={name:expected['files'][name] for name in replacements},
        engine_manifest_sha256=first['engine_manifest_sha256'], model_actions=False, business_acceptance=False)


def prepare(original, original_sha, output, *, profile_history=True):
    original = Path(original).resolve(strict=True); manifest = bundle.verify(original, original_sha)
    ledger.require(12 <= manifest['version'] <= 16 and manifest.get('recovery_profiles') is not None,
                   'resource adapter needs a qualified retained native payload')
    replacements = changes(original, profile_history=profile_history)
    ledger.require(not Path(output).resolve().is_relative_to(original), 'resource adapter output overlaps origin')
    target = engine.private_directory(output, Path(manifest['repository']))
    shutil.copytree(original, target, dirs_exist_ok=True, symlinks=True)
    for name,data in replacements.items():
        source = target/name; engine.regular(source)
        source.chmod(0o600); source.write_bytes(data); source.chmod(0o400)
        manifest['files'][name] = engine.sha(source)
    engine.save(target/'manifest.json', manifest); digest = engine.sha(target/'manifest.json')
    launcher = target/'bin/q4t'; engine.regular(launcher)
    launcher.chmod(0o700); launcher.write_bytes(bundle.launcher(digest, manifest['python']['path']))
    launcher.chmod(0o500)
    return verify(original, original_sha, target, digest)


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original',type=Path,required=True)
    parser.add_argument('--original-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.original,args.original_sha256,args.output),sort_keys=True))
