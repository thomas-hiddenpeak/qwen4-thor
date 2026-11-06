"""Conditional QSA install, exact previous-pair rollback and bounded health.

Preparation requires the completed H/N/C engineering decision and its verified
archive. This tool does not grant acceptance or migrate old managed packages.
"""
import argparse
import copy
import fcntl
import importlib.util
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('qsa_delivery_health',
    Path(__file__).with_name('adoption_health.py'))
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
a = health.a
d = a.module('qsa_previous_delivery', 'tools/mtp_prefill_scoring/deliver.py')
d.configure(a.ROOT, a.BASE / 'acceptance')
d.BASE, d.PACKAGE, d.OUT = a.BASE, a.BASE / 'release', a.BASE / 'delivery'
d.health = health
h, pkg = d.h, d.pkg
PACKAGE, OUT = d.PACKAGE, d.OUT
PROTOCOL = '55da359517f9f4a2109afc6e242bd6d74086eee44f239c5adf6aadb75188500d'


def prepare():
    gate = a.read(a.BASE / 'acceptance/decision.json')
    h.require(gate['status'] == 'PASS_QSA_BOUNDED_ENGINEERING_ACCEPTANCE' and
              gate['passed'] and gate['candidate_sha256'] == a.CANDIDATE,
              'qualified exact candidate required')
    for stage in ('http', 'contracts', 'cache'):
        path = a.BASE / stage / 'review.json'
        h.require(h.sha(path) == gate['reviews'][stage], 'bound completed review')
    archive = a.read(a.BASE / 'acceptance/archive-receipt.json')
    h.require(archive['passed'] and h.sha(archive['archive']) == archive['sha256'],
              'verified acceptance archive')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=a.ROOT),
              'commit delivery tool before freezing')
    h.require(h.sha(a.ORIGINAL / 'build/q4t.release.json') == a.SIDECAR,
              'default sidecar changed')
    old = h.verify_pair(a.ORIGINAL / 'build/q4t', a.ORIGINAL / 'build/q4t.release.json',
                        a.DEFAULT)
    hp, cp = a.read(a.PLAN), a.read(a.BASE / 'cache/plan.json')
    a.legacy.verify_files(hp)
    a.legacy.verify_files(cp)
    runtime = a.read(a.PLAN.parent / 'frozen/candidate-runtime-source.json')
    h.require(all(h.sha(a.ROOT / p) == v for p, v in runtime.items()),
              'tested runtime source')
    PACKAGE.mkdir()
    for name in ('previous', 'candidate', 'tested-source'):
        (PACKAGE / name).mkdir()
    for name in ('q4t', 'q4t.release.json'):
        h.copy_checked(h.identity(a.ORIGINAL / 'build' / name), PACKAGE / 'previous' / name)
    for key, name in [('build_cache', 'CMakeCache.txt'),
                      ('acceptance_decision', 'acceptance.json'),
                      ('tested_source_manifest', 'runtime-source.json')]:
        h.copy_checked(h.identity(old[key]), PACKAGE / 'previous' / name)
    shutil.copytree(old['release_declarations']['directory'], PACKAGE / 'previous/declarations')
    h.copy_checked(old['operations_rules'], PACKAGE / 'previous/q4t.rules.yml')
    h.seal(PACKAGE / 'previous')
    h.copy_checked(h.identity(hp['candidate']), PACKAGE / 'candidate/q4t')
    engine = a.ROOT / '.q4t-work/qsa-softmax-repair-20261106/engine'
    h.copy_checked(h.identity(engine / 'CMakeCache.txt'), PACKAGE / 'candidate/CMakeCache.txt')
    h.save(PACKAGE / 'candidate/runtime-source.json', runtime)
    for name, digest in runtime.items():
        target = PACKAGE / 'tested-source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(a.ROOT / name), target)
        h.require(h.sha(target) == digest, 'packaged tested source')
    for name in ('decision.json', 'archive-receipt.json'):
        h.copy_checked(h.identity(a.BASE / 'acceptance' / name), PACKAGE / name)
    shutil.copytree(hp['artifact'], PACKAGE / 'artifact')
    for path in (PACKAGE / 'artifact').rglob('*'):
        if path.is_file():
            original = Path(hp['artifact']) / path.relative_to(PACKAGE / 'artifact')
            h.require(h.sha(path) == hp['files'][str(original)], 'subset copy')
    h.copy_checked(old['operations_rules'], PACKAGE / 'candidate/q4t.rules.yml')
    plan = copy.deepcopy(cp)
    plan.update(stage='QSA_SOFTMAX_ADOPTION_HEALTH', output=str(OUT / 'run'),
        candidate=str(a.ORIGINAL / 'build/q4t'), candidate_sha256=a.CANDIDATE,
        parent_sha256=a.DEFAULT, source_commit=hp['runtime_commit'],
        protocol_sha256=PROTOCOL, artifact=str(PACKAGE / 'artifact'),
        http_requests=6, http_loads=2, stage_timeout_seconds=840,
        approved_total_wall_seconds=900, required_gate=str(PACKAGE / 'decision.json'),
        telemetry_sources=a.telemetry.discover_sources(), steps=[])
    for index, head in enumerate(('full', 'subset')):
        name = 'installed-' + head
        plan['steps'].append(dict(name=name, kind='lifecycle', head=head,
            layout='agent', suffix=True, binary=plan['candidate'], http_requests=3,
            http_loads=1, timeout=420, command=[sys.executable, '-B',
                str(Path(__file__).with_name('adoption_health.py')),
                '--plan', str(OUT / 'plan.json'), '--step', name]))
    # The new ELF and sidecar are deliberately installed during this stage.
    # The original pair remains sealed in previous/, not as mutable inputs.
    plan['files'].pop(str(a.ORIGINAL / 'build/q4t.release.json'), None)
    plan['files'][plan['candidate']] = a.CANDIDATE
    for path in PACKAGE.rglob('*'):
        if path.is_file():
            plan['files'][str(path)] = h.sha(path)
    for path in (Path(__file__).resolve(), Path(health.__file__).resolve()):
        plan['files'][str(path)] = h.sha(path)
    plan['task_deadline_unix'] = time.time() + 840
    health.validate(plan)
    OUT.mkdir()
    h.save(OUT / 'plan.json', plan)
    h.save(PACKAGE / 'health-plan.json', plan)
    print('PREPARED conditional delivery: 2 services / 6 HTTP / 900 seconds')


def release(plan):
    metadata = a.read(PACKAGE / 'previous/q4t.release.json')
    inherited = {key: copy.deepcopy(metadata[key]) for key in
        ('acceptance', 'acceptance_decision', 'acceptance_decision_sha256',
         'chat_protocol', 'scope') if key in metadata}
    inherited['release'] = h.identity(PACKAGE / 'previous/q4t.release.json')
    for key in ('tool_acceptance', 'tool_acceptance_sha256', 'deployment_completion'):
        metadata.pop(key, None)
    declarations = PACKAGE / 'candidate/declarations'
    declarations.mkdir()
    for name in ('capabilities', 'configuration'):
        h.copy_checked(h.identity(OUT / 'run/installed-full' / (name + '.json')),
                       declarations / (name + '.json'))
    shutil.copytree(PACKAGE / 'previous/declarations/schemas', declarations / 'schemas')
    caps, config = a.read(declarations / 'capabilities.json'), a.read(declarations / 'configuration.json')
    protocol = metadata['chat_protocol']
    protocol['protocol_sha256'] = PROTOCOL
    for key in caps.keys() & protocol.keys():
        protocol[key] = copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path = declarations / 'schemas' / (name + '.json')
        protocol[name + '_schema'] = str(path)
        protocol[name + '_schema_sha256'] = h.sha(path)
    protocol['declaration_evidence'] = 'Installed two-head health declarations; actual H project tool loops. Old managed/task packages stay pinned.'
    protocol['sdk_interoperability_acceptance'] = dict(
        current='No new SDK qualification by QSA adoption', new_sdk_qualification=False,
        prior_release=h.identity(PACKAGE / 'previous/q4t.release.json'))
    metadata.update(version='qsa-softmax-adoption-20261106',
        binary_sha256=a.CANDIDATE, source_commit=plan['source_commit'],
        tool_source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=a.ROOT, text=True).strip(),
        delivery_tool=h.identity(Path(__file__).resolve()),
        source_note='Five tested runtime files only: layer-type validation/aliases, optional QSA timing, ordered large-T softmax. Runtime manifest is authoritative.',
        tested_source_manifest=str(PACKAGE / 'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE / 'candidate/runtime-source.json'),
        build_cache=str(PACKAGE / 'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE / 'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_QSA_SOFTMAX',
        acceptance_decision=str(PACKAGE / 'decision.json'),
        acceptance_decision_sha256=h.sha(PACKAGE / 'decision.json'),
        inherited_acceptance=inherited,
        inherited_release=h.identity(PACKAGE / 'previous/q4t.release.json'),
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        observed_configuration=config, subset_artifact=str(PACKAGE / 'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE / 'artifact/manifest.json'),
        operations_rules=h.identity(PACKAGE / 'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE / 'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', production='default text MTP k3/full head; explicit subset head',
            performance='single-stream plain/full/subset, five contexts; descriptive, no statistical noninferiority',
            correctness='independent finite kernel/rounding boundaries, 48-layer four-slot state, paired HTTP and tools, two-head caches',
            installation_health='two heads, six installed cache requests; no persistent service',
            boundaries='No long soak, new SDK, real LAN, managed/task migration or four-client performance claim. Existing packages stay pinned.'))
    metadata['release_declarations'] = dict(profile=pkg.DECLARATION_PROFILE,
        directory=str(declarations), binary_sha256=a.CANDIDATE, protocol_sha256=PROTOCOL,
        files={name: h.sha(declarations / name) for name in pkg.DECLARATION_FILES})
    pkg.verify_declarations(metadata, required=True)
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    h.verify_pair(PACKAGE / 'candidate/q4t', PACKAGE / 'candidate/q4t.release.json', a.CANDIDATE)
    h.seal(PACKAGE)


def deploy():
    a.legacy.require_guard()
    plan = a.read(OUT / 'plan.json')
    health.validate(plan)
    h.require(plan == a.read(PACKAGE / 'health-plan.json'), 'health plan changed')
    h.verify_seal(PACKAGE / 'previous')
    h.require(not d.marker().exists(), 'unfinished recovery')
    for name in ('q4t', 'q4t.release.json'):
        h.require(h.sha(a.ORIGINAL / 'build' / name) == h.sha(PACKAGE / 'previous' / name),
                  'default pair changed')
    h.save(d.marker(), dict(kind='qsa_softmax_adoption', package=str(PACKAGE),
                           plan=h.identity(OUT / 'plan.json')))
    h.fsync_directory(d.marker().parent)
    state, child = dict(status='INSTALLING', started=time.time()), None
    try:
        h.replace_checked(PACKAGE / 'candidate/q4t', a.ORIGINAL / 'build/q4t', a.DEFAULT)
        with (OUT / 'driver.log').open('x') as log:
            child = subprocess.Popen([sys.executable, '-B', str(Path(health.__file__)),
                '--plan', str(OUT / 'plan.json')], cwd=a.ROOT, env=a.legacy.clean_env(),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            code = child.wait(timeout=max(.01, plan['task_deadline_unix'] - time.time()))
        a.base.monitored_stage.require_complete_monitor(plan)
        h.require(code == 0, 'installed health command failed')
        for step in plan['steps']:
            folder = OUT / 'run' / step['name']
            health.driver.audit(folder, 3)
            h.require(a.read(folder / 'health-result.json')['passed'], 'cache health')
        release(plan)
        h.replace_checked(PACKAGE / 'candidate/q4t.release.json',
                          a.ORIGINAL / 'build/q4t.release.json', a.SIDECAR)
        h.verify_pair(a.ORIGINAL / 'build/q4t', a.ORIGINAL / 'build/q4t.release.json', a.CANDIDATE)
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        if child is not None and child.poll() is None:
            a.legacy.stop_group(child)
        state.update(status='FAILED', error=repr(error))
        try:
            d.restore()
            state['rollback'] = 'RESTORED_PREVIOUS'
        except BaseException as failure:
            state.update(status='RECOVERY_REQUIRED', rollback_error=repr(failure))
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUT / 'deployment.json', state)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned QSA delivery interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'deploy', 'finalize', 'rollback'))
    action = parser.parse_args().action
    if action == 'rollback':
        with a.legacy.gpu_exclusive.common_lock().open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            d.restore()
    elif action == 'finalize':
        d.finalize()
    else:
        globals()[action]()
