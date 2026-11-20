"""Two capacities: shared Chat speech, Realtime voices and caller isolation.

One strict lease, two models, two native TLS entries, six WSS sessions and ten
response attempts (including two denied voices); no backend matrix or retries.
"""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import signal
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import contracts
from tools.serve_recovery import ledger, profile_acceptance as full
from tools.serve_recovery import profile_contract, profile_evidence, profile_record
from tools.serve_recovery import profile_refusal_targeted as prior
from tools.serve_recovery import profile_speech, profile_speech_reuse as reuse
from tools.serve_recovery import profile_speech_source
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service.package import read, require, save, sha

FORMAT = 'q4t-speech-targeted-qualification-v1'
SUCCESS = 'PASS_SPEECH_PRIMARY_LIGHT'
LIMITS = dict(starts=2, chats=10, controls=2000, seconds=1200,
              websocket_sessions=6, ingress_starts=2)
LIGHT_LIMITS = dict(LIMITS, starts=1, chats=5, websocket_sessions=3, ingress_starts=1,
                   seconds=900)


def limits(plan):
    return LIGHT_LIMITS if 'retained_primary_business' in plan else LIMITS


def requests():
    return dict(chat=profile_evidence.tts_requests()['chat'])


def original(plan, name):
    path = Path(plan['previous_registry'])
    require(sha(path) == reuse.REVIEW['previous_registry_sha256'], 'accepted registry drift')
    registry = read(path); source = path.parent / (name + '.json')
    require(sha(source) == profile_speech_source.SOURCES[name]['record_sha256'],
            'accepted record bytes drift')
    value = profile_record.read(source)
    profile_evidence.validate(value, registry, name, sha(source))
    return registry, value


def check_plan(plan):
    bound = limits(plan)
    require(plan['targeted_format'] == FORMAT and plan['limits'] == bound and
        plan['requests'] == requests() and plan['automatic_retries'] is False and
        plan['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        plan['identity']['protocol_sha256'] == reuse.REVIEW['candidate_protocol'] and
        plan['planned'] == {k: v for k,v in bound.items() if k not in ('controls','seconds')},
        'speech scope, candidate or budget drift')
    path = Path(plan['source_review'])
    require(sha(path) == reuse.REVIEW['source_review'] and
        sha(path.with_name('runtime.diff')) == reuse.REVIEW['runtime_diff'], 'audited source drift')
    reviewed = read(path)
    for name, digest in {**reviewed['unchanged'], **reviewed['added'],
            **{k: v['after'] for k,v in reviewed['changed'].items()}}.items():
        require(sha(ROOT / name) == digest, 'compiled source changed: ' + name)
    require(plan['voice_snapshot'] == prior.voice_inputs(plan['voice_snapshot']['manifest']),
            'accepted saved voice drift')
    actor = object.__new__(Stage); actor.plan = plan
    for name in profile_contract.NAMES:
        registry, record = original(plan, name)
        require(plan['profiles'] == registry['profiles'] and
            {k:v for k,v in plan['identity'].items() if k not in ('binary_sha256','protocol_sha256')} ==
            {k:v for k,v in registry['identity'].items() if k not in ('binary_sha256','protocol_sha256')},
            'backend or capacity drift')
        full.capacity.check_clients(plan['profiles'][name], actor.client_policy())
        require(all(ledger.canonical(plan['voice_snapshot']['records']['voice']['data'][k]) ==
            ledger.canonical(record['business']['voice']['conditioning']['voice']['data'][k])
            for k in prior.reuse.CONDITION_FIELDS), 'saved voice condition drift')
    if 'retained_primary_business' in plan:
        from tools.serve_recovery import profile_speech_primary
        profile_speech_primary.verify(plan['retained_primary_business']['source'], plan,
                                     original(plan, 'primary')[1])


def prepare(args):
    require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
            'commit reviewed changes before freezing')
    path = args.previous_registry.resolve(strict=True)
    previous = read(path.parent.parent / 'plan.json')
    manifest = args.package.resolve(strict=True) / 'engine/manifest.json'
    base = read(manifest)
    require(base['settings'] == previous['base']['settings'], 'retained settings drift')
    args.sdk_directory = Path(previous['sdk']['sdk_directory'])
    bound = LIGHT_LIMITS if args.continue_primary else LIMITS
    encrypted.foreground.base.prepare(args, rows=requests(), limits=bound, scope=__doc__)
    out = args.output.resolve(); plan = read(out / 'plan.json')
    base, candidate = full.candidate_build_base(base, args.binary, args.candidate_build)
    plan.update(targeted_format=FORMAT, base=base, candidate_identity=candidate,
        previous_registry=str(path), source_review=str(args.source_review.resolve(strict=True)),
        profiles=copy.deepcopy(previous['profiles']),
        identity=dict(previous['identity'], binary_sha256=plan['binary_sha256'],
                      protocol_sha256=base['protocol_sha256']),
        native_assets=copy.deepcopy(previous['native_assets']), chat_audio=True,
        tts_sampling_profile=previous['tts_sampling_profile'],
        planned={k:v for k,v in bound.items() if k not in ('controls','seconds')},
        voice_snapshot=prior.voice_inputs(previous['voice_snapshot']['manifest']))
    plan['speech'] = profile_speech.prepare(args.speech_reference, out, plan['port']+2, plan['sdk'])
    sources = [manifest, path, path.parent.parent / 'plan.json',
        Path(plan['source_review']), Path(plan['source_review']).with_name('runtime.diff'),
        Path(plan['voice_snapshot']['manifest']),
        *(path.parent / (n + '.json') for n in profile_contract.NAMES),
        *(Path(candidate[k]['path']) for k in ('compiled_sources','build_cache'))]
    sources += [Path(plan['voice_snapshot']['directory']) / n for n in plan['voice_snapshot']['files']]
    names = subprocess.check_output(['git','ls-files','tools/serve_realtime',
        'tools/serve_responses','tools/serve_recovery','tools/serve_ops'], cwd=ROOT, text=True).splitlines()
    for p in [*sources, *(ROOT / n for n in names)]: plan['sources'][str(p)] = sha(p)
    plan['sources'].update(plan['speech']['files'])
    plan['protected'][str(manifest)] = sha(manifest)
    if args.continue_primary:
        from tools.serve_recovery import profile_speech_primary
        source = args.continue_primary.resolve(strict=True)
        plan['retained_primary_business'] = dict(
            format='q4t-speech-reviewed-primary-v1', source=str(source))
        fresh = profile_speech_primary.verify(source, plan, original(plan, 'primary')[1])
        save(out/'primary-fresh.json',fresh)
        for name in ('primary-config.json','primary-capabilities.json'):
            save(out/name,read(source/name))
        save(out/'primary-cleanup.json',read(source/'cleanup.json'))
        save(out/'primary-speech-entry-cleanup.json',read(source/'primary-speech-entry-cleanup.json'))
        plan['sources'].update({str(source/name):digest
            for name,digest in profile_speech_primary.FILES.items()})
    check_plan(plan); save(out / 'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=bound)))


class Stage(prior.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = SUCCESS

    def __init__(self, plan):
        check_plan(plan)
        self.limits = limits(plan)
        encrypted.Stage.__init__(self, plan)
        self.counts.update(websocket_sessions=0, ingress_starts=0)

    def profile_business(self):
        caps = read(self.out / (self.profile_name + '-capabilities.json'))
        require(profile_evidence.speech_enabled(caps), 'actual speech capability absent')
        _, old = original(self.plan, self.profile_name)
        tts = copy.deepcopy(old['business']['tts'])
        tts['chat_response'] = self.request('shared-chat-speech', self.plan['requests']['chat'])
        status, raw = self.http('/v1/config', label=self.profile_name + '-tts-health')
        require(status == 200, 'fresh TTS health missing')
        tts['health'] = json.loads(raw)['native_tts_health']
        fresh = dict(tts=tts, realtime_speech=profile_speech.run(self))
        save(self.out / (self.profile_name + '-fresh.json'), fresh)
        value = copy.deepcopy(old['business']); value.update(fresh)
        profile_evidence.business(value, self.plan['identity'], self.plan['profiles'][self.profile_name], caps)

    def business(self):
        if 'retained_primary_business' not in self.plan:
            return full.Stage.business(self)
        require(self.profile_name == 'light', 'only unexecuted light work may continue')
        self.profile_business()
        profile_contract.capability_pair(read(self.out/'primary-capabilities.json'),
                                         read(self.out/'light-capabilities.json'))


def check_result(result, bound=LIMITS):
    require(result['status'] == SUCCESS and result['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        bound in (LIMITS,LIGHT_LIMITS) and
        type(result['seconds']) in (int,float) and 0 < result['seconds'] < bound['seconds'] and
        all(type(result['counts'].get(k)) is int and result['counts'][k] == v
            for k,v in bound.items() if k not in ('controls','seconds')) and
        type(result['counts'].get('controls')) is int and 0 <= result['counts']['controls'] <= bound['controls'],
        'speech work or bounded result incomplete')


def finalize(out):
    plan = read(out / 'plan.json'); check_plan(plan)
    result = read(out / 'result.json'); check_result(result,limits(plan))
    for name,digest in {**plan['sources'],**plan['protected'],**plan['native_assets']}.items():
        require(sha(ROOT / name) == digest, 'postflight source drift: ' + name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'candidate drift')
    full.bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out / 'lease'
    queries = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    cleanups = [read(out / n) for n in ('primary-cleanup.json','cleanup.json')]
    if 'retained_primary_business' in plan:
        from tools.serve_recovery import profile_speech_primary
        environment = profile_speech_primary.light_environment(read(lease/'exit.json'),queries,cleanups[-1])
        environment['lease_result_sha256'] = sha(lease/'exit.json')
        environment, proof = profile_speech_primary.environment(
            plan['retained_primary_business']['source'],environment)
        save(out/'environment-sources.json',proof)
        environment['lease_result_sha256'] = sha(out/'environment-sources.json')
    else:
        environment = full.environment(read(lease/'exit.json'), queries, cleanups)
        environment['lease_result_sha256'] = sha(lease/'exit.json')
    for name in profile_contract.NAMES:
        entry = read(out / (name + '-speech-entry-cleanup.json'))
        require(entry['exit_code'] == 0 and entry['port_released'] is True, 'entry not cleaned')
    guard = encrypted.foreground.base.lifecycle.guard
    processes = guard.processes()
    require(not any(processes.get(int(pid), {}).get('start_ticks') == row['start_ticks']
        for c in cleanups for pid,row in c['owned'].items()), 'owned process remains')
    full.background.probe_ports((plan['port'],plan['operations_port'],plan['speech']['public_port']))
    with guard.common_lock().open('r+') as lock: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    qualified = out / 'qualified'; qualified.mkdir(mode=0o700)
    registry = dict(format=profile_contract.FORMAT, identity=plan['identity'], profiles=plan['profiles'],
                    qualifications={})
    for name in profile_contract.NAMES:
        _, old = original(plan,name); config = read(out / (name+'-config.json'))
        business = copy.deepcopy(old['business']); fresh = read(out / (name+'-fresh.json'))
        require(set(fresh) == {'tts','realtime_speech'}, 'fresh scope incomplete')
        business.update(fresh)
        value = dict(format=profile_evidence.SPEECH_FORMAT, profile=name, identity=plan['identity'],
            capacity=plan['profiles'][name], capabilities=read(out/(name+'-capabilities.json')),
            configuration=contracts.configuration(config,version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],
            business=business, environment=environment)
        value = reuse.attach(value); path = qualified / (name+'.json'); profile_record.save(path,value)
        registry['qualifications'][name] = profile_contract.qualification(name,value['capacity'],
            value['identity'],value['capabilities'],value['configuration'],value['startup_memory'],sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path = qualified / (name+'.json')
        profile_evidence.validate(profile_record.read(path),registry,name,sha(path))
    save(qualified/'registry.json',registry)
    save(out/'acceptance.json',dict(status='ACCEPT_SPEECH_PRIMARY_LIGHT', environment=environment,
        counts=result['counts'],seconds=result['seconds'],registry_sha256=sha(qualified/'registry.json'),
        default_unchanged=True,managed_recovery_accepted=False,performance_claim=False,
        original_quality_failure_preserved=True,listening_deferred=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__); sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare')
    for key in ('binary','package','candidate-build','previous-registry','source-review','speech-reference','output'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--port',type=int,default=18468)
    p.add_argument('--continue-primary',type=Path)
    for mode in ('run','finalize'): sub.add_parser(mode).add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare': prepare(args)
    elif args.mode=='finalize': finalize(args.output.resolve())
    else:
        def interrupted(_signum,_frame): raise TimeoutError('speech stage deadline/interruption')
        signal.signal(signal.SIGTERM,interrupted); signal.signal(signal.SIGALRM,interrupted)
        plan=read(args.output/'plan.json')
        signal.alarm(limits(plan)['seconds'])
        try: Stage(plan).run()
        finally: signal.alarm(0)


if __name__=='__main__': main()
