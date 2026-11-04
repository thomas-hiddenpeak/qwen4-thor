"""Deliver the accepted client-isolation ELF with the existing rollback transaction."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_clients import cpu, short as s, handoff as fix, report
from tools.serve_media_composite import stage as transaction

h,a=transaction.h,transaction.a
BASE=cpu.BASE
PACKAGE,OUTPUT=BASE/'delivery-package',BASE/'delivery'
LIMITS=dict(starts=0,generations=0,controls=0,seconds=600)
NEW_RUNTIME=('include/q4t/server/client_identity.h','include/q4t/server/client_policy.h',
    'include/q4t/server/client_request.h','src/server/client_policy.cpp','src/server/client_request.cpp')


def evidence():
    report.verify()
    value=s.pkg.read(report.DEST/'result.json')
    h.require(value['status']=='ACCEPT_BOUNDED_CLIENT_ISOLATION', 'business gate')
    s.verify_sources(s.pkg.read(BASE/'handoff-build/sources.json'))
    h.require(cpu.sha(fix.ENGINE/'q4t')==value['candidate_sha256'], 'accepted ELF changed')
    return value


def prepare():
    accepted=evidence()
    h.require(not PACKAGE.exists() and not OUTPUT.exists(), 'preserve delivery attempt')
    h.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT), 'commit delivery first')
    short=s.pkg.read(BASE/'cache-plan.json')
    a.verify_hashes(ROOT,short['default'])
    previous=h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',short['default']['build/q4t'])
    old=s.pkg.read(previous['tested_source_manifest'])
    current={name:cpu.sha(ROOT/name) for name in sorted(set(old)|set(NEW_RUNTIME))}
    changed={name for name in old if old[name]!=current[name]}
    allowed={'CMakeLists.txt','cmake/ChatProtocolIdentity.cmake','cmake/HostTestSources.cmake','src/main.cpp'}
    h.require(all(name in allowed or name.startswith(('src/server/','include/q4t/server/')) for name in changed),
              'numerical or unreviewed runtime change')
    built=s.pkg.read(BASE/'handoff-build/sources.json')
    h.require(all(built[name]==digest for name,digest in current.items()), 'runtime not in accepted build')
    PACKAGE.mkdir()
    for name in ('candidate','previous'):
        (PACKAGE/name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT/'build'/name),PACKAGE/'previous'/name)
    h.copy_checked(h.identity(previous['build_cache']),PACKAGE/'previous/CMakeCache.txt')
    for name in ('q4t','CMakeCache.txt','compile_commands.json'):
        h.copy_checked(h.identity(fix.ENGINE/name),PACKAGE/'candidate'/name)
    shutil.copytree(fix.ENGINE/'generated',PACKAGE/'candidate/generated')
    for name in ('candidate','previous'):
        h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),PACKAGE/name/'q4t.rules.yml')
    cpu.save(PACKAGE/'candidate/runtime-source.json',current)
    cpu.save(PACKAGE/'numerical-reuse.json',dict(changed=sorted(changed),added=list(NEW_RUNTIME),
        previous_manifest=h.identity(previous['tested_source_manifest']),
        current_manifest=h.identity(PACKAGE/'candidate/runtime-source.json'),
        model_math_tokenizer_template_cache_storage_unchanged=True, performance_acceptance=False,
        note='Client identity scopes existing cache/registry keys. Handoff changes client admission only. '
             'Legacy HTTP from first candidate is reused for unchanged unscoped path; scoped HTTP/JSON/tool/cache from final ELF.'))
    paths=set(short['sources'])|{'tools/serve_clients/report.py','tools/serve_clients/delivery.py',
        'docs/SERVE_CLIENT_RESULT_2026-11-04.md','docs/SERVE_CLIENT_DELIVERY_2026-11-04.md'}
    cpu.save(PACKAGE/'sources.json',{name:cpu.sha(ROOT/name) for name in sorted(paths)})
    days={path:cpu.sha(path) for path in short['day_plans']}
    cpu.save(PACKAGE/'plan.json',dict(root=str(ROOT),limits=LIMITS,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        candidate=str(PACKAGE/'candidate/q4t'),binary=h.identity(PACKAGE/'candidate/q4t'),
        candidate_sha256=accepted['candidate_sha256'],protocol_sha256=accepted['protocol_sha256'],
        previous_binary_sha256=short['default']['build/q4t'],
        old_hashes={name:cpu.sha(PACKAGE/'previous'/name) for name in a.NAMES},
        acceptance=h.identity(report.DEST/'result.json'),archive=h.identity(report.DEST/'manifest.json'),
        model_dir=short['model_dir'],model_inputs=short['model_inputs'],day_plans=days,
        performance_acceptance=False,managed_package_e2e=False))
    h.seal(PACKAGE)
    return dict(prepared=True,candidate=accepted['candidate_sha256'],models_started=0)


def check():
    h.verify_seal(PACKAGE)
    plan=s.pkg.read(PACKAGE/'plan.json')
    h.require(plan['root']==str(ROOT) and plan['limits']==LIMITS,'delivery scope')
    a.verify_hashes(ROOT,s.pkg.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT,s.pkg.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'],plan['model_inputs'])
    for name in ('binary','acceptance','archive'):
        h.check_identity(plan[name])
    for name,digest in plan['day_plans'].items():
        h.require(cpu.sha(name)==digest and not (Path(name).parent/'execution').exists(), 'operator day changed')
    if (OUTPUT/'begin.json').exists():
        plan['started']=plan['original_started']=s.pkg.read(OUTPUT/'begin.json')['started']
    return plan


def verifier(plan,installed=False):
    accepted=evidence()
    h.require(accepted['candidate_sha256']==plan['candidate_sha256'], 'accepted candidate mismatch')
    if not installed:
        folder=OUTPUT/'business'; folder.mkdir()
        cpu.save(folder/'result.json',dict(status='PASS_REUSED_CLIENT_ACCEPTANCE',
            evidence=plan['acceptance'],new_model_starts=0,new_http=0))
        return
    h.require(cpu.sha(ROOT/'build/q4t')==plan['candidate_sha256'],'installed ELF drift')
    folder=OUTPUT/'health';folder.mkdir()
    h.require(cpu.command(folder,'version',[str(ROOT/'build/q4t'),'version'],10),'installed CLI')
    h.require('Qwen4-Thor' in (folder/'version.stdout').read_text(),'version output')
    cpu.save(folder/'result.json',dict(status='PASS_SAME_ELF_HTTP_REUSE_AND_CLI',
        installed=h.identity(ROOT/'build/q4t'),acceptance=plan['acceptance'],new_model_starts=0,new_http=0))


def release(plan):
    accepted=evidence()
    caps=s.pkg.read(BASE/'cache-short/scoped-model-config-wire.bin')
    h.require(caps['protocol_source_sha256']==plan['protocol_sha256'],'observed protocol drift')
    metadata=s.pkg.read(PACKAGE/'previous/q4t.release.json')
    inherited=copy.deepcopy({key:metadata[key] for key in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope',
        'implementation_capabilities') if key in metadata})
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion',None)
    metadata.pop('implementation_capabilities',None)
    metadata.update(version='serve-clients-20261104',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Optional private client policy authenticated by TLS gateway; client data/control quotas '
            'held through cleanup with 100ms terminal handoff; prompt/output/body budgets; scoped request, '
            'cancel and private/shared cache keys. No default LAN listener or credentials configured.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),
        build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance=accepted['status'],acceptance_decision=plan['acceptance']['path'],
        acceptance_decision_sha256=plan['acceptance']['sha256'],
        tool_acceptance=plan['acceptance']['path'],tool_acceptance_sha256=plan['acceptance']['sha256'],
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        inherited_acceptance=inherited,observed_configuration=caps,
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',heads=['full'],production='MTP k3, seq1, policy loopback text only',
            validation='27 native HTTP contracts plus two affected repeats; auth/budgets/cancellation, '
                'strict JSON immediately followed by real project tool and citation; private/shared model cache isolation.',
            evidence='18 successful generations/2 cancellations/6 pre-generation refusals across three attempts. '
                'Original link, terminal quota and missing shared-seed fixture failures preserved.',
            capabilities_configuration='Observed scoped test configuration; policy remains opt-in at startup.',
            boundaries='No new managed package E2E, actual LAN/CA/firewall, long-soak, token-per-minute fairness '
                'or per-client physical RAM guarantee. No new multimodal or SDK qualification.',
            installation_health='Exact accepted ELF reuse and installed CLI; no new model or HTTP',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'],
        declaration_evidence='Template/observability/http declaration inherited; '
            'ChatCapabilitiesJson implementation unchanged. Current config and real responses separately recorded.',
        sdk_interoperability_acceptance=dict(current='Python http.client through real TLS gateway',
            prior_sdk_evidence=inherited['release'],new_sdk_qualification=False))
    folder=OUTPUT/'release';folder.mkdir()
    cpu.save(folder/'q4t.release.json',metadata)
    cpu.save(folder/'binding.json',dict(old=plan['old_hashes'],new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json':cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder)
    return folder


def bind():
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-clients-recovery.json'
    a.check,a.verifier,a.release=check,verifier,release
    transaction.BASE,transaction.PACKAGE,transaction.OUTPUT=BASE,PACKAGE,OUTPUT
    transaction.LIMITS=LIMITS
    transaction.check,transaction.verifier,transaction.release,transaction.bind=check,verifier,release,bind


def finalize():
    check();bind()
    h.require(time.time()-s.pkg.read(OUTPUT/'begin.json')['started']<=LIMITS['seconds'],'delivery deadline')
    a.finalize();h.seal(OUTPUT)


def rollback(output):
    h.verify_seal(PACKAGE)
    plan=s.pkg.read(PACKAGE/'plan.json')
    h.require(plan['root']==str(ROOT),'foreign rollback')
    h.verify_pair(PACKAGE/'previous/q4t',PACKAGE/'previous/q4t.release.json',plan['previous_binary_sha256'])
    bind();a.check=lambda:plan
    a.restore(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','check','execute','finalize','rollback'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='execute':
        bind();transaction.execute()
    elif args.action=='rollback':
        h.require(args.output is not None,'fresh rollback lease directory required')
        rollback(args.output.resolve())
    else:
        value=globals()[args.action]()
        print(json.dumps(value if args.action=='prepare' else dict(action=args.action,passed=True)))
