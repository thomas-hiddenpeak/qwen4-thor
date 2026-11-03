"""One explicit correction of SRV-13; retain the failed run and its hard budget."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_lifecycle import stage as original, deployment, observe
from tools.serve_lifecycle.verify import Run as Lifecycle
from tools.serve_observation_capacity.cpu import command

h, a = original.h, original.a
BASE = ROOT/'.q4t-work/serve-lifecycle-repair-20261103'
PACKAGE, OUTPUT = BASE/'package', BASE/'execution'
MARKER = ROOT/'build/serve-lifecycle-repair-recovery.json'
PRIOR_COUNTS = dict(generations=1202, negatives=0, starts=1)
FINAL_COUNTS = dict(generations=1218, negatives=4, starts=6)


def original_gate():
    h.verify_seal(original.PACKAGE);h.verify_seal(original.OUTPUT)
    failed = h.read(original.OUTPUT/'business/result.json')
    h.require(failed['status'] == 'FAILED' and failed['counts'] == PRIOR_COUNTS and
              failed['error'] == "RuntimeError('graceful client error classification')" and
              h.read(original.OUTPUT/'lease/exit.json')['status'] == 'COMMAND_FAILED',
              'original failure identity')
    soak = h.read(original.OUTPUT/'business/soak-result.json')
    h.require(soak['passed'] and soak['cycles'] == 240 and soak['generations'] == 1200 and
              soak['seconds'] >= 3600, 'soak reuse gate')
    envelope = h.read(original.OUTPUT/'business/resource-envelope.json')
    h.require(envelope['warmup_cycles'] == 16 and envelope['measured_cycles'] == 224 and
              envelope['additive_envelope'] == original.ENVELOPE and all(
                  envelope['maxima'][k] <= envelope['baseline'][k]+v
                  for k,v in original.ENVELOPE.items()), 'soak resource envelope')
    return failed, h.read(original.OUTPUT/'begin.json')['started']


def cpu():
    folder = BASE/'cpu/tests';folder.mkdir()
    build = h.read(BASE/'cpu/build-result.json')
    h.require(build['passed'] and build['zero_warnings'] and
              build['runtime_sources_unchanged_during_build'], 'build gate')
    a.verify_hashes(ROOT, h.read(BASE/'cpu/build-sources.json'))
    checks = [
        ('host', [str(BASE/'engine/q4t_host_tests')]),
        ('required', ['/usr/bin/python3', '-B', 'tools/release/check_host_contracts.py',
                      '--test-binary', str(BASE/'engine/q4t_host_tests'),
                      '--output', str(folder/'required')]),
        ('driver', ['/usr/bin/python3', '-B', '-m', 'unittest', '-v',
                    'tools.serve_lifecycle.test_deployment', 'tools.serve_lifecycle.test_driver']),
        ('inventory', ['/usr/bin/python3', '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B', 'tools/serve_protocol/build_contract.py', '--check'])]
    results = {name:command(folder, name, argv, timeout=180) for name,argv in checks}
    h.save(folder/'result.json', dict(passed=all(results.values()), commands=results,
                                    http_or_model_started=False))
    h.require(all(results.values()), 'CPU gate failed; preserve all results')


def prepare():
    h.require(not PACKAGE.exists(), 'frozen correction exists')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed correction before freezing')
    failed, started = original_gate()
    h.require(time.time() < started+original.LIMITS['seconds']-600,
              'insufficient original time reserve; no automatic extension')
    cpu_result = h.read(BASE/'cpu/tests/result.json')
    h.require(all(cpu_result['commands'][key] for key in ('host','required','driver')) and
              h.read(BASE/'cpu/tests/reviewed-drift/result.json')['passed'], 'CPU gate')
    build = h.read(BASE/'cpu/build-result.json')
    current = h.read(BASE/'cpu/build-sources.json');a.verify_hashes(ROOT, current)
    prior = h.read(original.PACKAGE/'runtime-sources.json')
    h.require(current.keys() == prior.keys() and
              [p for p in current if current[p] != prior[p]] == ['src/server/chat_admission.cpp'],
              'repair must change only shutdown admission runtime')
    h.require(build['passed'] and build['zero_warnings'] and
              build['runtime_sources_unchanged_during_build'] and
              build['binary_sha256'] == h.sha(BASE/'engine/q4t'), 'build identity')
    cache = (BASE/'engine/CMakeCache.txt').read_text()
    h.require('Q4T_SERVE_FAULT_INJECTION:BOOL=OFF' in cache and
              'Q4T_ENABLE_XGRAMMAR:BOOL=ON' in cache, 'production build mode')
    commands = h.read(BASE/'engine/compile_commands.json')
    server = [r for r in commands if r['output'].startswith('CMakeFiles/q4t_server.dir/')]
    h.require(server and all('-DQ4T_SERVE_FAULT_INJECTION=1' not in r['command']
                            for r in server), 'production fault injection')
    a.verify_hashes(ROOT/'build', {'q4t':original.SHA,'q4t.release.json':original.SIDECAR})
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending recovery')
    PACKAGE.mkdir(mode=0o700)
    for name in ('candidate', 'previous'):(PACKAGE/name).mkdir()
    shutil.copytree(BASE/'cpu', PACKAGE/'cpu')
    for name in ('q4t','CMakeCache.txt','compile_commands.json','q4t_http_lifecycle_tests'):
        h.copy_checked(h.identity(BASE/'engine'/name), PACKAGE/'candidate'/name)
    shutil.copytree(BASE/'engine/generated', PACKAGE/'candidate/generated')
    h.save(PACKAGE/'candidate/runtime-source.json', current)
    old = h.read(ROOT/'build/q4t.release.json')
    for name in a.NAMES:h.copy_checked(h.identity(ROOT/'build'/name), PACKAGE/'previous'/name)
    h.copy_checked(h.identity(old['build_cache']), PACKAGE/'previous/CMakeCache.txt')
    for dest in ('candidate','previous'):
        h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'), PACKAGE/dest/'q4t.rules.yml')
    protocol = h.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    a.verify_hashes(ROOT, protocol)
    digest = hashlib.sha256(''.join(f'{p}:{protocol[p]}\n' for p in sorted(protocol)).encode()).hexdigest()
    h.save(PACKAGE/'candidate/diagnostic-identity.json',dict(binary_sha256=build['binary_sha256'],
        chat_protocol=dict(protocol_sha256=digest), candidate_identity_only=True,
        acceptance='PENDING; not a release attestation'))
    paths = subprocess.check_output(['git','ls-files','tools','deploy/serve-ops'],cwd=ROOT,text=True).splitlines()
    paths += ['docs/SERVE_LIFECYCLE_REPAIR_2026-11-03.md','docs/MODEL.md']
    paths += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json',{p:h.sha(ROOT/p) for p in sorted(set(paths)) if (ROOT/p).is_file()})
    plan = copy.deepcopy(h.read(original.PACKAGE/'plan.json'))
    plan.update(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        candidate=str(PACKAGE/'candidate/q4t'),candidate_sha256=build['binary_sha256'],
        protocol_sha256=digest, binary=h.identity(PACKAGE/'candidate/q4t'),
        sidecar=h.identity(PACKAGE/'candidate/diagnostic-identity.json'),
        old_hashes={n:h.sha(PACKAGE/'previous'/n) for n in a.NAMES},previous_binary_sha256=original.SHA,
        started=started, lease_root=str(OUTPUT), prior_counts=PRIOR_COUNTS, final_counts=FINAL_COUNTS,
        planned_correction=dict(generations=16,negatives=4,starts=5),
        source_manifest_sha256=h.sha(PACKAGE/'sources.json'),
        final_default_changed=True,runtime_changed=True,automatic_retries=0,
        explicit_failure_correction=True, original_failed_result=h.identity(original.OUTPUT/'business/result.json'))
    h.save(PACKAGE/'reuse.json',dict(original_package=h.identity(original.PACKAGE/'sealed-files.json'),
        original_execution=h.identity(original.OUTPUT/'sealed-files.json'),
        original_soak=h.identity(original.OUTPUT/'business/soak-result.json'),
        changed_runtime=['src/server/chat_admission.cpp'],
        reuse='Original 240 normal cycles and resource envelope only; not new-ELF hour-long acceptance. Model, tokenizer, template and math unchanged.',
        failures_retained=True, original_budget_unchanged=True))
    h.save(PACKAGE/'plan.json',plan)
    subprocess.run(['git','bundle','create',str(PACKAGE/'source.bundle'),'05d89984..HEAD'],cwd=ROOT,check=True)
    h.seal(PACKAGE);print('PREPARED',plan['candidate_sha256'],flush=True)


def check():
    h.verify_seal(PACKAGE);plan=h.read(PACKAGE/'plan.json')
    original_gate()
    h.require(plan['limits']==original.LIMITS and plan['prior_counts']==PRIOR_COUNTS and
              plan['final_counts']==FINAL_COUNTS and plan['old_hashes']==
              {'q4t':original.SHA,'q4t.release.json':original.SIDECAR},'frozen correction contract')
    h.require(plan['source_manifest_sha256']==h.sha(PACKAGE/'sources.json'),'source manifest changed')
    a.verify_hashes(ROOT,h.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT,h.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    h.require(original.sdk_identity()==h.read(original.PACKAGE/'sdk.json'),'SDK changed')
    for key in ('binary','sidecar','original_failed_result'):h.check_identity(plan[key])
    return plan


class Run(Lifecycle):
    def __init__(self,plan,output,*,installed=False):
        super().__init__(plan,output)
        self.installed=installed
        prior=(h.read(OUTPUT/'business/result.json') if installed else
               h.read(original.OUTPUT/'business/result.json'))
        self.counts=prior['counts'].copy();self.preflights=240
        self.control_count=prior['control_requests_used']
        if installed:self.last_diag=OUTPUT/'business/diagnostics/previous-business'

    def release_config(self,role):
        if role=='previous':return super().release_config(role)
        h.require(role=='candidate','unknown candidate role')
        if self.installed:
            binding=h.read(OUTPUT/'release/binding.json')
            a.verify_hashes(ROOT/'build',binding['new'])
            h.require(h.read(MARKER).get('stage')==str(PACKAGE),'foreign deployment marker')
            binary,sidecar=ROOT/'build/q4t',ROOT/'build/q4t.release.json'
        else:
            a.verify_hashes(ROOT/'build',self.plan['old_hashes'])
            h.require(not list((ROOT/'build').glob('*recovery*.json')),'pending recovery')
            binary,sidecar=Path(self.plan['candidate']),Path(self.plan['sidecar']['path'])
        h.require(h.sha(binary)==self.plan['candidate_sha256'],'candidate executable drift')
        return dict(binary=str(binary),sidecar=sidecar,
            pair=dict(observability_version=4,protocol_sha256=self.plan['protocol_sha256']),
            installed=dict(candidate_installed=self.installed,binary_sha256=h.sha(binary)),
            rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'))

    def interrupted(self,name,*,crash):
        requests=super().interrupted(name,crash=crash)
        if not crash:
            log=(self.model_folder/'server.log').read_text()
            for body in requests:
                lines=[line for line in log.splitlines() if
                       f'request cancelled id={body["request_id"]} reason=server_shutdown' in line]
                h.require(len(lines)==1,'shutdown abort must be counted exactly once per request')
            h.save(self.out/'shutdown-abort-accounting.json',dict(passed=True,requests=2,
                source='two production count_abort log events; no post-exit metric fabricated'))
        return requests

    def run(self):
        result=dict(status='FAILED',automatic_retries=0,requests_replayed=0,
                    prior_counts=PRIOR_COUNTS,original_failed_run_retained=True)
        try:
            if self.installed:
                self.start_model('model-installed','candidate',cross_instance=True)
                self.recovery_business('installed-business');self.stop_model(expected_reaped=2)
                h.require(self.counts==FINAL_COUNTS and len(self.wires)==3,'installed cumulative counts')
            else:
                self.start_model('model-1-corrected','candidate')
                graceful=self.interrupted('graceful-inflight',crash=False)
                self.start_model('model-2-restart','candidate',cross_instance=True)
                self.recovery_business('after-graceful',graceful)
                crashed=self.interrupted('crash-inflight',crash=True)
                self.start_model('model-3-restart','candidate',cross_instance=True)
                self.recovery_business('after-crash',crashed);self.stop_model(expected_reaped=2)
                deployment.transact(ROOT,original.BUNDLE,'previous',self.out/'switch-previous',OUTPUT)
                self.start_model('model-4-rollback','previous',cross_instance=True)
                self.recovery_business('previous-business');self.stop_model(expected_reaped=2)
                deployment.transact(ROOT,original.BUNDLE,'current',self.out/'switch-original-current',OUTPUT)
                h.require(self.counts==dict(generations=1215,negatives=4,starts=5) and
                    sum(r['success'] for r in self.wires)==9 and len(self.interruptions)==4,
                    'fixed correction counts')
            h.save(self.out/'wire-schema-result.json',dict(passed=True,complete_records=self.wires,
                interrupted_records=self.interruptions))
            result.update(status='PASS_BOUNDED_LIFECYCLE_REPAIR',installed=self.installed,
                          reused_original_hour=True,new_candidate_hour_proved=False)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error,(a.EnvironmentAbort,observe.ObservationAbort))
                          else 'FAILED',error=repr(error));raise
        finally:
            try:
                if self.server is not None:self.stop_model()
                if not self.installed:self.restore_current()
            except BaseException as error:
                result.update(status='ENV_ABORT' if isinstance(error,(a.EnvironmentAbort,observe.ObservationAbort))
                              else 'FAILED',cleanup_error=repr(error));raise
            finally:
                result.update(counts=self.counts,preflights=self.preflights,control_requests_used=self.control_count,
                    instances=self.instances,elapsed_seconds=time.monotonic()-self.started,
                    original_elapsed_seconds=time.time()-self.plan['started'])
                h.save(self.out/'result.json',result)


def verifier(plan,installed=False):
    if not installed:
        folder=OUTPUT/'native';folder.mkdir()
        passed=command(folder,'lifecycle',[str(PACKAGE/'candidate/q4t_http_lifecycle_tests')],timeout=90)
        logs=(folder/'lifecycle.stdout').read_text()+(folder/'lifecycle.stderr').read_text()
        passed=passed and '24 tests, 24 passed, 0 failed, 0 skipped' in logs
        h.save(folder/'result.json',dict(passed=passed,tests=24,model_loaded=False,
            binary=h.identity(PACKAGE/'candidate/q4t_http_lifecycle_tests')))
        h.require(passed,'native shutdown and existing HTTP contracts')
    Run(plan,OUTPUT/('health' if installed else 'business'),installed=installed).run()


def release(plan):
    result=OUTPUT/'business/result.json'
    h.require(h.read(result)['status']=='PASS_BOUNDED_LIFECYCLE_REPAIR','candidate business gate')
    caps=json.loads(h.read(OUTPUT/'business/model-1-corrected/capabilities.json')['raw'])
    metadata=h.read(PACKAGE/'previous/q4t.release.json')
    inherited={key:copy.deepcopy(metadata[key]) for key in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','scope','deployment_completion') if key in metadata}
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion',None)
    metadata.update(version='serve-lifecycle-repair-20261103',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Queued shutdown follows monotonic cancellation, error terminal and abort accounting. Original hour remains evidence of previous ELF only.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_LIFECYCLE_REPAIR',acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result),tool_acceptance=str(result),tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        inherited_acceptance=inherited,implementation_capabilities=caps,
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',heads=['full'],production='MTP k3, seq1/8192/inflight8/queued2',
            validation='Native24; real active and prepared-queue SIGTERM, SIGKILL unknown outcomes, restart and v3 rollback with real project tool loops.',
            reused_hour=str(original.OUTPUT/'business/soak-result.json'),
            boundaries='No new-ELF hour soak, day-scale stability, physical RAM hard-limit or daemon supervisor claim.',
            installation_health='Actual installed candidate ordinary and real project tool loop',performance_acceptance=False))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'])
    directory=OUTPUT/'release';directory.mkdir()
    h.save(directory/'q4t.release.json',metadata)
    h.save(directory/'binding.json',dict(old=plan['old_hashes'],new={
        'q4t':plan['candidate_sha256'],'q4t.release.json':h.sha(directory/'q4t.release.json')}))
    h.seal(directory);return directory


def bind():
    a.BASE,a.PACKAGE,a.OUTPUT,a.MARKER=BASE,PACKAGE,OUTPUT,MARKER
    a.check,a.verifier,a.release=check,verifier,release


def execute():
    h.require_guard(OUTPUT);plan=check()
    remaining=plan['started']+original.LIMITS['seconds']-90-time.time()
    h.require(remaining>0,'original deadline exhausted')
    def expired(signum,frame):raise RuntimeError('original lifecycle deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,remaining)
    try:bind();a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    plan=check()
    h.require(time.time()-plan['started']<=original.LIMITS['seconds'],'original deadline')
    h.require(h.read(OUTPUT/'health/result.json')['counts']==FINAL_COUNTS,'full cumulative budget')
    bind();a.finalize();h.seal(OUTPUT)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('cpu','prepare','launch','execute','finalize'))
    args=parser.parse_args()
    if args.action=='launch':
        check();OUTPUT.mkdir(mode=0o700)
        raise SystemExit(subprocess.call(['/usr/bin/python3','-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output',str(OUTPUT/'lease'),'--','/usr/bin/python3','-B',str(Path(__file__)),'execute'],cwd=ROOT))
    globals()[args.action]()


if __name__=='__main__':main()
