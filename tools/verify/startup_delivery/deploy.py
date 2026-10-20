"""Install an accepted product candidate with a verified rollback package.

Two files are replaced atomically individually, NOT as a cross-file power-loss
transaction. A durable recovery marker and an independent --rollback entry
handle the four known old/new pairs. Unknown identities are never overwritten.
No deployment runs before the independent six-stage reliability audit passes.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/startup-delivery-20261021'
OLD_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
DECISION = ROOT / 'docs/STARTUP_DELIVERY_2026-10-21.md'
STAGES = ('default-cold-v1', 'default-inherited-v1', 'c256-hot-v1',
          'c256-inherited-v1', 'boundary-v2', 'off-v1')
HEALTH = ('deploy-mtp-v1', 'deploy-plain-v1')
ATOMICITY = 'individual atomic replaces; not a cross-file power-loss transaction'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


build = module('startup_deployment_build', HERE / 'build.py')
sha, identity, check_identity = build.sha, build.identity, build.check_identity
require = build.require
_runner = None


def runner():
    global _runner
    if _runner is None:
        _runner = module('startup_deployment_runner', HERE / 'run.py')
    return _runner


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    """Create immutable receipts; evidence is never silently overwritten."""
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stamp():
    return dict(monotonic=time.monotonic(), wall=time.time())


def destinations():
    return ROOT / 'build/q4t', ROOT / 'build/q4t.release.json'


def require_regular(path):
    require(Path(path).is_file() and not Path(path).is_symlink(),
            'expected a regular non-symlink file: ' + str(path))


def verify_pair(binary, sidecar, expected_sha, cache=None):
    for path in (binary, sidecar):
        require_regular(path)
    metadata = read(sidecar)
    require(sha(binary) == metadata['binary_sha256'] == expected_sha,
            'binary/release identity differs')
    cache = Path(cache or metadata['build_cache'])
    require_regular(cache)
    require(sha(cache) == metadata['build_cache_sha256'],
            'release real build cache differs')
    return metadata


def verify_seal(directory, name='sealed-files.json'):
    directory = Path(directory).resolve(strict=True)
    seal = read(directory / name)
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*')
              if p.is_file() and p != directory / name}
    require(actual == set(seal), 'sealed file set differs: ' + str(directory))
    for relative, digest in seal.items():
        path = (directory / relative).resolve(strict=True)
        require(path.is_relative_to(directory) and sha(path) == digest,
                'sealed file differs: ' + relative)
    return identity(directory / name)


def seal(directory, name='sealed-files.json'):
    save(directory / name, {str(p.relative_to(directory)): sha(p)
         for p in sorted(directory.rglob('*')) if p.is_file()})
    return verify_seal(directory, name)


def identities(value):
    if isinstance(value, dict):
        if {'path', 'sha256', 'bytes'} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from identities(child)
    elif isinstance(value, list):
        for child in value:
            yield from identities(child)


def accepted_inputs(manifest_path, audit_path):
    build.verify(manifest_path, check_workspace=True, check_default=True)
    manifest, audit = read(manifest_path), read(audit_path)
    require(audit.get('schema') == 1 and
            audit.get('status') == 'PASS_BOUNDED_RELIABILITY' and
            audit.get('runtime_accepted') is True and audit.get('errors') == [],
            'independent runtime acceptance is missing')
    require(set(audit['stages']) == set(STAGES) and
            all(row['status'] == 'PASS' for row in audit['stages'].values()),
            'all six candidate stages must pass')
    require(all(audit['budget'][k] == v for k, v in
                {'model_starts': 6, 'http_requests': 9, 'preparations': 7}.items()),
            'accepted candidate attempt budget differs')
    require(audit['candidate'] == manifest['candidate'] and
            audit['build_manifest'] == identity(manifest_path) and
            audit['source_manifest'] == manifest['source_manifest'] and
            audit['default'] == manifest['default_pair'],
            'audit/build/source/default binding differs')
    require(manifest['default_sha256'] == OLD_SHA and
            manifest['default_pair']['binary']['sha256'] == OLD_SHA and
            manifest['candidate_sha256'] != OLD_SHA,
            'unexpected previous/candidate identity')
    for record in (*manifest['default_pair'].values(), audit['candidate'],
                   audit['build_manifest'], audit['source_manifest']):
        check_identity(record)
    records = list(identities(audit.get('evidence_inputs', {})))
    require(records, 'audit evidence identities missing')
    bound = {}
    for record in records:
        bound[str(check_identity(record))] = record
    for stage in STAGES:
        record = verify_seal(BASE / stage)
        require(bound.get(record['path']) == record,
                'stage seal is not bound to the accepted audit: ' + stage)
    failures = audit.get('retained_failures')
    require(isinstance(failures, list) and len(failures) == 1,
            'exactly one retained boundary preparation failure is required')
    failed = failures[0]
    require(failed['name'] == 'boundary-v1' and
            failed['status'] == 'PREPARATION_FAILED' and
            failed['runtime_accepted'] is False and
            failed['candidate_sha256'] == manifest['candidate_sha256'] and
            failed['model_starts'] == failed['http_requests'] == 0 and
            failed['preparations'] == 1,
            'retained failure identity or zero-load budget differs')
    old_seal = verify_seal(BASE / 'boundary-v1')
    old_audit = identity(BASE / 'audit-boundary-v1.json')
    require(failed['sealed_manifest'] == bound.get(old_seal['path']) == old_seal
            and failed['audit'] == bound.get(old_audit['path']) == old_audit,
            'retained failure seal/audit is not bound to acceptance')
    prior = read(old_audit['path'])
    prior_stage = prior['stages']['boundary-v1']
    require(prior['runtime_accepted'] is False and not prior['errors'] and
            prior['candidate_sha256'] == manifest['candidate_sha256'] and
            prior_stage['runtime_accepted'] is False and
            prior_stage['model_starts'] == prior_stage['http_requests'] == 0 and
            prior_stage['preparations'] == 1,
            'independent retained failure audit differs')
    binary, sidecar = destinations()
    verify_pair(binary, sidecar, OLD_SHA)
    require(identity(binary) == manifest['default_pair']['binary'] and
            identity(sidecar) == manifest['default_pair']['sidecar'],
            'current default differs from accepted previous pair')
    return manifest, audit


def validate_locations(output, package):
    output, package = Path(output).resolve(), Path(package).resolve()
    require(output != BASE and output.is_relative_to(BASE),
            'deployment output must be a new child of the stage BASE')
    releases = ROOT / '.q4t-work/releases'
    require(package != releases and package.is_relative_to(releases),
            'release package must be a child of .q4t-work/releases')
    return output, package


def preflight(manifest_path, audit_path, output, package):
    output, package = validate_locations(output, package)
    require(not output.exists() and not package.exists(),
            'deployment output/package already exists; no implicit retry')
    require(not (BASE / 'deployment-attempt.json').exists(),
            'deployment attempt already recorded; no additional model budget')
    require(DECISION.is_file(), 'formal delivery decision record missing')
    manifest, audit = accepted_inputs(manifest_path, audit_path)
    attempt = Path(manifest_path).parent / 'attempt.json'
    provenance = read(attempt)
    require(re.fullmatch('[0-9a-f]{40}', provenance['source_commit']) and
            provenance['default_pair'] == manifest['default_pair'],
            'build-time source revision record differs')
    return dict(status='READY', build_manifest=identity(manifest_path),
                build_attempt=identity(attempt),
                audit=identity(audit_path), candidate=manifest['candidate'],
                previous=manifest['default_pair'], output=str(output),
                package=str(package), deployment_model_limit=2,
                deployment_http_limit=2, total_model_limit=8, total_http_limit=11,
                atomicity=ATOMICITY, performance_acceptance=False,
                candidate_budget=audit['budget'])


def copy_checked(record, target):
    source = check_identity(record)
    with Path(target).open('xb') as destination, source.open('rb') as original:
        shutil.copyfileobj(original, destination)
        destination.flush()
        os.fsync(destination.fileno())
    shutil.copystat(source, target)
    require(sha(target) == record['sha256'], 'copy identity differs')
    check_identity(record)
    return identity(target)


def prepare_package(context):
    """Freeze old and new files completely before any default mutation."""
    output, package = Path(context['output']), Path(context['package'])
    manifest, _ = accepted_inputs(context['build_manifest']['path'],
                                  context['audit']['path'])
    check_identity(context['build_manifest'])
    check_identity(context['audit'])
    check_identity(context['build_attempt'])
    previous = output / 'previous'
    previous.mkdir()
    old = manifest['default_pair']
    for key, name in (('binary', 'q4t'), ('sidecar', 'q4t.release.json'),
                      ('build_cache', 'CMakeCache.txt')):
        copy_checked(old[key], previous / name)
    verify_pair(previous / 'q4t', previous / 'q4t.release.json', OLD_SHA,
                previous / 'CMakeCache.txt')
    previous_seal = seal(previous, 'manifest.json')
    package.mkdir(parents=True, exist_ok=False)
    copy_checked(manifest['candidate'], package / 'q4t')
    copy_checked(manifest['real_build_cache'], package / 'CMakeCache.txt')
    copy_checked(manifest['source_manifest'], package / 'source-inputs.json')
    copy_checked(context['build_manifest'], package / 'build.json')
    copy_checked(context['audit'], package / 'audit.json')
    copy_checked(context['build_attempt'], package / 'build-attempt.json')
    copy_checked(identity(DECISION), package / 'acceptance-decision.md')
    metadata = dict(version='startup-delivery-20261021',
        binary_sha256=manifest['candidate_sha256'],
        source_commit=read(package / 'build-attempt.json')['source_commit'],
        source_commit_record=str(package / 'build-attempt.json'),
        source_commit_record_sha256=sha(package / 'build-attempt.json'),
        build_cache=str(package / 'CMakeCache.txt'),
        build_cache_sha256=sha(package / 'CMakeCache.txt'),
        tested_source_manifest=str(package / 'source-inputs.json'),
        tested_source_manifest_sha256=sha(package / 'source-inputs.json'),
        source_note='actual compiler inputs match the frozen source manifest',
        build_manifest=str(package / 'build.json'),
        build_manifest_sha256=sha(package / 'build.json'),
        acceptance='ENGINEERING_ACCEPTED_RELIABILITY',
        acceptance_audit=str(package / 'audit.json'),
        acceptance_audit_sha256=sha(package / 'audit.json'),
        acceptance_decision=str(DECISION),
        acceptance_snapshot=str(package / 'acceptance-decision.md'),
        acceptance_snapshot_sha256=sha(package / 'acceptance-decision.md'),
        previous_binary=str(previous / 'q4t'),
        previous_release=str(previous / 'q4t.release.json'),
        scope='default executable; bounded reliability; no performance claim; '
              'no persistent service', atomicity=ATOMICITY)
    save(package / 'q4t.release.json', metadata)
    verify_pair(package / 'q4t', package / 'q4t.release.json',
                manifest['candidate_sha256'])
    package_seal = seal(package, 'manifest.json')
    plan = dict(schema=1, previous_sha256=OLD_SHA,
        candidate_sha256=manifest['candidate_sha256'],
        previous_manifest=previous_seal, package_manifest=package_seal,
        previous=old, candidate=identity(package / 'q4t'),
        candidate_release=identity(package / 'q4t.release.json'),
        destinations=[str(p) for p in destinations()], atomicity=ATOMICITY)
    save(output / 'installation-plan.json', plan)
    fsync_directory(previous)
    fsync_directory(package)
    fsync_directory(output)
    return plan


def read_plan(output):
    output = Path(output).resolve(strict=True)
    plan = read(output / 'installation-plan.json')
    require(plan['schema'] == 1 and plan['previous_sha256'] == OLD_SHA and
            plan['destinations'] == [str(p) for p in destinations()],
            'rollback destination/previous identity differs')
    previous = output / 'previous'
    package = Path(plan['package_manifest']['path']).parent
    validate_locations(output, package)
    require(plan['previous_manifest']['path'] == str(previous / 'manifest.json')
            and plan['candidate']['path'] == str(package / 'q4t') and
            plan['candidate_release']['path'] == str(package / 'q4t.release.json'),
            'installation package paths differ')
    for directory, record in ((previous, plan['previous_manifest']),
                              (package, plan['package_manifest'])):
        check_identity(record)
        require(verify_seal(directory, 'manifest.json') == record,
                'installation package seal differs')
    verify_pair(previous / 'q4t', previous / 'q4t.release.json', OLD_SHA,
                previous / 'CMakeCache.txt')
    verify_pair(package / 'q4t', package / 'q4t.release.json',
                plan['candidate_sha256'])
    check_identity(plan['candidate'])
    check_identity(plan['candidate_release'])
    require(sha(previous / 'q4t.release.json') ==
            plan['previous']['sidecar']['sha256'], 'original sidecar differs')
    return plan


def replace_checked(source, destination, expected_current):
    """Same-directory rename; detects unknown pairs, not a multi-file CAS."""
    source, destination = Path(source), Path(destination)
    require_regular(destination)
    require(sha(destination) == expected_current,
            'unknown current identity; refusing replace: ' + str(destination))
    record = identity(source)
    descriptor, temporary = tempfile.mkstemp(
        prefix=destination.name + '.startup-new-', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        require(sha(temporary) == record['sha256'] and
                sha(destination) == expected_current,
                'identity changed during replacement preparation')
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
        require(sha(destination) == record['sha256'], 'replacement differs')
    finally:
        temporary.unlink(missing_ok=True)


def install(output):
    plan = read_plan(output)
    binary, sidecar = destinations()
    for key, path in (('binary', binary), ('sidecar', sidecar)):
        require(identity(path) == plan['previous'][key],
                'default changed after rollback package was prepared')
    verify_pair(binary, sidecar, OLD_SHA)
    save(Path(output) / 'installation-started.json', dict(**stamp(),
         installation_plan=identity(Path(output) / 'installation-plan.json')))
    replace_checked(plan['candidate']['path'], binary, OLD_SHA)
    replace_checked(plan['candidate_release']['path'], sidecar,
                    plan['previous']['sidecar']['sha256'])
    verify_published_pair(output)


def verify_published_pair(output):
    plan = read_plan(output)
    binary, sidecar = destinations()
    verify_pair(binary, sidecar, plan['candidate_sha256'])
    require(sha(sidecar) == plan['candidate_release']['sha256'],
            'installed release metadata differs')
    return dict(binary=identity(binary), sidecar=identity(sidecar))


def restore_previous_cache(output, plan):
    """Restore a missing original cache only; never overwrite unknown data."""
    record = plan['previous']['build_cache']
    path = Path(record['path'])
    if path.exists():
        check_identity(record)
        return
    require(path.is_relative_to(ROOT / '.q4t-work/releases') and
            not os.path.lexists(path), 'unsafe missing previous cache path')
    source = Path(output) / 'previous/CMakeCache.txt'
    require(sha(source) == record['sha256'], 'previous cache backup differs')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Link an independent temporary copy exclusively. Linking the rollback
    # backup itself would let a later cache edit corrupt our recovery package.
    descriptor, temporary = tempfile.mkstemp(prefix='cache-restore-',
                                              dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        require(sha(temporary) == record['sha256'], 'restored cache copy differs')
        os.link(temporary, path)  # Exclusive: never clobber a concurrent file.
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    check_identity(record)


def rollback(output):
    """Requires the caller's exclusive lease; does not consult mutable source."""
    output = Path(output)
    plan = read_plan(output)
    binary, sidecar = destinations()
    for path in (binary, sidecar):
        require_regular(path)
    current_binary, current_release = sha(binary), sha(sidecar)
    require(current_binary in (OLD_SHA, plan['candidate_sha256']) and
            current_release in (plan['previous']['sidecar']['sha256'],
                                plan['candidate_release']['sha256']),
            'unknown current pair; refuse rollback')
    restore_previous_cache(output, plan)
    previous = output / 'previous'
    replace_checked(previous / 'q4t', binary, current_binary)
    replace_checked(previous / 'q4t.release.json', sidecar, current_release)
    verify_pair(binary, sidecar, OLD_SHA)
    require(sha(sidecar) == plan['previous']['sidecar']['sha256'],
            'restored sidecar differs')
    return dict(status='ROLLED_BACK', binary=identity(binary),
                sidecar=identity(sidecar), restored_from=identity(
                    output / 'previous/manifest.json'), atomicity=ATOMICITY)


def recovery_path():
    lock = runner().common_lock()
    return lock.with_name(lock.name + '.recovery-required')


def create_recovery(output):
    save(recovery_path(), dict(kind='startup_delivery', output=str(output),
         installation_plan=identity(output / 'installation-plan.json')))
    fsync_directory(recovery_path().parent)


def clear_recovery(output):
    path = recovery_path()
    if not path.exists():
        return
    record = read(path)
    require(record.get('kind') == 'startup_delivery' and
            record.get('output') == str(output) and
            record['installation_plan'] == identity(
                output / 'installation-plan.json'), 'foreign recovery marker')
    path.unlink()
    fsync_directory(path.parent)


def require_guard(output):
    lock_path = runner().common_lock()
    with lock_path.open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise RuntimeError('deployment worker requires the GPU lease')
        held = json.load(lock)
    lease_output = str(output / 'lease')
    require(held.get('state') == 'held' and held.get('output') == lease_output,
            'deployment worker has another lease')
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            break
        proc = Path('/proc') / str(pid)
        argv = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if str(ROOT / 'tools/evalscope/gpu_exclusive.py') in argv:
            require('--output' in argv and
                    argv[argv.index('--output') + 1] == lease_output,
                    'guard ancestry does not match deployment')
            return
        pid = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[1])
    raise RuntimeError('no matching deployment guard ancestor')


def verify_tools(output):
    for name, digest in read(output / 'sources.json').items():
        require(sha(ROOT / name) == digest and
                sha(output / 'source' / name) == digest,
                'deployment tool changed: ' + name)


def freeze_tools(output):
    sources = set()
    for folder in (HERE, ROOT / 'tools/evalscope',
                   ROOT / 'tools/verify/warm_reclaim',
                   ROOT / 'tools/verify/startup_cache',
                   ROOT / 'tools/verify/vmm_compare',
                   ROOT / 'tools/verify/startup_plan'):
        sources.update(folder.glob('*.py'))
    hashes = {}
    for path in sorted(sources):
        relative = path.relative_to(ROOT)
        target = output / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(identity(path), target)
        hashes[str(relative)] = sha(path)
    save(output / 'sources.json', hashes)


def health_command(output, stage):
    return [sys.executable, '-B', str(Path(__file__).resolve()),
            '--output', str(output), '--health-worker', stage]


def process_identity(pid):
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]))


def process_alive(record):
    try:
        return process_identity(record['pid']) == record
    except FileNotFoundError:
        return False


def verify_health(output, stage):
    out = output / stage
    http = out / 'run/normal/http'
    result, rows = read(http / 'exit.json'), read(http / 'results.json')
    require(result['server'] == 0 and result['completed'] == 1 and
            result['http_output_checks_passed'] is True and
            result['failure'] is None and result['cleanup_failure'] is None and
            not result['logging_errors'], 'deployment HTTP/shutdown failed')
    require(len(rows) == 1 and rows[0]['text'] == '710003' and
            rows[0]['actual_input'] == 1024 and rows[0]['actual_output'] == 7 and
            rows[0]['finish'] == ['stop'], 'deployment exact output differs')
    events = [json.loads(line) for line in (http / 'events.jsonl').read_text().splitlines()]
    require(sum(row['event'] == 'server_started' for row in events) == 1 and
            sum(row['event'] == 'client_before' for row in events) == 1,
            'deployment load/HTTP budget differs')
    cfg = runner().configuration(stage)
    require(cfg['real_default'] and cfg['cache'] == 'inherited' and
            cfg['memory_max'] is None and cfg['requests'] == 1 and
            cfg['mtp'] == (stage == HEALTH[0]), 'deployment mode differs')
    command = read(http / 'server-command.json')
    require(command['argv'] == runner().server_command(
                stage, out / 'run', binary=destinations()[0]) and
            command['effective_q4t_environment'] == {},
            'deployment must use actual default parameters')
    gate = read(out / 'run/cache-gate.json')
    require(gate['mode'] == 'inherited' and gate['naturally_inherited'] and
            gate['preparation_passes'] == gate['repair_passes'] == 0 and
            gate['temporary_locks_used'] is False,
            'deployment changed its inherited cache')
    log = (http / 'server.log').read_text()
    capacity = runner().capacity_evidence(log, stage)
    summary = runner().reservation_evidence(log, stage, finalized=True)
    verify_published_pair(output)
    return dict(status='PASS', stage=stage, model_starts=1, http_requests=1,
                capacity=capacity, reservation=summary, cache='inherited',
                lease_output=str(output / 'lease'))


def supervise_health(output, stage):
    require(stage in HEALTH, 'unknown deployment health stage')
    out = output / stage
    out.mkdir(exist_ok=False)
    (out / 'run').mkdir()
    state = dict(status='RUNNING', stage=stage, started=stamp(),
                 lease_output=str(output / 'lease'),
                 collector_profile='resources_nvml_production_summary',
                 kernel_timeline='NOT_COLLECTED', model_limit=1, http_limit=1)
    collector = child = None
    logs, errors = [], []
    stop = out / 'stop-resources'
    try:
        verify_tools(output)
        verify_published_pair(output)
        resource_command = ['sudo', '-n', sys.executable, '-B', str(
            ROOT / 'tools/verify/warm_reclaim/resources.py'), '--output',
            str(out / 'resources'), '--stop-file', str(stop), '--max-seconds',
            '360', '--binary', str(destinations()[0]), '--model-dir',
            str(runner().MODEL)]
        save(out / 'resources-command.json', resource_command)
        log = (out / 'resources-driver.log').open('x')
        logs.append(log)
        collector = subprocess.Popen(resource_command, stdout=log,
                                     stderr=subprocess.STDOUT)
        runner().wait_ready(collector, out / 'resources')
        observer = read(out / 'resources/ready.json')
        save(out / 'resources-identity.json', process_identity(observer['pid']))
        command = health_command(output, stage)
        save(out / 'command.json', command)
        log = (out / 'http-driver.log').open('x')
        logs.append(log)
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 300
        while child.poll() is None:
            require(collector.poll() is None, 'deployment resource collector exited')
            require(time.monotonic() < deadline, 'deployment health deadline')
            time.sleep(.1)
        state['http_returncode'] = child.returncode
        require(child.returncode == 0, 'deployment HTTP worker failed')
    except BaseException as error:
        errors.append(repr(error))
    finally:
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                    errors.append('HTTP worker exceeded cleanup grace')
        except BaseException as error:
            errors.append('HTTP cleanup: ' + repr(error))
        for name, action in (('scope_cleanup', runner().cleanup_scope),
                             ('helper_cleanup', runner().helper_cleanup_evidence)):
            try:
                state[name] = action(out)
            except BaseException as error:
                errors.append(name + ': ' + repr(error))
        try:
            if collector is not None:
                stop.touch()
                rc = collector.wait(timeout=15)
                observed = read(out / 'resources/exit.json')
                state['resources'] = dict(returncode=rc, result=observed)
                require(rc == 0 and observed['status'] == 'COMPLETED' and
                        not observed['failures'] and not observed['truncated'],
                        'deployment resource observations failed')
        except BaseException as error:
            errors.append('resource cleanup: ' + repr(error))
        for log in logs:
            try:
                log.close()
            except BaseException as error:
                errors.append('log close: ' + repr(error))
    if not errors:
        try:
            state['health'] = verify_health(output, stage)
            require(not state['helper_cleanup']['started'],
                    'deployment unexpectedly used a lock helper')
        except BaseException as error:
            errors.append(repr(error))
    state.update(status='LOCAL_HEALTH_COMPLETED' if not errors else 'FAILED',
                 errors=errors, finished=stamp())
    save(out / 'state.json', state)
    sealed = seal(out)
    require(not errors, 'deployment health failed: ' + '; '.join(errors))
    return dict(stage=stage, health=state['health'], sealed_files=sealed)


def worker(output):
    require_guard(output)
    verify_tools(output)
    context = read(output / 'context.json')
    prepare_package(context)
    create_recovery(output)
    install(output)
    save(output / 'installed.json', verify_published_pair(output))
    health = []
    for stage in HEALTH:
        health.append(supervise_health(output, stage))
    save(output / 'worker-result.json', dict(status='HEALTH_COMPLETED',
         health=health, loads=2, requests=2, performance_acceptance=False))
    return 0


def guard_result(output):
    status = read(output / 'lease/exit.json')
    require(status['status'] == 'COMPLETED' and status['child_returncode'] == 0
            and status['compute_backend'] == 'nvml' and
            status['poll_interval_seconds'] == 1.0 and
            status['external_lease'] is True and 'cleanup_error' not in status,
            'deployment NVML guard did not complete')
    queries = [json.loads(line) for line in
               (output / 'lease/nvml-queries.jsonl').read_text().splitlines()]
    require(queries and all(row['status'] == 'OK' for row in queries),
            'deployment NVML query failed')
    return dict(exit=identity(output / 'lease/exit.json'), query_count=len(queries),
                poll_seconds=1.0, query_timeout_seconds=5.0)


def cleanup_after_guard(output):
    result = dict(errors=[], scopes=[], collectors=[])
    for stage in HEALTH:
        out = output / stage
        if not out.exists():
            continue
        try:
            # Keep later recovery evidence outside the sealed health stage.
            spec = out / 'run/normal/http/isolation/spec.json'
            if spec.exists():
                launch = read(spec.with_name('launch.json'))
                unit = launch['unit']
                before = runner().scope_properties(unit)
                if before.get('LoadState') != 'not-found':
                    runner().stop_owned_scope(unit, read(spec)['token'])
                after = runner().scope_properties(unit)
                result['scopes'].append(dict(stage=stage, unit=unit,
                                             before=before, after=after))
                require(after.get('LoadState') == 'not-found',
                        'deployment scope remains after guard cleanup')
            runner().helper_cleanup_evidence(out)
            if (out / 'resources').exists():
                stop = out / 'stop-resources'
                if not stop.exists():
                    require(not (out / 'sealed-files.json').exists(),
                            'sealed resource cleanup stop evidence missing')
                    stop.touch(exist_ok=False)
                require((out / 'resources/exit.json').exists(),
                        'resource collector cleanup is not confirmed')
                who = read(out / 'resources-identity.json')
                require(not process_alive(who), 'resource observer remains')
                result['collectors'].append(dict(stage=stage, identity=who,
                                                 exited=True))
        except BaseException as error:
            result['errors'].append(stage + ': ' + repr(error))
    return result


def deploy(manifest_path, audit_path, output, package):
    output, package = validate_locations(output, package)
    lock_path = runner().common_lock()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not os.path.lexists(recovery_path()), 'unfinished GPU recovery')
        context = preflight(manifest_path, audit_path, output, package)
        output.mkdir(parents=True, exist_ok=False)
        save(output / 'context.json', context)
        freeze_tools(output)
        save(BASE / 'deployment-attempt.json', dict(output=str(output),
             candidate=context['candidate'], model_limit=2, http_limit=2,
             started=stamp(), no_retry=True))
        command = [sys.executable, '-B', str(ROOT / 'tools/evalscope/gpu_exclusive.py'),
                   '--output', str(output / 'lease'), '--compute-backend', 'nvml',
                   '--lease-fd', str(lock.fileno()), '--', sys.executable, '-B',
                   str(Path(__file__).resolve()), '--output', str(output), '--worker']
        save(output / 'guard-command.json', command)
        state = dict(status='RUNNING', started=stamp(), atomicity=ATOMICITY,
                     performance_acceptance=False, persistent_service=False)
        guard = None
        try:
            with (output / 'guard.log').open('x') as log:
                guard = subprocess.Popen(command, stdout=log,
                    stderr=subprocess.STDOUT, pass_fds=(lock.fileno(),),
                    start_new_session=True)
                rc = guard.wait(timeout=700)
            require(rc == 0, 'deployment guard failed')
            state['guard'] = guard_result(output)
            result = read(output / 'worker-result.json')
            require(result['status'] == 'HEALTH_COMPLETED' and
                    result['loads'] == result['requests'] == 2 and
                    [row['stage'] for row in result['health']] == list(HEALTH),
                    'deployment health matrix incomplete')
            for row in result['health']:
                require(verify_seal(output / row['stage']) == row['sealed_files'],
                        'health seal changed')
            state['health'] = result['health']
            state['installed'] = verify_published_pair(output)
            state.update(status='DEPLOYED_HEALTH_CHECKED', loads=2, requests=2)
        except BaseException as error:
            state.update(status='FAILED', error=repr(error))
        finally:
            try:
                if guard is not None and guard.poll() is None:
                    guard.terminate()
                    try:
                        guard.wait(timeout=65)
                    except subprocess.TimeoutExpired:
                        guard.kill()
                        guard.wait(timeout=5)
                        state['forced_guard_kill'] = True
            except BaseException as error:
                state['guard_cleanup_error'] = repr(error)
            if guard is not None:
                try:
                    final_guard = read(output / 'lease/exit.json')
                    state['guard_exit'] = final_guard
                    require('cleanup_error' not in final_guard and
                            'finished' in final_guard,
                            'guard-owned descendant cleanup is unconfirmed')
                except BaseException as error:
                    state['guard_cleanup_error'] = repr(error)
            state['post_guard_cleanup'] = cleanup_after_guard(output)
            cleanup_errors = state['post_guard_cleanup']['errors']
            if cleanup_errors or state.get('guard_cleanup_error') or state.get('forced_guard_kill'):
                state.update(status='RECOVERY_REQUIRED', cleanup_errors=cleanup_errors)
            if state['status'] != 'DEPLOYED_HEALTH_CHECKED':
                if (output / 'installation-plan.json').exists():
                    try:
                        state['rollback'] = rollback(output)
                    except BaseException as error:
                        state.update(status='RECOVERY_REQUIRED', rollback_error=repr(error))
            if state['status'] != 'RECOVERY_REQUIRED':
                try:
                    clear_recovery(output)
                except BaseException as error:
                    state.update(status='RECOVERY_REQUIRED', recovery_error=repr(error))
            if state['status'] == 'RECOVERY_REQUIRED' and not os.path.lexists(recovery_path()):
                save(recovery_path(), dict(kind='startup_delivery',
                     output=str(output), state=state))
            state['finished'] = stamp()
            save(output / 'deployment.json', state)
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(dict(state='recovery_required' if
                state['status'] == 'RECOVERY_REQUIRED' else 'released',
                output=str(output))) + '\n')
            lock.flush()
        seal(output)
        print(json.dumps(state))
        return 0 if state['status'] == 'DEPLOYED_HEALTH_CHECKED' else 1


def rollback_entry(output):
    output = Path(output).resolve(strict=True)
    lock_path = runner().common_lock()
    with lock_path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = output.with_name(output.name + '-rollback-' + str(time.time_ns()))
        receipt.mkdir()
        state = dict(status='FAILED', started=stamp())
        try:
            state['restoration'] = rollback(output)
            state['post_guard_cleanup'] = cleanup_after_guard(output)
            require(not state['post_guard_cleanup']['errors'],
                    'owned cleanup unconfirmed')
            clear_recovery(output)
            state['status'] = 'ROLLED_BACK'
        except BaseException as error:
            state['error'] = repr(error)
        state['finished'] = stamp()
        save(receipt / 'result.json', state)
        seal(receipt)
        print(json.dumps(state))
        return 0 if state['status'] == 'ROLLED_BACK' else 1


def main():
    def interrupted(signum, _frame):
        raise KeyboardInterrupt('deployment interrupted by ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-manifest', type=Path,
                        default=BASE / 'candidate-v1/build.json')
    parser.add_argument('--audit', type=Path, default=BASE / 'audit-final-v1.json')
    parser.add_argument('--output', type=Path, default=BASE / 'deploy-v1')
    parser.add_argument('--package', type=Path,
                        default=ROOT / '.q4t-work/releases/startup-delivery-20261021')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight-only', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    mode.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--health-worker', choices=HEALTH, help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.rollback:
        return rollback_entry(output)
    if args.worker or args.health_worker:
        require_guard(output)
        verify_tools(output)
        if args.health_worker:
            return runner().http_worker(args.health_worker,
                run=output / args.health_worker / 'run', binary=destinations()[0],
                verify_callback=lambda: verify_published_pair(output))
        return worker(output)
    if args.preflight_only:
        print(json.dumps(preflight(args.build_manifest, args.audit,
                                   output, args.package)))
        return 0
    return deploy(args.build_manifest, args.audit, output, args.package)


if __name__ == '__main__':
    raise SystemExit(main())
