"""Qualify stored Vault credentials on the two actual managed capacities."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess

from tools.serve_responses import mcp_business_peer as mcp
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT/'docs/evidence/serve-agent-vaults-20261123/mcp.json'
PUBLICATION = ROOT/'docs/evidence/serve-agent-sessions-20261123/delivery.json'
SDK = 'tools/serve_responses/agent_vault_handoff_sdk.mjs'
HELPER = 'tools/serve_responses/vault_profile_acceptance.py'
PEER = 'tools/serve_responses/mcp_business_peer.py'
SESSION_MCP = dict(connection_origin='service', transport='http',
                   vault_auth=['static_bearer', 'mcp_oauth_access_token'])


def candidate(build, current):
    proof = pkg.read(EVIDENCE)
    actor = pkg.sha(build/'q4t')
    pkg.require(proof['accepted']['status'] == 'PASS' and proof['business']['calls'] == 1
                and proof['engine_sha256'] == actor and
                protocol(current).encode() in (build/'q4t').read_bytes(),
                'managed qualification needs the tested MCP engine')
    return dict(receipt_sha256=pkg.sha(EVIDENCE), binary=actor, protocol=protocol(current),
                ingress=proof['ingress_sha256'])


def prepare_peer(out):
    folder = out/'mcp'
    folder.mkdir(mode=0o700)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    tls = mcp.prepare_tls(folder)
    pkg.save(folder/'peer.json', dict(port=port, tls=tls, bearer=secrets.token_urlsafe(32)))
    pkg.save(folder/'policy.json', dict(version=1, targets=[], fetch=dict(public_networks=False,
        allow_http=False, allowed_networks=['127.0.0.1/32','::1/128'], ports=[port],
        ca_file=tls['ca_certificate'], timeout_ms=5000, max_redirects=0)))
    return {str(p.relative_to(out)):pkg.sha(p) for p in
            [folder/'peer.json',folder/'policy.json',*(Path(v) for v in tls.values())]}


def verify_identity(plan, capabilities):
    from tools.serve_responses.session_profile_acceptance import CAPABILITY
    witness = plan['agent_vaults']
    sessions = dict(CAPABILITY, tool_types=['function','mcp'], mcp=SESSION_MCP)
    pkg.require(witness['receipt_sha256'] == pkg.sha(EVIDENCE) and
                plan['binary_sha256'] == witness['binary'] == pkg.sha(plan['binary']) ==
                pkg.read(EVIDENCE)['engine_sha256'] and
                plan['ingress_sha256'] == witness['ingress'] == pkg.sha(plan['ingress']) and
                capabilities.get('protocol_source_sha256') == witness['protocol'] and
                capabilities.get('agent_sessions') == sessions and
                capabilities.get('agent_vaults',{}).get('credentials') == dict(
                    auth=['mcp_oauth','static_bearer','environment_variable'],
                    write_only=True,rotation=True,archive_cascade=True) and
                all(plan['sources'].get(n) == pkg.sha(ROOT/n) for n in
                    (SDK, HELPER, PEER, str(PUBLICATION.relative_to(ROOT)))),
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
    return {name:pkg.sha(phase/name)}


def business(stage, capabilities):
    verify_identity(stage.plan, capabilities)
    out = stage.out.parent
    folder = out/'mcp'
    plan = pkg.read(folder/'peer.json')
    pkg.require(all(pkg.sha(out/k) == h for k,h in stage.plan['agent_vaults']['fixture'].items()),
                'peer fixture drift')
    peer = mcp.ProjectMcp(ROOT, folder, plan['port'], tls=plan['tls'], bearer=plan['bearer'])
    try:
        name = stage.plan['name']
        expected, _ = peer.source.read()
        args = dict(base='https://127.0.0.1:'+str(stage.plan['public_port'])+'/v1',
                    url=peer.url, profile=name, citation=expected['source']['citation'],
                    key=stage.keys['beta'], other=stage.keys['gamma'], sdk=stage.plan['sdk_directory'])
        if name == 'primary':
            args['secret'] = peer.secret
        else:
            args['prior'] = pkg.read(out/'primary/vault-handoff.json')
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
    finally:
        peer.close()
    verify(stage.out, stage.plan, capabilities)
