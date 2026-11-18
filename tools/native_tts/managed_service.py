"""Actual Speech/Chat audio before and after managed recovery, no extra retries."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

from tools.serve_recovery import profile_contract, profile_evidence as evidence
from tools.serve_service.package import require, same_json, save

ROOT = Path(__file__).resolve().parents[2]


def labels(plan):
    if plan['scenario'] == 'tls':
        return ('tls-before', 'tls-after')
    if plan['scenario'] == 'tool_version':
        return ('primary', 'fallback')
    require(plan['scenario'] == 'tool_retained', 'complete managed TTS stage required')
    return ('primary',)


def sdk(stage, label, body, *, speech, python):
    """Use the real pinned SDK with verified TLS and an independent API key."""
    stage.budget.reserve('chats', label)
    save(stage.output/(label+'-request.json'), body)
    config = dict(base_url=f'https://127.0.0.1:{stage.port}/v1',
        api_key=stage.keys['alpha'], ca=stage.plan['tls_inputs']['ca']['path'],
        sdk_directory=stage.plan['sdk']['sdk_directory'], request=body,
        output=str(stage.output/(label+'.wire')))
    source = 'tools/native_tts/http_sdk' if speech else 'tools/native_asr/chat_sdk'
    command = ([stage.plan['python_sdk']['python'], '-I', '-B', str(ROOT/(source+'.py'))]
               if python else [stage.plan['sdk']['node']['path'], str(ROOT/(source+'.mjs'))])
    env = {k:v for k,v in os.environ.items() if k not in
        ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'HTTP_PROXY', 'HTTPS_PROXY',
         'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'NODE_USE_ENV_PROXY')}
    env.update(PYTHONDONTWRITEBYTECODE='1', NODE_EXTRA_CA_CERTS=config['ca'])
    run = subprocess.run(command, input=json.dumps(config), capture_output=True,
                         text=True, env=env, timeout=190)
    (stage.output/(label+'.stderr')).write_text(run.stderr)
    require(run.stdout, 'managed audio SDK returned no evidence: '+label)
    value = json.loads(run.stdout)
    if not speech:
        (stage.output/(label+'.wire')).write_bytes(
            base64.b64decode(value.pop('wire_base64'), validate=True))
    save(stage.output/(label+'.sdk.json'), value)
    require(run.returncode == 0 and value.get('ok') is True and
        len(value.get('exchanges', [])) == 1, 'managed audio SDK failed or retried: '+label)


def observe(stage, label):
    if stage.plan.get('native_tts') is None:
        return
    require(label in labels(stage.plan), 'unplanned managed speech request')
    for kind, body in evidence.tts_requests().items():
        name = label+'-tts-'+kind
        if stage.plan['scenario'] == 'tls':
            sdk(stage, name, body, speech=kind == 'speech',
                python=(kind == 'speech') == (label == 'tls-before'))
        else:
            stage.request(name, body, model_work=True, raw_result=kind == 'speech',
                path='/v1/audio/speech' if kind == 'speech' else '/v1/chat/completions')
    stage.drained(label+'-tts-drain')
    stage.configuration(label+'-tts')


def check(out, plan, configured):
    """Require complete actual outputs in each phase, bound to its own health."""
    fixed = None if configured is None else profile_contract.tts_binding(configured)
    require(same_json(plan.get('native_tts'), fixed),
            'managed TTS omitted or changed from the actual package')
    if fixed is None:
        return {}
    read = lambda name: json.loads((out/name).read_text())
    identity = dict(native_tts=fixed)
    result = {}
    for label in labels(plan):
        prefix = label+'-tts'
        request = {kind:read(prefix+'-'+kind+'-request.json') for kind in ('speech', 'chat')}
        speech = (out/(prefix+'-speech.wire')).read_bytes()
        chat = read(prefix+'-chat.wire')
        config = read(prefix+'-config.wire')
        witness = dict(requests=request, speech_base64=base64.b64encode(speech).decode(),
            chat_response=chat, health=config['native_tts_health'],
            limits=config['native_tts_limits'])
        evidence.tts_business(witness, identity)
        for kind in ('speech', 'chat'):
            name = prefix+'-'+kind
            if plan['scenario'] == 'tls':
                observed = read(name+'.sdk.json')
                python = (kind == 'speech') == (label == 'tls-before')
                require(observed.get('ok') is True and
                    observed.get('version') == ('2.8.1' if python else '7.47.0') and
                    len(observed.get('exchanges', [])) == 1,
                    'managed TTS SDK failed, retried or changed version')
                exchange = observed['exchanges'][0]
                path = '/v1/audio/speech' if kind == 'speech' else '/v1/chat/completions'
                require(exchange.get('method') == 'POST' and
                    exchange.get('url', '').startswith('https://127.0.0.1:') and
                    exchange['url'].endswith(path), 'managed TTS TLS endpoint differs')
                if kind == 'speech':
                    require(observed.get('status') == 200 and
                        observed.get('bytes') == len(speech) and
                        observed.get('sha256') == hashlib.sha256(speech).hexdigest(),
                        'managed Speech SDK bytes differ from captured wire')
                else:
                    evidence.tts_business(dict(witness, chat_response=observed['result']), identity)
            else:
                require(read(name+'-http.json')['status'] == 200,
                        'managed TTS HTTP did not succeed')
        result[prefix] = dict(actual_speech_sha256=hashlib.sha256(speech).hexdigest(),
            actual_chat_audio_sha256=evidence.TTS_AUDIO['chat'][1],
            usage=chat['usage'], health=witness['health'], limits=witness['limits'])
    return result
