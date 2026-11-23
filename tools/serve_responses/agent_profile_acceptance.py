"""Qualify hosted agent ownership on the actual primary and light capacities."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

EVIDENCE = ROOT/'docs/evidence/serve-responses-hosted-agents-20261123/cross-round.json'


def prepare(output, build, sdk_run):
    from tools.serve_live import profile_acceptance
    approved = pkg.read(EVIDENCE)['selected']
    sdk_plan = pkg.read(sdk_run/'plan.json')
    sdk = pkg.read(sdk_run/'sdk-result.json')
    pkg.require(sdk == approved['sdk'] and sdk['status'] == 'PASS' and
                pkg.read(sdk_run/'result.json') == approved['run'] and
                all(pkg.sha(sdk_run/name) == digest for name, digest in approved['wire_sha256'].items()),
                'hosted SDK observations differ from acceptance')
    selected_doc = ROOT/'docs/evidence/serve-responses-beta-delivery-20261123/delivery.json'
    current_digest = pkg.read(selected_doc)['publication']['primary']
    current_dir = ROOT/'build/service'
    manifest = bundle.verify(current_dir, current_digest)
    engine = pkg.verify(current_dir/'engine', manifest['engine_manifest_sha256'])
    sources = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    binary = pkg.sha(build/'q4t')
    pkg.require(current_digest == sdk_plan['protected']['build/service/manifest.json'] and
                binary == sdk_plan['binary'] == approved['engine'] and
                sdk_plan['ingress'] == approved['ingress'] and
                all(pkg.sha(ROOT/name) == digest for name, digest in sources.items()),
                'hosted SDK, build or installed identities differ')
    guard = {str(ROOT/'build'/name): pkg.sha(ROOT/'build'/name) for name in
             ('q4t', 'q4t.release.json', 'service/manifest.json', 'service.release.json',
              'service-native-state/ledger.json')}
    change = dict(source_manifest_sha256=current_digest, previous_protocol=engine['protocol_sha256'],
                  sdk_binary=binary, sdk_protocol=protocol(sources), protocol=protocol(sources))
    profile_acceptance.prepare_profiles(output, manifest, engine,
        dict(engine['live_workers'], engine_sha256=binary), approved['ingress'],
        protected=guard, beta_change=change, agent_change=dict(
            witness_sha256=pkg.sha(EVIDENCE), capabilities=approved['capabilities']))


def verify(phase, plan, capabilities):
    selection = plan['agent_change']
    pkg.require(selection['witness_sha256'] == pkg.sha(EVIDENCE) and
                selection['capabilities'] == capabilities['responses']['multi_agent'] ==
                pkg.read(EVIDENCE)['selected']['capabilities'], 'hosted capability or witness changed')
    response = pkg.read(phase/'agent-response.json')
    output = response['output']
    calls = [i for i in output if i['type'] == 'multi_agent_call']
    results = [i for i in output if i['type'] == 'multi_agent_call_output']
    child = [i for i in output if i.get('agent', {}).get('agent_name') == 'root/reader'
             and i['type'] == 'message']
    root = [i for i in output if i['type'] == 'message' and not i.get('agent')]
    pkg.require(response['status'] == 'completed' and response['usage']['output_tokens'] > 0 and
                [i['action'] for i in calls] == [i['action'] for i in results] ==
                ['spawn_agent', 'wait_agent'] and
                [i['call_id'] for i in calls] == [i['call_id'] for i in results] and
                len({i['call_id'] for i in calls}) == 2 and len(child) == len(root) == 1 and
                child[0]['content'][0]['text'].strip() == root[0]['content'][0]['text'].strip() == '37' and
                json.loads(results[-1]['output'][0]['text'])['output'].strip() == '37',
                'actual hosted child, root answer or tool association differs')
    files = ['agent-response.json']
    if plan['name'] == 'light':
        previous = pkg.read(phase.parent/'primary/agent-response.json')
        saved = pkg.read(phase/'agent-restored.json')
        pkg.require(saved['response']['id'] == previous['id'] and
                    saved['response']['output'] == previous['output'] and saved['other_status'] == 404,
                    'hosted response continuity or ownership failed')
        files.append('agent-restored.json')
    return {name: pkg.sha(phase/name) for name in files}


def business(stage, capabilities):
    prompt = ('First use multi_agent_spawn_agent with name "reader" and task '
              '"Reply only with the decimal integer equal to 19 plus 18. Do not delegate." '
              'Then use multi_agent_wait_agent for root/reader. Finally reply only with '
              'the number reported by that worker.')
    body = dict(model='qwen3.8-flash-next', reasoning=dict(effort='none'), input=prompt,
                max_output_tokens=384, temperature=0, store=True,
                multi_agent=dict(enabled=True, max_concurrent_subagents=1))
    status, response = stage.api('POST', '/v1/responses?beta=true', body=body, agent_beta=True)
    pkg.save(stage.out/'agent-response.json', response)
    pkg.require(status == 200, 'hosted response status '+str(status))
    if stage.plan['name'] == 'light':
        previous = pkg.read(stage.out.parent/'primary/agent-response.json')
        route = '/v1/responses/'+previous['id']+'?beta=true&include[]=message.output_text.logprobs'
        own, saved = stage.api('GET', route, agent_beta=True)
        other, _ = stage.api('GET', route, client='gamma', agent_beta=True)
        pkg.save(stage.out/'agent-restored.json', dict(response=saved, other_status=other))
        pkg.require(own == 200, 'hosted response read after capacity change')
    verify(stage.out, stage.plan, capabilities)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--sdk-run', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'private evidence directory')
    prepare(out, args.build.resolve(), args.sdk_run.resolve())
