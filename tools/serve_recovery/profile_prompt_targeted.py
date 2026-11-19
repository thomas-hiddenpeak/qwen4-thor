"""Exact-build Prompt qualification: fresh affected work, visible prior backends.

Two model starts, twenty generations and ten registry operations, no retries.
This does not qualify managed recovery or performance. The strict lease owns
both models, and evidence is constructed only after its successful exit.
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
from tools.serve_recovery import profile_conversation, profile_prompt
from tools.serve_recovery import profile_prompt_reuse as reuse
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_responses import prompt_acceptance as legacy
from tools.serve_service.package import read, require, save, sha

FORMAT = 'q4t-prompt-targeted-qualification-v1'
SUCCESS = 'PASS_TARGETED_PROMPT_PRIMARY_LIGHT'
LIMITS = dict(starts=2, chats=20, controls=2000, seconds=900,
              websocket_sessions=4, ingress_starts=4, prompt_registry_requests=10)


def requests():
    rows = full.requests()
    return {name: rows[name] for name in ('schema', 'tool')}


def original(plan, name):
    registry = read(plan['previous_registry'])
    require(ledger.digest(registry) == reuse.REVIEW['previous_registry'],
            'original qualified registry changed')
    path = Path(plan['previous_registry']).parent / (name + '.json')
    value = read(path)
    require(path.read_bytes() == ledger.canonical(value) + b'\n',
            'original qualification serialization differs')
    profile_evidence.validate(value, registry, name, sha(path))
    return registry, value


def check_plan(plan):
    require(plan['targeted_format'] == FORMAT and plan['limits'] == LIMITS and
        ledger.canonical(plan['requests']) == ledger.canonical(requests()) and
        plan['automatic_retries'] is False and
        plan['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        plan['identity']['protocol_sha256'] == reuse.REVIEW['candidate_protocol'],
        'targeted frozen identity or work budget differs')
    review_path = Path(plan['source_review'])
    require(sha(review_path) == reuse.REVIEW['source_review'] and
        sha(review_path.with_name('runtime.diff')) == reuse.REVIEW['runtime_diff'],
        'audited runtime source review changed')
    review = read(review_path)
    sources = {**review['unchanged'],
        **{k: v['after'] for k, v in review['changed'].items()},
        **review['current_compiled_sources']}
    for name, digest in sources.items():
        require(sha(ROOT / name) == digest, 'audited runtime source drift: ' + name)
    for name in profile_contract.NAMES:
        registry, previous = original(plan, name)
        require(plan['profiles'] == registry['profiles'] and
            {k: v for k, v in plan['identity'].items()
             if k not in ('binary_sha256', 'protocol_sha256')} ==
            {k: v for k, v in registry['identity'].items()
             if k not in ('binary_sha256', 'protocol_sha256')} and
            previous['identity']['binary_sha256'] == reuse.REVIEW['previous_binary'],
            'original backend/model/capacity binding differs')
    require(plan['planned'] == {k: v for k, v in LIMITS.items()
        if k not in ('controls', 'seconds')}, 'planned affected work differs')


def prepare(args):
    previous_path = args.previous_registry.resolve(strict=True)
    previous_plan = read(previous_path.parent.parent / 'plan.json')
    base_path = args.package.resolve(strict=True) / 'engine/manifest.json'
    base = read(base_path)
    require(base['settings'] == previous_plan['base']['settings'],
            'retained settings changed beyond the audited qualification')
    if args.sdk_directory is None:
        args.sdk_directory = Path(previous_plan['sdk']['sdk_directory'])
    encrypted.foreground.base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    out = args.output.resolve(); plan = read(out / 'plan.json')
    base, candidate = full.candidate_build_base(base, args.binary, args.candidate_build)
    plan.update(targeted_format=FORMAT, base=base, candidate_identity=candidate,
        previous_registry=str(previous_path), source_review=str(args.source_review.resolve(strict=True)),
        profiles=copy.deepcopy(previous_plan['profiles']),
        identity=dict(previous_plan['identity'], binary_sha256=plan['binary_sha256'],
                      protocol_sha256=base['protocol_sha256']),
        native_assets=copy.deepcopy(previous_plan['native_assets']), chat_audio=True,
        planned={k: v for k, v in LIMITS.items() if k not in ('controls', 'seconds')})
    require(plan['model_assets'] == plan['identity']['model_assets'] and
        plan['model'] == base['model_dir'] and not base['settings']['application_state']['directory'],
        'qualification assets or private object directory differs')
    plan['conversation'] = profile_conversation.prepare(args.conversation_reference,
        out, plan['port'] + 2, plan['sdk'])
    plan['prompt'] = profile_prompt.prepare(args.prompt_reference,
        out, plan['port'] + 3, plan['sdk'])
    sources = [base_path, previous_path, previous_path.parent.parent / 'plan.json',
        Path(plan['source_review']), Path(plan['source_review']).with_name('runtime.diff'),
        ROOT / full.DOCUMENT, *(previous_path.parent / (n + '.json') for n in profile_contract.NAMES)]
    sources += [Path(candidate[k]['path']) for k in ('compiled_sources', 'build_cache')]
    for key in ('conversation', 'prompt'):
        plan['sources'].update(plan[key]['files'])
    names = subprocess.check_output(['git', 'ls-files', 'tools/serve_realtime',
        'tools/serve_responses', 'tools/serve_ops'], cwd=ROOT, text=True).splitlines()
    for path in [*sources, *(ROOT / n for n in names)]:
        plan['sources'][str(path)] = sha(path)
    plan['sources'][full.DOCUMENT] = sha(ROOT / full.DOCUMENT)
    plan['protected'][str(base_path)] = sha(base_path)
    check_plan(plan)
    save(out / 'plan.json', plan)


class Stage(full.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = SUCCESS

    def __init__(self, plan):
        check_plan(plan)
        encrypted.Stage.__init__(self, plan)
        self.counts.update(websocket_sessions=0, ingress_starts=0, prompt_registry_requests=0)

    def profile_business(self):
        capability = read(self.out / (self.profile_name + '-capabilities.json'))
        require(profile_evidence.prompt_enabled(capability), 'actual Prompt capability absent')
        _, previous = original(self.plan, self.profile_name)
        rows = self.plan['requests']
        schema = self.request('schema', rows['schema'])
        call = self.request('tool', rows['tool'])
        message = profile_evidence.message(call, 'tool_calls')
        calls = message.get('tool_calls', [])
        require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=full.DOCUMENT),
            'unsafe or different actual tool request')
        source = dict(path=full.DOCUMENT, content=(ROOT / full.DOCUMENT).read_text())
        digest = sha(ROOT / full.DOCUMENT)
        require(digest == self.plan['sources'][full.DOCUMENT], 'actual document drift')
        reply = dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(dict(
            path=full.DOCUMENT, sha256=digest, answer=digest[:12])))
        follow = copy.deepcopy(rows['tool']); follow['tool_choice'] = 'none'
        follow['messages'] += [message, reply]
        result = self.request('tool-result', follow)
        fresh = dict(schema=schema, tool_call=call, tool_result=result,
            tool_reply=reply, tool_source=source,
            conversation=profile_conversation.run(self), prompt=profile_prompt.run(self))
        value = {k: copy.deepcopy(v) for k, v in previous['business'].items() if k not in reuse.FRESH}
        value.update(fresh)
        profile_evidence.business(value, self.plan['identity'],
                                 self.plan['profiles'][self.profile_name], capability)
        # Only fresh work is written here. The final record explicitly retains
        # the original backend record instead of relabeling it as this process.
        save(self.out / (self.profile_name + '-fresh.json'), fresh)
        self.budget('prompt_registry_requests')
        definition = reuse.legacy_definition()
        registered = legacy.Stage.admin(self, self.profile_name + '-legacy-register',
            'POST', '/v1/prompts', dict(definition=definition))
        request = reuse.legacy_request(registered['id'])
        response = self.request('legacy-responses', request, '/v1/responses')
        witness = dict(definition=definition, registered=registered, request=request, response=response)
        reuse.legacy_business(witness)
        save(self.out / (self.profile_name + '-legacy.json'), witness)


def check_result(result):
    require(result['status'] == SUCCESS and result['binary_sha256'] == reuse.REVIEW['candidate_binary'] and
        type(result['seconds']) in (int, float) and 0 < result['seconds'] < LIMITS['seconds'] and
        all(type(result['counts'].get(k)) is int and result['counts'][k] == v
            for k, v in LIMITS.items() if k not in ('seconds', 'controls')) and
        type(result['counts'].get('controls')) is int and 0 <= result['counts']['controls'] <= LIMITS['controls'],
        'targeted actual work or bounded result incomplete')


def finalize(out):
    plan = read(out / 'plan.json'); check_plan(plan)
    result = read(out / 'result.json'); check_result(result)
    for name, digest in {**plan['sources'], **plan['protected'], **plan['native_assets']}.items():
        require(sha(ROOT / name) == digest, 'postflight identity drift: ' + name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'candidate engine drift')
    full.bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out / 'lease'
    queries = [json.loads(line) for line in (lease / 'nvml-queries.jsonl').read_text().splitlines()]
    cleanups = [read(out / n) for n in ('primary-cleanup.json', 'cleanup.json')]
    environment = full.environment(read(lease / 'exit.json'), queries, cleanups)
    environment['lease_result_sha256'] = sha(lease / 'exit.json')
    guard = encrypted.foreground.base.lifecycle.guard
    processes = guard.processes()
    require(not any(processes.get(int(pid), {}).get('start_ticks') == item['start_ticks']
        for cleanup in cleanups for pid, item in cleanup['owned'].items()), 'owned process still running')
    full.background.probe_ports((plan['port'], plan['operations_port'],
        plan['conversation']['public_port'], plan['prompt']['public_port']))
    with guard.common_lock().open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    qualified = out / 'qualified'; qualified.mkdir(mode=0o700)
    registry = dict(format=profile_contract.FORMAT, identity=plan['identity'],
        profiles=plan['profiles'], qualifications={})
    for name in profile_contract.NAMES:
        previous_registry, previous = original(plan, name)
        config = read(out / (name + '-config.json'))
        capability = read(out / (name + '-capabilities.json'))
        business = {k: copy.deepcopy(v) for k, v in previous['business'].items() if k not in reuse.FRESH}
        fresh = read(out / (name + '-fresh.json'))
        require(set(fresh) == reuse.FRESH, 'actual fresh scope incomplete')
        business.update(fresh)
        value = dict(format=profile_evidence.PROMPT_FORMAT, profile=name,
            identity=plan['identity'], capacity=plan['profiles'][name], capabilities=capability,
            configuration=contracts.configuration(config, version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],
            business=business, environment=environment)
        value = reuse.attach(value, previous_registry, previous, read(out / (name + '-legacy.json')))
        path = qualified / (name + '.json'); profile_record.save(path, value)
        registry['qualifications'][name] = profile_contract.qualification(name,
            value['capacity'], value['identity'], value['capabilities'], value['configuration'],
            value['startup_memory'], sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path = qualified / (name + '.json')
        profile_evidence.validate(profile_record.read(path), registry, name, sha(path))
    save(qualified / 'registry.json', registry)
    save(out / 'acceptance.json', dict(status='ACCEPT_TARGETED_PROMPT_PRIMARY_LIGHT_QUALIFICATION',
        environment=environment, counts=result['counts'], seconds=result['seconds'],
        registry_sha256=sha(qualified / 'registry.json'), default_unchanged=True,
        automatic_managed_switching_accepted=False, performance_claim=False,
        original_quality_failure_preserved=True, reused_backend_keys=sorted(reuse.REUSABLE)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_parser = sub.add_parser('prepare')
    for key in ('binary', 'package', 'candidate-build', 'previous-registry', 'source-review',
                'conversation-reference', 'prompt-reference', 'output'):
        prepare_parser.add_argument('--' + key, type=Path, required=True)
    prepare_parser.add_argument('--port', type=int, default=18438)
    prepare_parser.add_argument('--sdk-directory', type=Path)
    for mode in ('run', 'finalize'):
        sub.add_parser(mode).add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    elif args.mode == 'finalize': finalize(args.output.resolve())
    else:
        def interrupted(_signum, _frame): raise TimeoutError('targeted stage deadline/interruption')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGALRM, interrupted)
        signal.alarm(LIMITS['seconds'])
        try: Stage(read(args.output / 'plan.json')).run()
        finally: signal.alarm(0)


if __name__ == '__main__': main()
