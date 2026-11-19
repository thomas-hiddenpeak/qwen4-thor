"""Two capacities, fresh main-model consumers, unchanged backend provenance.

34 external model requests plus two CSV preparation steps and the existing
bounded semantic scoring steps; two model starts, eight WSS, 900 seconds.
No request replay, no full performance/quality or registration matrix.
"""
import argparse
import base64
import copy
import fcntl
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import contracts
from tools.serve_recovery import ledger, profile_acceptance as full
from tools.serve_recovery import profile_contract, profile_evidence, profile_record
from tools.serve_recovery import profile_conversation, profile_prompt, profile_realtime
from tools.serve_recovery import profile_semantic, profile_voice
from tools.serve_recovery import profile_prompt_targeted as targeted
from tools.serve_recovery import profile_refusal_reuse as reuse
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import prompt_acceptance as legacy
from tools.serve_service.package import read, require, save, sha

FORMAT = 'q4t-refusal-targeted-qualification-v1'
SUCCESS = 'PASS_TARGETED_REFUSAL_PRIMARY_LIGHT'
LIMITS = dict(starts=2, chats=34, controls=2000, seconds=900,
              websocket_sessions=8, ingress_starts=6, prompt_registry_requests=10)


def requests():
    rows = full.requests(True)
    return {k: rows[k] for k in ('schema', 'tool', 'media')}


def original(plan, name):
    registry = read(plan['previous_registry'])
    require(ledger.digest(registry) == reuse.REVIEW['previous_registry'],
            'original registry drift')
    path = Path(plan['previous_registry']).parent / (name + '.json')
    value = read(path)
    require(path.read_bytes() == ledger.canonical(value) + b'\n',
            'original evidence serialization drift')
    profile_evidence.validate(value, registry, name, sha(path))
    return registry, value


def voice_inputs(path):
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(ROOT / '.q4t-work') and
        sha(path) == reuse.VOICE_SNAPSHOT, 'qualified saved state required')
    snapshot = read(path); directory = path.parent / 'registered-state'
    require(snapshot['isolated_fixture'] is True and
        snapshot['consent'] == reuse.VOICE_CONSENT and snapshot['voice'] == reuse.VOICE_ID and
        directory.is_dir() and not directory.is_symlink() and
        {p.name for p in directory.iterdir()} == set(snapshot['files']),
        'saved state inventory or identity drift')
    total = 0; records = {}
    for name, digest in snapshot['files'].items():
        require(Path(name).name == name and name not in ('.', '..'), 'unsafe saved filename')
        item = directory / name
        require(item.is_file() and not item.is_symlink() and sha(item) == digest,
                'saved state bytes drift')
        total += item.stat().st_size
        require(total <= 16 << 20, 'saved state byte bound')
        for key, kind, identifier in (('consent', 'voice_consent', reuse.VOICE_CONSENT),
                                      ('voice', 'voice', reuse.VOICE_ID)):
            if name == reuse.VOICE_OWNER + '.' + kind + '.' + identifier + '.json':
                records[key] = read(item)['record']
    profile_evidence.voice_reference(records, reuse.VOICE_CONSENT, reuse.VOICE_ID)
    require(all(r['owner'] == reuse.VOICE_OWNER for r in records.values()), 'saved owner drift')
    return dict(manifest=str(path), sha256=sha(path), directory=str(directory),
                files=snapshot['files'], records=records)


def check_plan(plan):
    require(plan['targeted_format'] == FORMAT and plan['limits'] == LIMITS and
        ledger.canonical(plan['requests']) == ledger.canonical(requests()) and
        plan['automatic_retries'] is False and
        plan['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        plan['identity']['protocol_sha256'] == reuse.REVIEW['candidate_protocol'] and
        plan['planned'] == {k: v for k, v in LIMITS.items() if k not in ('controls', 'seconds')},
        'targeted frozen identity or work budget drift')
    review_path = Path(plan['source_review'])
    require(sha(review_path) == reuse.REVIEW['source_review'] and
        sha(review_path.with_name('runtime.diff')) == reuse.REVIEW['runtime_diff'],
        'audited runtime source drift')
    review = read(review_path)
    for name, digest in {**review['unchanged'],
            **{k: v['after'] for k, v in review['changed'].items()},
            **review['current_compiled_sources']}.items():
        require(sha(ROOT / name) == digest, 'compiled source drift: ' + name)
    require(plan['voice_snapshot'] == voice_inputs(plan['voice_snapshot']['manifest']),
            'saved voice fixture drift')
    for name in profile_contract.NAMES:
        registry, record = original(plan, name)
        require(plan['profiles'] == registry['profiles'] and
            {k: v for k, v in plan['identity'].items()
             if k not in ('binary_sha256', 'protocol_sha256')} ==
            {k: v for k, v in registry['identity'].items()
             if k not in ('binary_sha256', 'protocol_sha256')}, 'backend identity/capacity drift')
        require(all(ledger.canonical(plan['voice_snapshot']['records']['voice']['data'][k]) ==
            ledger.canonical(record['business']['voice']['conditioning']['voice']['data'][k])
            for k in reuse.CONDITION_FIELDS), 'saved voice does not match qualified condition')


def prepare(args):
    prior_path = args.previous_registry.resolve(strict=True)
    prior_plan = read(prior_path.parent.parent / 'plan.json')
    base_path = args.package.resolve(strict=True) / 'engine/manifest.json'
    base = read(base_path)
    require(base['settings'] == prior_plan['base']['settings'], 'retained settings drift')
    args.sdk_directory = Path(prior_plan['sdk']['sdk_directory'])
    encrypted.foreground.base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve(); plan = read(out / 'plan.json')
    base, candidate = full.candidate_build_base(base, args.binary, args.candidate_build)
    plan.update(targeted_format=FORMAT, base=base, candidate_identity=candidate,
        previous_registry=str(prior_path), source_review=str(args.source_review.resolve(strict=True)),
        profiles=copy.deepcopy(prior_plan['profiles']),
        identity=dict(prior_plan['identity'], binary_sha256=plan['binary_sha256'],
                      protocol_sha256=base['protocol_sha256']),
        native_assets=copy.deepcopy(prior_plan['native_assets']), chat_audio=True,
        semantic_vad=True, tts_sampling_profile=prior_plan['tts_sampling_profile'],
        planned={k: v for k, v in LIMITS.items() if k not in ('controls', 'seconds')},
        voice_snapshot=voice_inputs(args.voice_snapshot))
    plan['conversation'] = profile_conversation.prepare(args.conversation_reference,
        out, plan['port'] + 2, plan['sdk'])
    plan['prompt'] = profile_prompt.prepare(args.prompt_reference,
        out, plan['port'] + 3, plan['sdk'])
    plan['realtime'] = profile_realtime.prepare(Path(prior_plan['realtime']['source_plan']),
        out, plan['port'] + 4, plan['sdk'])
    sources = [base_path, prior_path, prior_path.parent.parent / 'plan.json',
        Path(plan['source_review']), Path(plan['source_review']).with_name('runtime.diff'),
        ROOT / full.DOCUMENT, Path(plan['voice_snapshot']['manifest']),
        *(prior_path.parent / (name + '.json') for name in profile_contract.NAMES)]
    sources += [Path(candidate[k]['path']) for k in ('compiled_sources', 'build_cache')]
    for key in ('conversation', 'prompt', 'realtime'):
        plan['sources'].update(plan[key]['files'])
    sources += [Path(plan['voice_snapshot']['directory']) / n for n in plan['voice_snapshot']['files']]
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_realtime',
        'tools/serve_responses', 'tools/serve_ops'], cwd=ROOT, text=True).splitlines()
    for path in [*sources, *(ROOT / name for name in names)]:
        plan['sources'][str(path)] = sha(path)
    plan['sources'][full.DOCUMENT] = sha(ROOT / full.DOCUMENT)
    plan['protected'][str(base_path)] = sha(base_path)
    check_plan(plan); save(out / 'plan.json', plan)


class Stage(full.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = SUCCESS

    def __init__(self, plan):
        check_plan(plan)
        encrypted.Stage.__init__(self, plan)
        self.counts.update(websocket_sessions=0, ingress_starts=0, prompt_registry_requests=0)

    def client_policy(self):
        value = super().client_policy()
        require('audio' not in [r['id'] for r in value['clients']], 'saved client collision')
        value['clients'].append(dict(value['clients'][0], id='audio'))
        return value

    def start(self):
        if self.counts['starts'] == 0:
            selected = self.plan['voice_snapshot']
            require(voice_inputs(selected['manifest']) == selected, 'saved voice drift before copy')
            target = self.out / 'application-objects'; target.mkdir(mode=0o700)
            for name, digest in selected['files'].items():
                path = target / name
                shutil.copyfile(Path(selected['directory']) / name, path); path.chmod(0o600)
                require(sha(path) == digest, 'saved private copy drift')
        super().start()

    def voice_chat(self, previous):
        actor = object.__new__(profile_voice.Lifecycle)
        actor.stage, actor.requests = self, 0
        rows = profile_evidence.voice_requests(reuse.VOICE_CONSENT, reuse.VOICE_ID)
        chat = actor.accepted(actor.http('fresh-chat', '/v1/chat/completions', rows['chat'], client='audio'))
        stored = actor.accepted(actor.http('fresh-stored', '/v1/chat/completions/' + chat['id'], client='audio'))
        status, raw = self.http('/v1/config', label=self.profile_name + '-fresh-voice-health')
        require(status == 200, 'fresh voice health missing')
        health = json.loads(raw)['native_tts_base_health']
        audio = profile_evidence.message(chat, 'stop')['audio']['data']
        transcript = actor.accepted(actor.form('fresh-chat-asr', '/v1/audio/transcriptions',
            rows['transcription'], 'file', base64.b64decode(audio, validate=True), client='audio'))
        witness = dict(snapshot_sha256=reuse.VOICE_SNAPSHOT,
            records=self.plan['voice_snapshot']['records'], request=rows['chat'], chat=chat,
            stored_chat=stored, chat_transcript=transcript, health=health)
        require(actor.requests == 2, 'saved voice request budget drift')
        reuse.voice_business(witness, previous, self.plan['identity'])
        save(self.out / (self.profile_name + '-voice-prompt.json'), witness)
        return witness

    def profile_business(self):
        caps = read(self.out / (self.profile_name + '-capabilities.json'))
        _, old = original(self.plan, self.profile_name)
        fresh = targeted.Stage.text_business(self)
        fresh['prompt'] = profile_prompt.run(self)
        rows = self.plan['requests']
        fresh['media'] = dict(request=rows['media'],
            response=self.request('media', rows['media'], '/v1/responses'))
        source = old['business']['asr']['source_base64']
        audio = self.request('chat-audio', profile_evidence.chat_audio_request(source))
        status, raw = self.http('/v1/config', label=self.profile_name + '-chat-audio-config')
        require(status == 200, 'Chat audio configuration missing')
        fresh['chat_audio'] = profile_evidence.chat_audio_witness(source, audio, json.loads(raw))
        self.voice_chat(old['business']['voice'])
        tts = copy.deepcopy(old['business']['tts'])
        tts['chat_response'] = self.request('chat-output', profile_evidence.tts_requests()['chat'])
        status, raw = self.http('/v1/config', label=self.profile_name + '-tts-config')
        require(status == 200, 'TTS configuration missing')
        tts['health'] = json.loads(raw)['native_tts_health']
        fresh['tts'] = tts
        fresh['semantic_vad'] = profile_semantic.run(self)
        value = copy.deepcopy(old['business']); value.update(fresh)
        profile_evidence.business(value, self.plan['identity'], self.plan['profiles'][self.profile_name], caps)
        profile_semantic.save_receipt(self.out / (self.profile_name + '-fresh.json'), fresh)
        self.budget('prompt_registry_requests')
        definition = targeted.reuse.legacy_definition()
        registered = legacy.Stage.admin(self, self.profile_name + '-legacy-register',
            'POST', '/v1/prompts', dict(definition=definition))
        request = targeted.reuse.legacy_request(registered['id'])
        response = self.request('legacy-responses', request, '/v1/responses')
        witness = dict(definition=definition, registered=registered, request=request, response=response)
        targeted.reuse.legacy_business(witness)
        save(self.out / (self.profile_name + '-legacy.json'), witness)


def check_result(value):
    require(value['status'] == SUCCESS and value['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        type(value['seconds']) in (int, float) and 0 < value['seconds'] < LIMITS['seconds'] and
        all(type(value['counts'].get(k)) is int and value['counts'][k] == v
            for k, v in LIMITS.items() if k not in ('seconds', 'controls')) and
        type(value['counts'].get('controls')) is int and 0 <= value['counts']['controls'] <= LIMITS['controls'],
        'actual affected work or budget incomplete')


def finalize(out):
    plan = read(out / 'plan.json'); check_plan(plan)
    result = read(out / 'result.json'); check_result(result)
    for name, digest in {**plan['sources'], **plan['protected'], **plan['native_assets']}.items():
        require(sha(ROOT / name) == digest, 'postflight drift: ' + name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'candidate drift')
    full.bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out / 'lease'
    queries = [json.loads(line) for line in (lease / 'nvml-queries.jsonl').read_text().splitlines()]
    cleanups = [read(out / name) for name in ('primary-cleanup.json', 'cleanup.json')]
    environment = full.environment(read(lease / 'exit.json'), queries, cleanups)
    environment['lease_result_sha256'] = sha(lease / 'exit.json')
    guard = encrypted.foreground.base.lifecycle.guard; processes = guard.processes()
    require(not any(processes.get(int(pid), {}).get('start_ticks') == row['start_ticks']
        for cleanup in cleanups for pid, row in cleanup['owned'].items()), 'owned process remains')
    full.background.probe_ports((plan['port'], plan['operations_port'],
        *(plan[k]['public_port'] for k in ('conversation', 'prompt', 'realtime'))))
    with guard.common_lock().open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    qualified = out / 'qualified'; qualified.mkdir(mode=0o700)
    registry = dict(format=profile_contract.FORMAT, identity=plan['identity'],
        profiles=plan['profiles'], qualifications={})
    for name in profile_contract.NAMES:
        source_registry, source = original(plan, name)
        config = read(out / (name + '-config.json')); caps = read(out / (name + '-capabilities.json'))
        fresh = read(out / (name + '-fresh.json'))
        require(set(fresh) == reuse.FRESH | {'tts'}, 'actual main-model consumers incomplete')
        business = copy.deepcopy(source['business']); business.update(fresh)
        value = dict(format=profile_evidence.PROMPT_FORMAT, profile=name,
            identity=plan['identity'], capacity=plan['profiles'][name], capabilities=caps,
            configuration=contracts.configuration(config, version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],
            business=business, environment=environment)
        value = reuse.attach(value, source_registry, source, read(out / (name + '-legacy.json')),
                             read(out / (name + '-voice-prompt.json')))
        path = qualified / (name + '.json'); profile_record.save(path, value)
        registry['qualifications'][name] = profile_contract.qualification(name,
            value['capacity'], value['identity'], caps, value['configuration'], value['startup_memory'], sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path = qualified / (name + '.json')
        profile_evidence.validate(profile_record.read(path), registry, name, sha(path))
    save(qualified / 'registry.json', registry)
    save(out / 'acceptance.json', dict(status='ACCEPT_TARGETED_REFUSAL_PRIMARY_LIGHT',
        environment=environment, counts=result['counts'], seconds=result['seconds'],
        registry_sha256=sha(qualified / 'registry.json'), default_unchanged=True,
        managed_switching_accepted=False, performance_claim=False,
        original_failures_preserved=True, unchanged_standalone_backends_reused=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='mode', required=True)
    command = sub.add_parser('prepare')
    for name in ('binary', 'package', 'candidate-build', 'previous-registry', 'source-review',
                 'conversation-reference', 'prompt-reference', 'voice-snapshot', 'output'):
        command.add_argument('--' + name, type=Path, required=True)
    command.add_argument('--port', type=int, default=18438)
    for name in ('run', 'finalize'): sub.add_parser(name).add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    elif args.mode == 'finalize': finalize(args.output.resolve())
    else:
        def stopped(_s, _f): raise TimeoutError('targeted stage deadline/interruption')
        signal.signal(signal.SIGTERM, stopped); signal.signal(signal.SIGALRM, stopped); signal.alarm(900)
        try:
            with encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(read(args.output.resolve() / 'plan.json')).run()
        finally: signal.alarm(0)


if __name__ == '__main__': main()
