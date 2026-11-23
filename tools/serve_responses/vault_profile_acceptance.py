"""Qualify stored Vault credentials on the two actual managed capacities."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from contextlib import ExitStack

from tools.serve_responses import mcp_business_peer as mcp
from tools.serve_responses.vault_oauth_fixture import TokenIssuer
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT/'docs/evidence/serve-agent-vaults-20261123/mcp.json'
PUBLICATION = ROOT/'docs/evidence/serve-agent-sessions-20261123/delivery.json'
OAUTH_EVIDENCE = EVIDENCE.with_name('oauth-session.json')
OAUTH_PUBLICATION = EVIDENCE.with_name('delivery.json')
OAUTH_ISSUER = 'tools/serve_responses/vault_oauth_fixture.py'
SDK = 'tools/serve_responses/agent_vault_handoff_sdk.mjs'
HELPER = 'tools/serve_responses/vault_profile_acceptance.py'
PEER = 'tools/serve_responses/mcp_business_peer.py'
SESSION_MCP = dict(connection_origin='service', transport='http',
                   vault_auth=['static_bearer', 'mcp_oauth_access_token'])


def candidate(build, current, renewal=False):
    source = OAUTH_EVIDENCE if renewal else EVIDENCE
    proof = pkg.read(source)
    actor = pkg.sha(build/'q4t')
    if renewal:
        pkg.require(proof['business']['status'] == 'PASS' and proof['sdk']['calls'] == 1
                    and proof['issuer']['distinct_bearers'] == 3 and proof['issuer']['no_replay']
                    and proof['protocol'] == protocol(current) and proof['engine'] == actor,
                    'managed renewal requires the tested OAuth session engine')
    else:
        pkg.require(proof['accepted']['status'] == 'PASS' and proof['business']['calls'] == 1
                    and proof['engine_sha256'] == actor, 'managed qualification needs the tested MCP engine')
    pkg.require(protocol(current).encode() in (build/'q4t').read_bytes(), 'compiled protocol differs')
    result = dict(receipt_sha256=pkg.sha(source), binary=actor, protocol=protocol(current),
                  ingress=proof['ingress' if renewal else 'ingress_sha256'])
    if renewal:
        result['renewal'] = True
    return result


def prepare_peer(out, renewal=False):
    folder = out/'mcp'
    folder.mkdir(mode=0o700)
    with ExitStack() as sockets:
        ports = []
        for _ in range(2 if renewal else 1):
            sock = sockets.enter_context(socket.socket())
            sock.bind(('127.0.0.1', 0))
            ports.append(sock.getsockname()[1])
    tls = mcp.prepare_tls(folder)
    peer = dict(port=ports[0], tls=tls, bearer=secrets.token_urlsafe(32))
    if renewal:
        peer.update(issuer_port=ports[1], issuer_credentials=TokenIssuer.credentials())
    pkg.save(folder/'peer.json', peer)
    pkg.save(folder/'policy.json', dict(version=1, targets=[], fetch=dict(public_networks=False,
        allow_http=False, allowed_networks=['127.0.0.1/32','::1/128'], ports=ports,
        ca_file=tls['ca_certificate'], timeout_ms=5000, max_redirects=0)))
    return {str(p.relative_to(out)):pkg.sha(p) for p in
            [folder/'peer.json',folder/'policy.json',*(Path(v) for v in tls.values())]}


def verify_identity(plan, capabilities):
    from tools.serve_responses.session_profile_acceptance import CAPABILITY
    witness = plan['agent_vaults']
    renewal = witness.get('renewal') is True
    evidence = OAUTH_EVIDENCE if renewal else EVIDENCE
    sources = [SDK, HELPER, PEER, str((OAUTH_PUBLICATION if renewal else PUBLICATION).relative_to(ROOT))]
    if witness.get('environment_templates'):
        from tools.serve_responses import environment_profile_acceptance as environments
        pkg.require(renewal, 'template handoff requires the renewable Vault profile')
        evidence = environments.EVIDENCE
        sources = [SDK, HELPER, PEER, *environments.SOURCES]
        if witness.get('hosted_workspaces'):
            evidence = environments.WORKSPACE_PROOF
            sources.extend(environments.WORKSPACE_SOURCES)
    methods = SESSION_MCP
    if renewal:
        sources.append(OAUTH_ISSUER)
        methods = dict(methods, vault_auth=[*methods['vault_auth'], 'mcp_oauth_refresh'])
    sessions = dict(CAPABILITY, tool_types=['function','mcp'], mcp=methods)
    if witness.get('hosted_workspaces'):
        sessions = dict(sessions, environments=['none','openai_hosted'])
    pkg.require(witness['receipt_sha256'] == pkg.sha(evidence) and
                plan['binary_sha256'] == witness['binary'] == pkg.sha(plan['binary']) ==
                pkg.read(evidence)['engine' if renewal else 'engine_sha256'] and
                plan['ingress_sha256'] == witness['ingress'] == pkg.sha(plan['ingress']) and
                capabilities.get('protocol_source_sha256') == witness['protocol'] and
                capabilities.get('agent_sessions') == sessions and
                capabilities.get('agent_vaults',{}).get('credentials') == dict(
                    auth=['mcp_oauth','static_bearer','environment_variable'],
                    write_only=True,rotation=True,archive_cascade=True) and
                all(plan['sources'].get(n) == pkg.sha(ROOT/n) for n in sources),
                'Vault profile identity differs')


def verify(phase, plan, capabilities):
    verify_identity(plan, capabilities)
    out = phase.parent
    selected = plan['agent_vaults']
    pkg.require(plan['command'][-2:] == ['--mcp-policy-file',str(out/'mcp/policy.json')]
                and all(k and (out/k).resolve().is_relative_to(out/'mcp')
                        and pkg.sha(out/k) == h for k,h in selected['fixture'].items()),
                'private peer policy or PKI changed')
    name = 'vault-handoff.json'
    row = pkg.read(phase/name)
    pkg.require(row['sdk'] == '7.47.0' and row['foreign'] == 404 and
                row['vault'].startswith('vault_') and row['credential'].startswith('cred_'),
                'invalid Vault SDK result')
    if plan['name'] == 'primary':
        pkg.require(row['created'] is True and row['http_requests'] == 0,
                    'resource creation should complete offline')
    else:
        prior = pkg.read(out/'primary'/name)
        expected, _ = mcp.DocumentSource(ROOT).read()
        pkg.require(all(row[k] == prior[k] for k in ('vault','credential')) and
                    row['call'].startswith('mcp_') and row['session'].startswith('agent_session_') and
                    row['citation'] == expected['source']['citation'] and
                    row['source_sha256'] == expected['source']['sha256'] and row['archived_bind'] == 404
                    and row['methods'] == ['initialize','notifications/initialized',
                                            'tools/list','tools/call','resources/read']
                    and row['http_requests'] == len(row['methods']),
                    'light failed to use and revoke the original credential')
    files = {name:pkg.sha(phase/name)}
    if selected.get('renewal'):
        exchanges = pkg.read(phase/'oauth-handoff.json')
        early = plan['name'] == 'primary'
        expected = [] if early else [[], row['methods'][:3], row['methods'][:4]]
        pkg.require(exchanges == dict(distinct_bearers=len(expected), methods_at_exchange=expected,
                    receipts=[] if early else ['original','rotated','rotated']),
                    'recorded OAuth exchanges differ from the owned MCP work')
        files['oauth-handoff.json'] = pkg.sha(phase/'oauth-handoff.json')
    return files


def business(stage, capabilities):
    verify_identity(stage.plan, capabilities)
    out = stage.out.parent
    folder = out/'mcp'
    plan = pkg.read(folder/'peer.json')
    pkg.require(all(pkg.sha(out/k) == h for k,h in stage.plan['agent_vaults']['fixture'].items()),
                'peer fixture drift')
    renewal = stage.plan['agent_vaults'].get('renewal') is True
    def cross_expiry(method):
        if method in ('tools/list', 'tools/call'):
            time.sleep(1.35)
    with ExitStack() as peers:
        peer = mcp.ProjectMcp(ROOT, folder, plan['port'], tls=plan['tls'], bearer=plan['bearer'],
                              before_reply=cross_expiry if renewal else None)
        peers.callback(peer.close)
        if renewal:
            issuer = TokenIssuer(peer, live_rotation=True, port=plan['issuer_port'],
                                 credentials=plan['issuer_credentials'])
            peers.callback(issuer.close)
        name = stage.plan['name']
        expected, _ = peer.source.read()
        args = dict(base='https://127.0.0.1:'+str(stage.plan['public_port'])+'/v1',
                    url=peer.url, profile=name, citation=expected['source']['citation'],
                    key=stage.keys['beta'], other=stage.keys['gamma'], sdk=stage.plan['sdk_directory'])
        if name == 'primary':
            args['secret'] = secrets.token_urlsafe(32) if renewal else peer.secret
            if renewal:
                args['refresh'] = issuer.grant()
        else:
            args['prior'] = pkg.read(out/'primary/vault-handoff.json')
            args['renewal'] = renewal
        result = subprocess.run(['node',ROOT/SDK],input=json.dumps(args),text=True,capture_output=True,
            env=dict(os.environ,NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']),timeout=100)
        if result.returncode:
            (stage.out/'vault-handoff.stderr').write_text(result.stderr)
            raise RuntimeError('Vault handoff failed in '+name+'; see private SDK stderr')
        row = json.loads(result.stdout)
        row['http_requests'] = peer.http_requests
        if name == 'light':
            snapshot = peer.snapshot()
            pkg.require(snapshot['calls'] == [expected] and snapshot['resource_reads'] == [expected['source']],
                        'expected exactly one real project read and one linked resource')
            row.update(methods=snapshot['methods'], source_sha256=expected['source']['sha256'])
        pkg.save(stage.out/'vault-handoff.json', row)
        if renewal:
            pkg.save(stage.out/'oauth-handoff.json', dict(distinct_bearers=len(set(issuer.issued)),
                methods_at_exchange=issuer.methods_at_exchange, receipts=issuer.receipts))
    verify(stage.out, stage.plan, capabilities)
