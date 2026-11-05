"""Authorized final start: no-policy JSON/SSE and exact default-pair delivery."""
import argparse
import copy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_governance import cpu, stage as prior, scoped_report
from tools.serve_client_governance import report as old_archive
from tools.serve_clients import short as s
from tools.serve_error_params.stage import a, h, Children
from tools.serve_service import package as pkg

BASE = cpu.BASE/'completion'
PACKAGE, OUTPUT = BASE/'package', BASE/'execution'
PORT = 18138
LIMITS = dict(starts=1, generation_attempts=2, controls=20, seconds=240)
CUMULATIVE = dict(starts=3, generation_attempts=24, controls=250, seconds=480)
CPU = BASE/'cpu-final'
PRIOR_ARCHIVE = ROOT/'docs/evidence/serve-client-governance-20261105'


def verify_prior():
    """Rebuild historical evidence against its preserved default, not today's.

    Only that explicit old default pair is redirected. All runtime, wire,
    source, archive and package checks remain the original independent audit.
    The current installed pair is checked separately by the delivery transaction.
    """
    expected = cpu.read(prior.PACKAGE/'plan.json')['old_hashes']
    original = a.verify_hashes
    def historical(base, values):
        if Path(base) == ROOT/'build' and values == expected:
            return original(prior.PACKAGE/'previous', values)
        return original(base, values)
    a.verify_hashes = historical
    try:
        scoped_report.bind()
        old_archive.verify()
    finally:
        a.verify_hashes = original
    return cpu.read(PRIOR_ARCHIVE/'result.json')


def validate_disabled(config):
    s.cp.observed_matches(config['client_policy'], None)
    governance = config['client_governance']
    cpu.require(governance['fair_queue'] is False and governance['policy_version'] == 1 and
                config['limits']['queue_policy'] == 'capacity_fifo_best_fit', 'no-policy discovery')


def sources():
    result = prior.source_paths()
    result['docs/SERVE_CLIENT_GOVERNANCE_COMPLETION_2026-11-05.md'] = cpu.sha(
        ROOT/'docs/SERVE_CLIENT_GOVERNANCE_COMPLETION_2026-11-05.md')
    return result


def test():
    CPU.mkdir(parents=True)
    cpu.save(CPU/'sources.json', sources())
    passed = cpu.command(CPU, 'driver', [sys.executable, '-B', '-m', 'unittest', '-v',
                                       'tools.serve_client_governance.test_completion'])
    cpu.require(sources() == cpu.read(CPU/'sources.json'), 'CPU source drift')
    cpu.save(CPU/'result.json', dict(passed=passed, models=0, http=0, gpu=0,
                                    reused_cpu=str(cpu.BASE/prior.CPU_LABEL/'result.json')))
    cpu.require(passed, 'driver failed; preserve attempt')


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(), 'preserve frozen attempts')
    cpu.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit reviewed stage first')
    cpu.require(cpu.read(CPU/'result.json')['passed'], 'CPU driver gate')
    a.verify_hashes(ROOT, cpu.read(CPU/'sources.json'))
    reused = verify_prior()
    old_plan = prior.check()
    a.verify_hashes(ROOT/'build', old_plan['old_hashes'])
    cpu.require(reused['models'] == 2 and reused['generation_attempts'] == 16 and
                reused['cumulative_lease_seconds']+LIMITS['seconds'] < CUMULATIVE['seconds'], 'cumulative budget')
    cpu.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending foreign recovery')
    PACKAGE.mkdir()
    for name in ('previous', 'candidate'):
        shutil.copytree(prior.PACKAGE/name, PACKAGE/name)
    for name in ('numerical-reuse.json', 'reused-evidence.json'):
        h.copy_checked(h.identity(prior.PACKAGE/name), PACKAGE/name)
    cpu.save(PACKAGE/'scoped-acceptance.json', reused)
    cpu.save(PACKAGE/'sources.json', sources())
    plan = dict(root=str(ROOT), source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        binary=str(PACKAGE/'candidate/q4t'), candidate=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=old_plan['candidate_sha256'], protocol=old_plan['protocol'],
        protocol_sha256=old_plan['protocol'], previous_binary_sha256=old_plan['previous_binary_sha256'],
        old_hashes=old_plan['old_hashes'], model_dir=old_plan['model_dir'], model_inputs=old_plan['model_inputs'],
        preserved=old_plan['preserved'], limits=LIMITS, cumulative_limits=CUMULATIVE,
        user_authorization='2026-11-05: continue after explicit request for one additional startup; request/time caps unchanged',
        reused_acceptance=h.identity(PACKAGE/'scoped-acceptance.json'),
        prior_archive={name: h.identity(PRIOR_ARCHIVE/name) for name in ('result.json', 'manifest.json', 'records.tar.gz')},
        performance_acceptance=False)
    cpu.require(cpu.sha(plan['candidate']) == plan['candidate_sha256'], 'candidate changed')
    cpu.save(PACKAGE/'plan.json', plan)
    h.seal(PACKAGE)
    return dict(prepared=True, limits=LIMITS, cumulative_limits=CUMULATIVE, models_started=0)


def check():
    h.verify_seal(PACKAGE)
    plan = cpu.read(PACKAGE/'plan.json')
    cpu.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
                plan['cumulative_limits'] == CUMULATIVE, 'frozen completion scope')
    a.verify_hashes(ROOT, cpu.read(PACKAGE/'sources.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    a.verify_hashes(ROOT, cpu.read(PACKAGE/'candidate/runtime-source.json'))
    for name, digest in plan['preserved'].items():
        cpu.require(cpu.sha(name) == digest, 'preserved input changed: '+name)
    for record in plan['prior_archive'].values():
        cpu.require(cpu.sha(record['path']) == record['sha256'], 'prior archive changed')
    cpu.require(cpu.sha(plan['candidate']) == plan['candidate_sha256'], 'candidate drift')
    if (OUTPUT/'begin.json').exists():
        plan['started'] = plan['original_started'] = cpu.read(OUTPUT/'begin.json')['started']
    return plan


class Run(s.Run):
    def __init__(self, plan):
        s.OUT, s.PORT, s.LIMITS = OUTPUT/'business', PORT, LIMITS
        s.OUT.mkdir()
        self.plan, self.port, self.started = plan, PORT, time.monotonic()
        self.counts = dict(starts=0, generation_attempts=0, controls=0)
        self.children = Children(OUTPUT/'lease')
        self.tls, self.sockets = False, []
        self.model = self.nginx = None
        self.successes = self.cancelled = 0
        s.a.OUT = s.OUT

    def execute(self):
        result = dict(status='FAILED', counts=self.counts, limits=LIMITS)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', PORT))
            self.launch(False)
            validate_disabled(cpu.read(s.OUT/'legacy-model-config-wire.bin'))
            self.request('capabilities', path='/v1/capabilities', method='GET')
            for stream, label in ((False, 'disabled-json'), (True, 'disabled-sse')):
                value = self.complete(label, prior.echo(stream))
                cpu.require(value.finish_reason == 'stop' and value.message['content'].strip() == '710003', 'no-policy quality')
            metrics = self.idle('final-idle')
            cpu.require(metrics['q4t_requests_success_total'] == self.successes == 2 and
                metrics['q4t_requests_aborted_total'] == metrics['q4t_client_policy_enabled'] == 0 and
                not any(key.startswith('q4t_client_rate_') and '{' in key for key in metrics), 'disabled rate metrics')
            cpu.save(s.OUT/'final-metrics.json', metrics)
            self.stop(False)
            self.children.reap(s.OUT)
            s.a.require_idle(s.OUT/'final-gpu.json')
            result.update(status='PASS_NO_POLICY_JSON_SSE', successes=2, cancelled=0)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED', error=repr(error))
            raise
        finally:
            for connection in self.sockets:
                connection.close()
            try:
                self.children.cleanup()
            except BaseException as error:
                result.update(cleanup_error=repr(error))
                if result['status'] != 'ENV_ABORT':
                    result['status'] = 'FAILED'
                raise
            finally:
                result['seconds'] = time.monotonic()-self.started
                cpu.save(s.OUT/'result.json', result)


def verify_business():
    """Raw evidence gate precedes metadata creation and either replacement."""
    result = cpu.read(OUTPUT/'business/result.json')
    cpu.require(result['status'] == 'PASS_NO_POLICY_JSON_SSE' and result['counts']['starts'] == 1 and
                result['counts']['generation_attempts'] == result['successes'] == 2 and
                result['counts']['controls'] <= LIMITS['controls'], 'completion budget/result')
    folder = OUTPUT/'business'
    validate_disabled(cpu.read(folder/'legacy-model-config-wire.bin'))
    responses = []
    for label, stream in (('disabled-json', False), ('disabled-sse', True)):
        request = cpu.read(folder/(label+'-request.json'))['body']
        cpu.require(request['stream'] is stream and cpu.read(folder/(label+'-http.json'))['status'] == 200, 'HTTP contract')
        raw = (folder/(label+'-wire.bin')).read_bytes()
        value = (s.a.read_sse if stream else s.a.read_json)(io.BytesIO(raw))
        record = cpu.read(folder/(label+'-response.json'))
        seconds = record.pop('elapsed')
        cpu.require(asdict(value) == record and value.id == request['request_id'] and
            value.model == request['model'] and value.finish_reason == 'stop' and
            value.message['content'].strip() == '710003' and
            0 < value.usage['completion_tokens'] <= request['max_tokens'], 'raw no-policy response')
        responses.append(dict(label=label, usage=value.usage, body_read_seconds=seconds))
    raw_metrics = old_archive.latest_poll(folder, 'final-idle')
    cpu.require(s.parse_metrics(raw_metrics.read_text()) == cpu.read(folder/'final-metrics.json'), 'raw metrics')
    return dict(result=result, responses=responses)


def verifier(plan, installed=False):
    if not installed:
        Run(plan).execute()
        business = verify_business()
        reused = cpu.read(PACKAGE/'scoped-acceptance.json')
        cpu.save(OUTPUT/'acceptance.json', dict(status='PASS_BOUNDED_CLIENT_GOVERNANCE',
            candidate_sha256=plan['candidate_sha256'], models=3, generation_attempts=18,
            successful_responses=14, real_tools=2, cancelled=2, refusals=2,
            business=business, reused_acceptance=plan['reused_acceptance'],
            prior_failures_preserved=True, prior_cumulative_lease_seconds=reused['cumulative_lease_seconds'],
            current_guard_pending=True, performance_acceptance=False))
        return
    cpu.require(cpu.sha(ROOT/'build/q4t') == plan['candidate_sha256'], 'installed candidate differs')
    folder = OUTPUT/'health'
    folder.mkdir()
    cpu.require(cpu.command(folder, 'version', [ROOT/'build/q4t', 'version'], 10), 'installed CLI')
    cpu.save(folder/'result.json', dict(passed=True, installed=h.identity(ROOT/'build/q4t'),
        reused_same_elf_http=h.identity(OUTPUT/'business/result.json'), new_models=0, new_http=0))


def release(plan):
    result = OUTPUT/'acceptance.json'
    cpu.require(cpu.read(result)['status'] == 'PASS_BOUNDED_CLIENT_GOVERNANCE', 'combined business gate')
    metadata = cpu.read(PACKAGE/'previous/q4t.release.json')
    inherited = {k: copy.deepcopy(metadata[k]) for k in ('acceptance', 'acceptance_decision',
        'acceptance_decision_sha256', 'chat_protocol', 'deployment_completion', 'scope') if k in metadata}
    inherited['release'] = h.identity(PACKAGE/'previous/q4t.release.json')
    for key in ('deployment_completion', 'implementation_capabilities', 'metadata_correction'):
        metadata.pop(key, None)
    metadata.update(version='serve-client-governance-20261105', binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'], tool_source_commit=plan['source_commit'],
        source_note='Optional v2 RPM/TPM reservation/settlement and prepared-slot client rotation; default disabled.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'), build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_CLIENT_GOVERNANCE', acceptance_decision=str(result), acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result), tool_acceptance_sha256=cpu.sha(result), inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'), previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=cpu.read(OUTPUT/'business/legacy-model-config-wire.bin'),
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'), rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', production='default MTP k3, full head, text; minute quotas/fairness opt-in',
            validation='303 host/269 required/9 structured; scoped TLS minute quotas, cancellation, slot order and rollover; two actual project tool loops; same-ELF no-policy JSON/SSE.',
            installation_health='identical accepted ELF, installed CLI; HTTP reused',
            boundaries='Process-local fixed windows, prepared-slot fairness only. No preparation/GPU-time SLO, distributed billing, long soak, real LAN, new managed/task migration or physical RAM acceptance. Existing packages/tasks pinned.',
            numerical_evidence=str(PACKAGE/'numerical-reuse.json'), reused_evidence=str(PACKAGE/'scoped-acceptance.json'),
            performance_acceptance=False))
    folder = OUTPUT/'release'
    declarations = folder/'declarations'
    declarations.mkdir(parents=True)
    for name, label in (('capabilities.json', 'capabilities-wire.bin'), ('configuration.json', 'legacy-model-config-wire.bin')):
        h.copy_checked(h.identity(OUTPUT/'business'/label), declarations/name)
    shutil.copytree(PACKAGE/'candidate/schemas', declarations/'schemas')
    caps = cpu.read(declarations/'capabilities.json')
    protocol = metadata['chat_protocol']
    protocol['protocol_sha256'] = plan['protocol']
    protocol['client_governance'] = copy.deepcopy(caps['client_governance'])
    for key in caps.keys() & protocol.keys():
        protocol[key] = copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path = declarations/'schemas'/(name+'.json')
        protocol[name+'_schema'], protocol[name+'_schema_sha256'] = str(path), cpu.sha(path)
    protocol['declaration_evidence'] = 'Actual same-ELF no-policy config/capabilities plus frozen schemas; scoped evidence preserved separately. Old package/task identities stay pinned.'
    metadata['release_declarations'] = dict(profile=pkg.DECLARATION_PROFILE, directory=str(declarations),
        binary_sha256=plan['candidate_sha256'], protocol_sha256=plan['protocol'],
        files={name: cpu.sha(declarations/name) for name in pkg.DECLARATION_FILES})
    pkg.verify_declarations(metadata, required=True)
    cpu.save(folder/'q4t.release.json', metadata)
    cpu.save(folder/'binding.json', dict(old=plan['old_hashes'], new=dict(
        q4t=plan['candidate_sha256'], **{'q4t.release.json': cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder)
    return folder


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-client-governance-completion-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release


def execute():
    bind()
    h.require_guard(OUTPUT)
    cpu.save(OUTPUT/'begin.json', dict(started=time.time(), limits=LIMITS))
    def expired(*_):
        raise RuntimeError('completion deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-45)
    try:
        a.execute()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    bind()
    a.finalize()
    h.seal(OUTPUT)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('test', 'prepare', 'check', 'execute', 'finalize'))
    print(json.dumps(globals()[parser.parse_args().action]() or dict(passed=True)))
