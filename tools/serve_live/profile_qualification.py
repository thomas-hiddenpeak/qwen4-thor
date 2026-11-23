"""Bind retained Live business to the capacities and binaries a package can use."""
import json
from pathlib import Path

from tools.serve_ops import contracts
from tools.serve_recovery import profile_acceptance, profile_contract
from tools.serve_service import package


def observations(directory, accepted):
    """Recheck both actual HTTP observations and the accepted deployment actors."""
    directory = Path(directory)
    environment = profile_acceptance.environment(package.read(directory/'lease/exit.json'),
        [json.loads(line) for line in (directory/'lease/nvml-queries.jsonl').read_text().splitlines()],
        [package.read(directory/name/'cleanup.json') for name in profile_contract.NAMES])
    package.require(package.same_json(accepted['environment'], environment) and
                    len(accepted['results']) == len(profile_contract.NAMES) and
                    set(accepted['profiles']) == set(profile_contract.NAMES),
                    'Live qualification differs from the monitored two-profile business run')
    profiles = {}
    capabilities = []
    for i, name in enumerate(profile_contract.NAMES):
        phase = directory/name
        plan = package.read(phase/'plan.json')
        result = package.read(phase/'result.json')
        observed = package.read(phase/'observation.json')
        config = package.read(phase/'configuration.wire')
        declared = package.read(phase/'capabilities.json')
        caps = plan['capacity']
        protocol = config['protocol_source_sha256']
        binary = accepted['engine_sha256']
        workers = package.accepted_live_workers(plan, binary)
        package.require(plan['name'] == observed['profile'] == name and
                        package.same_json(result, accepted['results'][i]) and
                        result['status'] == 'PASS' and result['default_unchanged'] and
                        all(result['counts'][k] == 1 for k in
                            ('starts', 'ingress_starts', 'sdk_posts', 'chats')) and
                        result['binary_sha256'] == plan['binary_sha256'] == binary and
                        package.sha(phase/'engine/q4t') == binary and
                        Path(plan['binary']).resolve() == (phase/'engine/q4t').resolve() and
                        package.sha(plan['ingress']) == plan['ingress_sha256'] ==
                        accepted['ingress_sha256'], 'Live profile run or binary identity differs')
        package.require(package.same_json(observed, accepted['profiles'][name]) and
                        package.same_json(caps, observed['capacity']) and
                        declared['protocol_source_sha256'] == observed['protocol'] == protocol and
                        observed['worker_files'] == workers['files'] and
                        config['live_worker_profile'] == workers['profile'] and
                        observed['mtp_enabled'] is caps['mtp_enabled'] and
                        observed['recordings_enabled'] is True and
                        config['application_state']['live_recordings'] is True,
                        'Live profile observation differs')
        for filename, digest in workers['files'].items():
            package.read_live_worker(phase/'engine'/filename, digest)
        chat = package.read(phase/'core-after.wire')['choices'][0]
        package.require(chat['message']['content'].strip() == observed['returned_chat'] == '42'
                        and chat['finish_reason'] == 'stop' and observed['media_created'] == 201
                        and observed['session_closed'] is True and observed['conversation'] ==
                        ('created' if name == 'primary' else 'recovered'),
                        'Live SDK, stored conversation or Chat result differs')
        classified = contracts.configuration(config, version=5)
        profile_contract.configuration(caps, classified)
        memory = config['startup_memory']['checks']['before_allocations']
        profile_contract.startup_memory(memory)
        capabilities.append(declared)
        profiles[name] = dict(capacity=caps, configuration=classified, startup_memory=memory,
            workers=workers, protocol_sha256=protocol,
            evidence_sha256={p: package.sha(phase/p) for p in (
                'configuration.wire', 'capabilities.json', 'core-after.wire',
                'observation.json', 'result.json', 'cleanup.json', 'plan.json')})
        if 'beta_query_selector' in declared.get('responses', {}) or 'beta_change' in plan:
            from tools.serve_responses import beta_profile_acceptance
            proofs = beta_profile_acceptance.verify(phase, plan, declared)
            profiles[name]['evidence_sha256'].update(proofs)
        if 'multi_agent' in declared.get('responses', {}) or 'agent_change' in plan:
            from tools.serve_responses import agent_profile_acceptance
            profiles[name]['evidence_sha256'].update(
                agent_profile_acceptance.verify(phase, plan, declared))
        if 'agent_interruption' in plan:
            from tools.serve_responses import interruption_profile_acceptance
            profiles[name]['evidence_sha256'].update(
                interruption_profile_acceptance.verify(phase, plan))
        if 'agent_registration' in declared or 'agent_registration' in plan:
            from tools.serve_responses import registration_profile_acceptance
            profiles[name]['evidence_sha256'].update(
                registration_profile_acceptance.verify(phase, plan, declared))
    profile_contract.capability_pair(*capabilities)
    package.require(profiles['primary']['workers'] == profiles['light']['workers'] and
                    profiles['primary']['protocol_sha256'] == profiles['light']['protocol_sha256'],
                    'Live profiles would install different runtime identities')
    return dict(format='q4t-live-profile-observations-v1', engine_sha256=accepted['engine_sha256'],
                ingress_sha256=accepted['ingress_sha256'], profiles=profiles)
