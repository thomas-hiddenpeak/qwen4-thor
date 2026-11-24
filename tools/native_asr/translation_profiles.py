"""Exercise native translation and legacy Realtime clients at both release capacities."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import wave

from tools.serve_live import profile_acceptance
from tools.serve_live.client_acceptance import phone_query_matches, retained_query
from tools.serve_recovery import bundle, profile_live_reuse, profile_record
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_responses.vault_profile_acceptance import prepare_peer
from tools.serve_service import package as pkg

ROOT = profile_acceptance.ROOT
SDK = 'tools/native_asr/translation_sdk.mjs'
REALTIME = 'tools/serve_live/client_secret_sdk.mjs'
TRANSLATION = ROOT/'docs/evidence/serve-audio-translation-20261124/business.json'
BETA = ROOT/'docs/evidence/serve-realtime-legacy-sessions-20261124/beta-resources.json'
PREVIOUS = ROOT/'docs/evidence/serve-agent-workspaces-20261124/subagent-quota-profiles.json'
DELIVERY = PREVIOUS.with_name('subagent-delivery.json')
COMMON = {'configuration.wire', 'capabilities.json', 'core-after.wire',
          'observation.json', 'result.json', 'cleanup.json', 'plan.json'}


def prepare(out, build):
    source = ROOT/'build/service'
    manifest_digest = pkg.read(DELIVERY)['publication']['primary']
    manifest = bundle.verify(source, manifest_digest)
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    accepted = pkg.read(TRANSLATION)
    beta = pkg.read(BETA)
    sources = build/'generated/q4t/server/chat_protocol_sources.json'
    compiled = pkg.read(sources)
    candidate = pkg.sha(build/'q4t')
    ingress = pkg.sha(build/'q4t-ingress')
    pkg.require(accepted['lease'] == beta['lease'] == 'COMPLETED' and
                accepted['engine'] == candidate and accepted['ingress'] == ingress and
                accepted['compiled_sources'] == pkg.sha(sources) and
                accepted['protocol'] == protocol(compiled) and
                all(pkg.sha(ROOT/n) == h for n, h in compiled.items()) and
                pkg.sha(ROOT/SDK) == accepted['sdk']['driver_sha256'] and
                pkg.sha(ROOT/REALTIME) == beta['sdk_conversation']['driver_sha256'] ==
                beta['sdk_transcription']['driver_sha256'], 'candidate or SDK identity differs')
    protected = {str(ROOT/'build'/n): pkg.sha(ROOT/'build'/n) for n in (
        'q4t', 'q4t.release.json', 'service/manifest.json', 'service.release.json',
        'service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out, manifest, engine,
        dict(engine['live_workers'], engine_sha256=candidate), ingress,
        build=build, protected=protected)
    prepare_peer(out, renewal=True)
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'), previous)
        prior = pkg.sha(previous)
        pkg.require(prior == pkg.read(PREVIOUS)['profiles'][name]['record'] ==
                    manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
                    'selected capacity predecessor differs')
        pinned = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'], pinned)
        policy = pkg.read(phase/'client-policy.json')
        owner = next(c for c in policy['clients'] if c['id'] == 'alpha')
        owner.update(requests_per_minute=60, tokens_per_minute=32000, max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json', policy)
        shutil.copy2(phase/'client-policy.json', phase/'engine/client-policy.json')
        plan.update(ingress=str(pinned), media_client_change=dict(
            previous_profile=prior, protocol=accepted['protocol'], predecessor_manifest=manifest_digest),
            client_policy_sha256=pkg.sha(phase/'client-policy.json'),
            installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        plan['command'].extend(['--mcp-policy-file', str(out/'mcp/policy.json')])
        for path in (ROOT/SDK, ROOT/REALTIME, Path(__file__), TRANSLATION, BETA, PREVIOUS, DELIVERY):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json', plan)


def call(stage, label, driver, options):
    env = dict(os.environ, Q4T_NODE_SDK_DIR=stage.plan['sdk_directory'],
               NODE_EXTRA_CA_CERTS=stage.plan['tls']['certificate'])
    params = dict(base_url=f"https://127.0.0.1:{stage.plan['public_port']}/v1",
                  key=stage.keys['alpha'], **options)
    result = subprocess.run(['node', ROOT/driver], input=json.dumps(params), text=True,
                            capture_output=True, timeout=120, env=env)
    (stage.out/(label+'.stderr')).write_text(result.stderr)
    if result.stdout:
        pkg.save(stage.out/(label+'.json'), json.loads(result.stdout))
    pkg.require(result.returncode == 0, label+' SDK failed in '+stage.plan['name'])


def business(stage, capabilities):
    clip = retained_query()
    wav = stage.out/'source.wav'
    with wave.open(str(wav), 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(clip['rate'])
        target.writeframes(Path(clip['path']).read_bytes())
    call(stage, 'translations', SDK, dict(wav=str(wav), duration=clip['bytes']/2/clip['rate'],
                                           sdk_directory=stage.plan['sdk_directory']))
    call(stage, 'beta-conversation', REALTIME, dict(legacy_wire=True))
    call(stage, 'beta-transcription', REALTIME, dict(legacy_transcription=True,
         beta_transcription_wire=True, pcm=clip['path']))
    verify(stage.out, stage.plan, capabilities)


def verify(phase, plan, capabilities):
    change = plan['media_client_change']
    predecessor = phase/'previous-profile.json'
    name = plan['name']
    pkg.require(pkg.sha(predecessor) == change['previous_profile'] ==
                pkg.read(PREVIOUS)['profiles'][name]['record'] and
                change['predecessor_manifest'] == pkg.read(DELIVERY)['publication']['primary'],
                'wrong prior release or capacity')
    prior = profile_record.read(predecessor)
    declared = dict(capabilities)
    translation = declared.pop('translations', None)
    accepted = pkg.read(TRANSLATION)
    pkg.require(translation == profile_live_reuse.TRANSLATION_CAPABILITY and
                declared.pop('protocol_source_sha256') == change['protocol'] == accepted['protocol'] and
                pkg.same_json(declared, {k:v for k,v in prior['capabilities'].items()
                                         if k != 'protocol_source_sha256'}) and
                plan['capacity'] == prior['capacity'] and plan['binary_sha256'] == accepted['engine']
                and plan['ingress_sha256'] == accepted['ingress'], 'capacity or capability drift')
    result = pkg.read(phase/'translations.json')
    convo = pkg.read(phase/'beta-conversation.json')
    asr = pkg.read(phase/'beta-transcription.json')
    clip = retained_query()
    pkg.require(result['status'] == 'PASS' and result['independent_chat'] == '42' and
                result['sdk'] == convo['sdk'] == asr['sdk'] == accepted['sdk']['sdk'] and
                result['negative'] == accepted['sdk']['negative'] and
                result['formats'].keys() == accepted['sdk']['formats'].keys() and
                all(v['status'] == 200 and v['wire'] for v in result['formats'].values()) and
                convo['answer'] == '42' and convo['reported_temperature'] == .6 and
                convo['max_output_tokens'] == 32 and asr['independent_chat'] == '42' and
                asr['beta_start'] == 'session.created' and asr['beta_update'] == 'transcription_session.updated'
                and phone_query_matches(asr['transcript'], clip['expected_transcript']) and
                pkg.sha(phase/'source.wav') == accepted['sdk']['wav_sha256'] and
                pkg.sha(ROOT/SDK) == accepted['sdk']['driver_sha256'] and
                pkg.sha(ROOT/REALTIME) == pkg.read(BETA)['sdk_conversation']['driver_sha256'],
                'real translation or legacy Realtime observation differs')
    previous = prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= previous.keys(), 'missing predecessor observations')
    proofs = {k:v for k,v in previous.items() if k not in COMMON}
    proofs.update({n:pkg.sha(phase/n) for n in (
        'translations.json', 'beta-conversation.json', 'beta-transcription.json')})
    return proofs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'), 'fresh private output required')
    prepare(out, args.build.resolve(strict=True))
