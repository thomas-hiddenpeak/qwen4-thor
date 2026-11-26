"""Reopen the preserved interrupted job through each managed capacity."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_responses import agent_profile_acceptance as agents
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

EVIDENCE = ROOT/'docs/evidence/serve-responses-agent-interruption-20261123/checks.json'
SDK = 'tools/serve_responses/agent_interruption_sdk.mjs'


def prepare(output, build, original):
    from tools.serve_live import profile_acceptance
    evidence = pkg.read(EVIDENCE)
    installed = ROOT/'build/service'
    manifest = bundle.verify(installed, evidence['installed'])
    engine = pkg.verify(installed/'engine', manifest['engine_manifest_sha256'])
    meta = pkg.read(installed/'engine/q4t.release.json')
    prior = Path(meta['tested_source_manifest'])
    pkg.require(pkg.sha(prior) == meta['tested_source_manifest_sha256'],
                'installed source receipt changed')
    compiled = pkg.read(build/'generated/q4t/server/chat_protocol_sources.json')
    old = pkg.read(prior)
    pkg.require(protocol({n: old[n] for n in compiled}) == engine['protocol_sha256'] and
                {n for n, h in compiled.items() if old[n] != h} == {'src/server/responses_state.cpp'}
                and all(pkg.sha(ROOT/n) == h for n, h in compiled.items()),
                'runtime change exceeds the targeted recovery fix')
    binary = pkg.sha(build/'q4t')
    pkg.require(binary == evidence['candidate_sha256'] and
                manifest['files']['bin/q4t-ingress'] == evidence['ingress_sha256'],
                'candidate differs from the SDK recovery run')
    protected = {str(ROOT/'build'/n): pkg.sha(ROOT/'build'/n) for n in (
        'q4t', 'q4t.release.json', 'service/manifest.json', 'service.release.json',
        'service-native-state/ledger.json')}
    records = evidence['original']['record']
    for name, digest in records.items():
        pkg.require(pkg.sha(original/name) == digest, 'original interrupted store differs')
    change = dict(source_manifest_sha256=evidence['installed'],
                  previous_protocol=engine['protocol_sha256'], sdk_binary=binary,
                  sdk_protocol=protocol(compiled), protocol=protocol(compiled))
    profile_acceptance.prepare_profiles(output, manifest, engine,
        dict(engine['live_workers'], engine_sha256=binary), evidence['ingress_sha256'],
        protected=protected, beta_change=change, agent_change=dict(
            witness_sha256=pkg.sha(agents.EVIDENCE),
            capabilities=pkg.read(agents.EVIDENCE)['selected']['capabilities']))
    for name in profile_acceptance.PROFILES:
        path = output/name/'plan.json'
        plan = pkg.read(path)
        plan['agent_interruption'] = dict(original=str(original), evidence_sha256=pkg.sha(EVIDENCE))
        for helper in (SDK, 'tools/serve_responses/interruption_profile_acceptance.py'):
            plan['sources'][helper] = pkg.sha(ROOT/helper)
        pkg.save(path, plan)


def fixture(phase, plan):
    evidence = pkg.read(EVIDENCE)
    pkg.require(pkg.sha(EVIDENCE) == plan['agent_interruption']['evidence_sha256'] and
                Path(plan['output']) == phase and phase.parent.is_relative_to(ROOT/'.q4t-work'),
                'recovery fixture identity or directory')
    original = Path(plan['agent_interruption']['original'])
    records = evidence['original']['record']
    iid = evidence['original']['interrupted_after']['id']
    name, = [n for n in records if n.endswith('.response.' + iid + '.json')]
    store = phase.parent/'state/application-objects'
    args = plan['command']
    pkg.require(Path(args[args.index('--object-store-dir') + 1]) == store and
                original.resolve() != store.resolve() and pkg.sha(original/name) == records[name],
                'recovery fixture must use the private profile store')
    return original/name, store/name, evidence


def seed(phase, plan):
    # Both capacities start from the exact same real, pre-recovery bytes.
    # Preserve the primary terminal separately. All other shared objects stay.
    src, dest, evidence = fixture(phase, plan)
    dest.parent.mkdir(exist_ok=True, mode=0o700)
    if plan['name'] == 'light':
        primary = phase.parent/'primary'
        receipt = pkg.read(primary/'interruption.json')
        pkg.require(pkg.sha(dest) == receipt['terminal_sha256'] and
                    not (primary/'terminal-object.json').exists(), 'primary terminal changed')
        dest.rename(primary/'terminal-object.json')
    with dest.open('xb') as out, src.open('rb') as inp:
        shutil.copyfileobj(inp, out)
    dest.chmod(0o600)
    pkg.require(pkg.sha(dest) == evidence['original']['record'][dest.name], 'seed differs')


def business(stage):
    src, dest, evidence = fixture(stage.out, stage.plan)
    old = pkg.read(src)['record']
    # This historical verifier only qualifies the original, pinned engine.
    pkg.require(stage.plan['binary_sha256'] == evidence['candidate_sha256'],
                'legacy interruption contract belongs to its recorded engine')
    body = dict(base='https://127.0.0.1:'+str(stage.plan['public_port'])+'/v1',
                key=stage.keys['alpha'], other=stage.keys['gamma'], id=old['id'],
                items=old['data']['response']['output'], mode='recover', legacy_error_code=True,
                sequence=evidence['original']['interrupted_after']['sequence'],
                sdk=stage.plan['sdk_directory'])
    result = subprocess.run(['node', ROOT/SDK], input=json.dumps(body), text=True,
        env=dict(os.environ, NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate']),
        capture_output=True, timeout=45)
    pkg.require(result.returncode == 0, 'recovery SDK: '+result.stderr[-640:])
    actual = pkg.read(dest)['record']
    pkg.require(actual['id'] == old['id'] and actual['owner'] == old['owner'] and
                actual['data']['response']['output'] == old['data']['response']['output']
                and actual['data']['events'][:-1] == old['data']['events'] and
                actual['data']['events'][-1]['type'] == 'response.failed',
                'durable output or event prefix changed')
    pkg.save(stage.out/'interruption.json', dict(sdk=json.loads(result.stdout),
        from_revision=old['revision'], to_revision=actual['revision'],
        original_sha256=pkg.sha(src), terminal_sha256=pkg.sha(dest)))
    verify(stage.out, stage.plan)


def verify(phase, plan):
    phase = phase.resolve(strict=True)
    src, dest, evidence = fixture(phase, plan)
    proof = pkg.read(phase/'interruption.json')
    expected = evidence['recovery']
    saved = phase/'terminal-object.json'
    if not saved.exists():
        saved = dest
    pkg.require(proof['sdk'] == expected['sdk'] and proof['original_sha256'] == pkg.sha(src) and
                proof['from_revision'] == expected['persistence']['from_revision'] and
                proof['to_revision'] == expected['persistence']['to_revision'] and
                proof['terminal_sha256'] == pkg.sha(saved), 'reviewed agent recovery differs')
    return {'interruption.json': pkg.sha(phase/'interruption.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for opt in ('output', 'build', 'original'):
        parser.add_argument('--'+opt, type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    pkg.require(output.is_relative_to(ROOT/'.q4t-work'), 'private output required')
    prepare(output, args.build.resolve(strict=True), args.original.resolve(strict=True))
