"""Release capacity handoff for the translation socket and browser track."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from tools.serve_live import profile_acceptance
from tools.serve_recovery import bundle, profile_live_reuse as reuse, profile_record
from tools.serve_responses.assistant_profiles import COMMON
from tools.serve_responses.beta_profile_acceptance import protocol
from tools.serve_service import package as pkg

ROOT = profile_acceptance.ROOT
PROOF = ROOT/'docs/evidence/serve-realtime-translation-20261124/sampling.json'
RELEASE = ROOT/'docs/evidence/serve-code-interpreter-20261124/delivery.json'
ACTORS = [ROOT/'tools/serve_realtime'/name for name in
          ('translation_sdk.mjs', 'translation_browser.py', 'translation_browser.js')]
RECEIPT = 'translation-transport.json'


def prepare(out, build, deployment):
    release = pkg.read(RELEASE)['publication']
    source = deployment/'build/service'
    manifest = bundle.verify(source, release['primary'])
    engine = pkg.verify(source/'engine', manifest['engine_manifest_sha256'])
    proof = pkg.read(PROOF)
    hashes = proof['candidate']
    identity = build/'generated/q4t/server/chat_protocol_sources.json'
    sources = pkg.read(identity)
    pkg.require(proof['status'] == 'ACCEPT_TRANSLATION_SAMPLING_AND_TYPED_ERRORS' and
                proof['protocol_sources_sha256'] == pkg.sha(identity) and
                all(pkg.sha(ROOT/n) == h for n,h in sources.items()) and
                hashes['binary'] == pkg.sha(build/'q4t') and
                hashes['ingress'] == pkg.sha(build/'q4t-ingress') and
                hashes['worker'] == engine['live_workers']['files']['q4t-live-webrtc'],
                'translation capacity needs the verified browser candidate')
    protected = {str(deployment/'build'/n):pkg.sha(deployment/'build'/n)
                 for n in ('q4t','q4t.release.json','service/manifest.json',
                           'service.release.json','service-native-state/ledger.json')}
    profile_acceptance.prepare_profiles(out, manifest, engine,
        dict(engine['live_workers'], engine_sha256=hashes['binary']), hashes['ingress'],
        build=build, protected=protected, installed_workers=source/'engine',
        selected_manifest=source/'manifest.json',
        previous_fixture=deployment/'.q4t-work/serve-live-20261121/fixture-business-01')
    for name in profile_acceptance.PROFILES:
        phase = out/name
        plan = pkg.read(phase/'plan.json')
        previous = phase/'previous-profile.json'
        shutil.copyfile(source/('profile-'+name+'.json'), previous)
        anchor = pkg.sha(previous)
        pkg.require(anchor == reuse.CODE_REVIEWED[name] ==
            manifest['recovery_profiles']['qualifications'][name]['evidence_sha256'],
            'selected release capacity differs from the reviewed predecessor')
        ingress = phase/'engine/q4t-ingress'
        shutil.copy2(plan['ingress'], ingress)
        policy = pkg.read(phase/'client-policy.json')
        for client in policy['clients']:
            if client['id'] in ('alpha','beta'):
                client.update(max_output_tokens=4096 if client['id']=='alpha' else 240,
                              requests_per_minute=60,tokens_per_minute=32000,
                              max_prompt_tokens=8192)
        pkg.save(phase/'client-policy.json', policy)
        shutil.copy2(phase/'client-policy.json',phase/'engine/client-policy.json')
        plan.update(ingress=str(ingress), translation_transport=dict(previous=anchor,
                    release=release['primary'], protocol=protocol(sources)),
                    client_policy_sha256=pkg.sha(phase/'client-policy.json'),
                    installed_policy_sha256=pkg.sha(phase/'engine/client-policy.json'))
        for path in (*ACTORS,Path(__file__),PROOF,RELEASE):
            plan['sources'][str(path.relative_to(ROOT))] = pkg.sha(path)
        pkg.save(phase/'plan.json',plan)


def certificate_pin(cert):
    public = subprocess.run(['openssl','x509','-in',str(cert),'-pubkey','-noout'],
                            check=True,capture_output=True,timeout=5).stdout
    der = subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=public,
                         check=True,capture_output=True,timeout=5).stdout
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def business(stage, capabilities):
    plan = stage.plan
    previous = stage.out.parent/'primary'/'translation-secret.private'
    primary = plan['name'] == 'primary'
    old_seconds = None
    if not primary:
        grant = pkg.read(previous)
        old_seconds = grant['expires_at'] - int(time.time())
        pkg.require(old_seconds > 30, 'old translation credential already expired')
        # Admission must reject the previous model generation while its signed
        # wall-clock expiry is still in the future.
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        import ssl
        req = Request(f"https://127.0.0.1:{plan['public_port']}/v1/realtime/translations/calls",
                      data=b'v=0\r\n',headers={'Content-Type':'application/sdp',
                          'Authorization':'Bearer '+grant['value']})
        try:
            with urlopen(req,timeout=8,context=ssl.create_default_context(
                    cafile=plan['tls']['certificate'])) as reply:
                status = reply.status
        except HTTPError as error:
            status = error.code
        pkg.require(status == 401, 'previous translation credential survived handoff')
    clip = plan['clips']['zh']
    label = 'socket' if primary else 'browser'
    params = dict(base=f"https://127.0.0.1:{plan['public_port']}/v1", key=stage.keys['alpha'],
                  other=stage.keys['beta'],sdk=plan['sdk_directory'],clip=clip['path'],
                  sha256=clip['sha256'],wire=str(stage.out/'translation-events.json'),
                  wav=str(stage.out/'translated.wav'),ca=plan['tls']['certificate'])
    if primary:
        args = ['node',ACTORS[0]]
    else:
        params['cert_spki'] = certificate_pin(params['ca'])
        args = [sys.executable,ACTORS[1]]
    run = subprocess.run(args,input=json.dumps(params),capture_output=True,text=True,
                         timeout=150,env=dict(os.environ,NODE_EXTRA_CA_CERTS=params['ca']))
    (stage.out/'translation.stderr').write_text(run.stderr)
    pkg.require(run.returncode == 0, 'translation '+label+' failed: '+plan['name'])
    row = json.loads(run.stdout)
    row['wire_events'] = len(row.pop('wire'))
    if primary:
        status, grant = stage.api('POST','/v1/realtime/translations/client_secrets',
            body={'session':{'model':'gpt-realtime-translate'},
                  'expires_after':{'anchor':'created_at','seconds':600}})
        pkg.require(status == 200 and grant['value'].startswith('ek_'), 'handoff credential issue')
        with open(previous,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as out:
            json.dump({'value':grant['value'],'expires_at':grant['expires_at']},out)
    row.update(transport=label,old_seconds=old_seconds,revoked=not primary)
    pkg.save(stage.out/RECEIPT,row)
    verify(stage.out,plan,capabilities)


def verify(phase,plan,capabilities):
    change = plan['translation_transport']
    name = plan['name']
    prior = profile_record.read(phase/'previous-profile.json')
    expected = dict(prior['capabilities'])
    expected['realtime'] = dict(expected['realtime'], translations=reuse.TRANSLATOR_CAPABILITY)
    expected['protocol_source_sha256'] = change['protocol']
    proof = pkg.read(PROOF)['candidate']
    pkg.require(pkg.sha(phase/'previous-profile.json') == change['previous'] == reuse.CODE_REVIEWED[name]
                and plan['selected_sha256'] == change['release'] == pkg.read(RELEASE)['publication']['primary']
                and pkg.same_json(expected,capabilities) and plan['capacity'] == prior['capacity']
                and proof['binary'] == plan['binary_sha256'] and proof['ingress'] == plan['ingress_sha256'],
                'translation declaration, capacity or candidate differs')
    row = pkg.read(phase/RECEIPT)
    pkg.require(row['independent_chat']=='42' and row['grant_audiences']==[401,401] and
                row['wire_events']==len(pkg.read(phase/'translation-events.json')) and row['wire_events']>2
                and row['pcm_bytes']>9600 and row['wav_sha256']==pkg.sha(phase/'translated.wav')
                and row['input'] and row['english'] and row['spoken'], 'incomplete translated speech')
    if name=='primary':
        pkg.require(row['transport']=='socket' and row['sdk']=='7.47.0' and row['before_close']>0
                    and row['limited_error']=='tts_output_limit' and row['limited_close']==1011
                    and not row['revoked'] and row['old_seconds'] is None,'invalid socket handoff')
    else:
        pkg.require(row['transport']=='browser' and row['streamCount']>0 and row['receivedBeforeClose']
                    and row['inbound']['packets']>0 and row['inbound']['energy']>0
                    and row['revoked'] and row['old_seconds']>30,'invalid browser handoff')
    inherited = prior['business']['live']['evidence_sha256']
    pkg.require(COMMON <= inherited.keys() and 'code-handoff.json' in inherited, 'missing release history')
    return {**{k:v for k,v in inherited.items() if k not in COMMON},RECEIPT:pkg.sha(phase/RECEIPT)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('output','build','deployment'):
        parser.add_argument('--'+key,type=Path,required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    pkg.require(out.is_relative_to(ROOT/'.q4t-work'),'private evidence directory required')
    prepare(out,args.build.resolve(strict=True),args.deployment.resolve(strict=True))
