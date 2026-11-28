"""Qualify native and selector-scoped Responses producers on each service capacity."""
import json
from pathlib import Path

from tools.serve_recovery import profile_live_reuse as history, profile_record
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
KEY = 'responses_producer_handoff'
RECEIPT = 'responses-producer-handoff.json'
PROOF = ROOT/'docs/evidence/serve-interpreter-output-discovery-20261128/device.json'
INPUTS = (Path(__file__), PROOF)
PYTHON_TYPE = 'code_interpreter_call'
BETA_TYPES = ('multi_agent_call', 'multi_agent_call_output')


def extended_responses(previous):
    """Preserve the exact old declaration and add only the three observed types."""
    items = list(previous['output_items'])
    agent = previous['multi_agent']
    package.require(PYTHON_TYPE not in items and 'output_items' not in agent and
                    history.valid_multi_agent(agent) and not set(items).intersection(BETA_TYPES),
                    'unexpected predecessor Responses declaration')
    items.insert(items.index('web_search_call') + 1, PYTHON_TYPE)
    return dict(previous, output_items=items,
                multi_agent=dict(agent, output_items=list(BETA_TYPES)))


def prepare(out, build, deployment):
    from tools.serve_responses import environment_lifecycle_profiles as lifecycle

    proof = package.read(PROOF)
    package.require(proof['status'] == 'PASS' and proof['guard']['status'] == 'COMPLETED'
                    and proof['engine'] == package.sha(build/'q4t')
                    and proof['ingress'] == package.sha(build/'q4t-ingress'),
                    'producer qualification requires the short-tested engine and gateway')
    lifecycle.prepare_from_manifest(out, build, deployment, proof['predecessor_manifest'],
                                    history.OVERLAP_DISCOVERY_REVIEWED, KEY, INPUTS)
    for name in ('primary', 'light'):
        path = out/name/'plan.json'
        plan = package.read(path)
        plan['limits']['producer_responses'] = 2
        package.save(path, plan)


def check(declaration, row):
    ordinary = set(declaration['output_items'])
    beta = declaration['multi_agent']
    package.require(PYTHON_TYPE in ordinary and not ordinary.intersection(BETA_TYPES)
                    and history.valid_multi_agent(beta) and beta.get('output_items') == list(BETA_TYPES),
                    'ordinary or selector-scoped output types differ')
    interpreter, agents = row['interpreter'], row['agents']
    package.require(interpreter['id'] != agents['id'] and
                    all(response['status'] == 'completed' and response['id'].startswith('resp_')
                        for response in (interpreter, agents)), 'expected two independent completed responses')
    output = interpreter['output']
    calls = [i for i in output if i['type'] == PYTHON_TYPE]
    package.require(all(i['type'] in ordinary for i in output) and len(calls) == 1
                    and calls[0]['container_id'] == row['container'] and calls[0]['status'] == 'completed'
                    and calls[0].get('id') and calls[0]['outputs'] == [dict(type='logs', logs='603\n')],
                    'interpreter result, stdout or container identity differs')
    answer = ''.join(part['text'] for i in output if i['type'] == 'message'
                     for part in i['content'] if part['type'] == 'output_text')
    package.require(answer.strip() == '603', 'interpreter answer differs')
    output = agents['output']
    calls = [i for i in output if i['type'] == BETA_TYPES[0]]
    results = [i for i in output if i['type'] == BETA_TYPES[1]]
    package.require(all(i['type'] in ordinary.union(BETA_TYPES) for i in output)
                    and len(calls) == len(results) == 1 and calls[0].get('call_id')
                    and calls[0]['call_id'] == results[0]['call_id']
                    and calls[0]['action'] == results[0]['action'] == 'list_agents',
                    'agent result missing or attributed to a different call')
    text = [json.loads(p['text']) for p in results[0]['output'] if p['type'] == 'output_text']
    package.require(len(text) == 1 and any(a['agent_name'] == 'root' for a in text[0]['agents']),
                    'list_agents did not return the actual root agent')


def business(stage, capabilities):
    def post(path, body, *, beta=False):
        status, result = stage.api('POST', path, body=body, agent_beta=beta, timeout=90)
        package.require(status == 200, 'producer HTTP status '+str(status))
        return result

    stage.same_model()
    container = post('/v1/containers', dict(name='producer-discovery'))['id']
    common = dict(model=capabilities['model'], temperature=0, reasoning=dict(effort='none'),
                  max_output_tokens=256, max_tool_calls=1, store=True)
    stage.budget('producer_responses')
    interpreter = post('/v1/responses', dict(common,
        input='Use the Python interpreter to execute exactly print(602 + 1). '
              'After executing, respond with only the printed number.',
        tools=[dict(type='code_interpreter', container=container)],
        tool_choice=dict(type='code_interpreter'), include=['code_interpreter_call.outputs']))
    stage.budget('producer_responses')
    agents = post('/v1/responses?beta=true', dict(common,
        input='First call multi_agent_list_agents with an empty object to list the actual agents. '
              'Then reply with the name of the root agent.',
        multi_agent=dict(enabled=True, max_concurrent_subagents=1)), beta=True)
    row = dict(container=container, interpreter=interpreter, agents=agents)
    package.save(stage.out/RECEIPT, row)
    check(capabilities['responses'], row)
    stage.same_model()


def verify(phase, plan, capabilities, current):
    change = plan[KEY]
    previous_file = phase/'previous-profile.json'
    previous = profile_record.read(previous_file)
    proof = package.read(PROOF)
    before = previous['capabilities']
    expected = dict(before, protocol_source_sha256=change['protocol'],
                    responses=extended_responses(before['responses']))
    package.require(plan['limits']['producer_responses'] == 2
                    and package.read(phase/'result.json')['counts']['producer_responses'] == 2
                    and proof['status'] == 'PASS' and proof['guard']['status'] == 'COMPLETED'
                    and package.same_json(expected, capabilities)
                    and package.same_json(plan['capacity'], previous['capacity'])
                    and package.sha(previous_file) == change['previous'] == history.OVERLAP_DISCOVERY_REVIEWED[plan['name']]
                    and plan['selected_sha256'] == change['release'] == proof['predecessor_manifest']
                    and plan['binary_sha256'] == proof['engine'] and plan['ingress_sha256'] == proof['ingress']
                    and all(plan['sources'][str(p.relative_to(ROOT))] == package.sha(p) for p in INPUTS),
                    'producer qualification changed the source, capacity, program or prior contract')
    row = package.read(phase/RECEIPT)
    check(capabilities['responses'], row)
    if plan['name'] == 'light':
        primary = package.read(phase.parent/'primary'/RECEIPT)
        package.require(not {row[k]['id'] for k in ('agents','interpreter')}.intersection(
                            primary[k]['id'] for k in ('agents','interpreter'))
                        and row['container'] != primary['container'], 'capacity reused a prior result')
    evidence = previous['business']['live']['evidence_sha256']
    package.require(history.OVERLAP_DISCOVERY_RECEIPT in evidence, 'missing prior capacity evidence')
    return {**{k: v for k, v in evidence.items() if k not in current},
            RECEIPT: package.sha(phase/RECEIPT)}
