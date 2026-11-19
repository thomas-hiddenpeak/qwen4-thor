"""Actual registered voice continuity across native entry/payload recovery.

A single consent and voice are created through the API. Each phase uses the
same IDs, complete Speech/Chat, unhinted ASR and stored audio. Revocation is
checked after the final phase. This test actor is not a runtime dependency.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

from tools.native_tts import managed_service
from tools.native_tts.http_acceptance import sse
from tools.serve_recovery import profile_contract, profile_evidence as evidence
from tools.serve_service.package import read, require, same_json, save, sha


def voice_helper():
    # This acceptance-only helper is intentionally absent from the sealed
    # production namespace. Its own production imports must remain sealed.
    path = Path(__file__).resolve().parents[2]/'tools/serve_recovery/profile_voice.py'
    spec = importlib.util.spec_from_file_location('q4t_voice_acceptance_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile_voice = voice_helper()


def labels(plan):
    return managed_service.labels(plan)


def posts(plan):
    return 2 + 4*len(labels(plan)) + 2


def selection(plan, configured):
    fixed = None if configured is None else profile_contract.tts_binding(configured, base=True)
    require(same_json(plan.get('native_voice'), fixed),
            'managed registered voice omitted or changed from the package')
    return fixed


def original_request(out, name, path, body=None, *, method=None, client='alpha',
                     content_type='application/json'):
    method = method or ('GET' if body is None else 'POST')
    raw = body if type(body) is bytes else b'' if body is None else json.dumps(body).encode()
    expected = dict(method=method, path=path, client=client, content_type=content_type,
                    bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    require(read(out/(name+'-exchange.json')) == expected,
            'managed voice original request differs: '+name)
    if body is not None:
        require((out/(name+'-request.body')).read_bytes() == raw,
                'managed voice request wire changed: '+name)


def response(out, name, status=200):
    require(read(out/(name+'-http.json'))['status'] == status,
            'managed voice HTTP status differs: '+name)
    return (out/(name+'.wire')).read_bytes()


def json_response(out, name, status=200):
    return json.loads(response(out,name,status))


def clip(plan, name):
    row=plan['voice_clips'][name];path=Path(row['path'])
    require(0 < path.stat().st_size <= profile_voice.MAX_AUDIO_BYTES and
            sha(path)==row['sha256'], 'managed voice input identity changed')
    return path.read_bytes()


def reference_records(directory, consent, voice):
    records = {}
    for name, kind, identifier in (('consent','voice_consent',consent),
                                   ('voice','voice',voice)):
        paths = list(Path(directory).glob('*.'+kind+'.'+identifier+'.json'))
        require(len(paths) == 1 and not paths[0].is_symlink() and
            paths[0].stat().st_size <= profile_voice.MAX_RESPONSE_BYTES,
            'one actual managed ICL record required')
        records[name] = read(paths[0])['record']
    evidence.voice_reference(records, consent, voice)
    return records


class Actor:
    def __init__(self, stage):
        self.stage=stage;self.consent=self.voice=None;self.completed=[];self.count=0
        self.attempted=[];self.finished=False
        self.reference = None

    def call(self, name, path, body=None, *, method=None, client='alpha',
             status=200, content_type='application/json', raw=False):
        method=method or ('GET' if body is None else 'POST')
        if method=='POST':
            require(self.count < posts(self.stage.plan), 'managed voice attempt bound')
            self.count+=1
        payload=body if type(body) is bytes else b'' if body is None else json.dumps(body).encode()
        require(len(payload)<=profile_voice.MAX_AUDIO_BYTES+65536,'managed voice request bound')
        save(self.stage.output/(name+'-exchange.json'),dict(method=method,path=path,
            client=client,content_type=content_type,bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest()))
        if body is not None:(self.stage.output/(name+'-request.body')).write_bytes(payload)
        value=self.stage.request(name,body,path=path,method=method,client=client,
            expected=status,model_work=method=='POST',content_type=content_type,raw_result=raw)
        if method=='POST':self.stage.drained(name+'-drain')
        return value

    def form(self,name,path,fields,field,audio):
        body,mime=profile_voice.multipart(fields,field,audio)
        return self.call(name,path,body,content_type=mime)

    def observe(self,label):
        require(not self.finished and label in labels(self.stage.plan) and
                label not in self.attempted and
                label==labels(self.stage.plan)[len(self.attempted)],
                'managed voice phase repeated or reordered')
        self.attempted.append(label)
        if self.voice is None:
            fields=evidence.voice_requests('','')['consent']
            self.consent=self.form('voice-consent',profile_voice.CONSENTS,fields,
                'recording',clip(self.stage.plan,'consent'))
            profile_voice.Lifecycle.created((200,json.dumps(self.consent).encode()),'voice_consent')
            fields=evidence.voice_requests(self.consent['id'],'')['voice']
            self.voice=self.form('voice-create',profile_voice.VOICES,fields,
                'audio_sample',clip(self.stage.plan,'sample'))
            profile_voice.Lifecycle.created((200,json.dumps(self.voice).encode()),'voice')
        rows=evidence.voice_requests(self.consent['id'],self.voice['id']);prefix=label+'-voice'
        if self.stage.plan.get('voice_icl'):
            records = reference_records(self.stage.output/'state/application-objects',
                                        self.consent['id'], self.voice['id'])
            require(self.reference is None or same_json(records, self.reference),
                    'persisted ICL reference changed during recovery')
            self.reference = records
            save(self.stage.output/(prefix+'-conditioning.json'), records)
        self.call(prefix+'-consent',profile_voice.CONSENTS+'/'+self.consent['id'])
        self.call(prefix+'-cross-consent',profile_voice.CONSENTS+'/'+self.consent['id'],
                  client='beta',status=404)
        if self.completed:
            prior=read(self.stage.output/(self.completed[-1]+'-voice-chat.wire'))
            self.call(prefix+'-prior-chat','/v1/chat/completions/'+prior['id'])
        wire=self.call(prefix+'-speech','/v1/audio/speech',rows['speech'],raw=True)
        audio,_=sse(wire)
        self.form(prefix+'-speech-asr','/v1/audio/transcriptions',rows['transcription'],'file',audio)
        chat=self.call(prefix+'-chat','/v1/chat/completions',rows['chat'])
        message=evidence.message(chat,'stop')
        self.call(prefix+'-stored-chat','/v1/chat/completions/'+chat['id'])
        self.call(prefix+'-cross-chat','/v1/chat/completions/'+chat['id'],client='beta',status=404)
        audio=base64.b64decode(message['audio']['data'],validate=True)
        self.form(prefix+'-chat-asr','/v1/audio/transcriptions',rows['transcription'],'file',audio)
        self.stage.configuration(prefix)
        self.completed.append(label)

    def finish(self):
        require(not self.finished and tuple(self.completed)==labels(self.stage.plan),
                'managed voice phases incomplete or already finished')
        self.finished=True
        rows=evidence.voice_requests(self.consent['id'],self.voice['id'])
        self.call('voice-revoke',profile_voice.CONSENTS+'/'+self.consent['id'],method='DELETE')
        self.call('voice-revoked-speech','/v1/audio/speech',rows['speech'],status=404)
        self.call('voice-revoked-chat','/v1/chat/completions',rows['chat'],status=404)
        self.call('voice-revoked-consent',profile_voice.CONSENTS+'/'+self.consent['id'],status=404)
        self.call('voice-delete',profile_voice.VOICES+'/'+self.voice['id'],method='DELETE')
        require(self.count==posts(self.stage.plan),'managed voice checklist incomplete')


def check(out, plan, configured):
    fixed=selection(plan,configured)
    if fixed is None:return {}
    out=Path(out);phases=labels(plan)
    consent=json_response(out,'voice-consent');voice=json_response(out,'voice-create')
    for item,kind in [(consent,'voice_consent'),(voice,'voice')]:
        profile_voice.Lifecycle.created((200,json.dumps(item).encode()),kind)
    rows=evidence.voice_requests(consent['id'],voice['id'])
    require(consent['name']==rows['consent']['name'] and
        consent['language']==rows['consent']['language'] and
        voice['name']==rows['voice']['name'] and voice['type']==rows['voice']['type'],
        'managed voice returned metadata differs from registration')
    def request(name,path,body=None,**options):
        original_request(out,name,path,body,**options)
    def upload(name,path,fields,field,audio):
        body,mime=profile_voice.multipart(fields,field,audio)
        request(name,path,body,content_type=mime)
    def rejected(name,path,body=None,**options):
        request(name,path,body,**options)
        evidence.voice_error(dict(status=404,body=json_response(out,name,404)),404)
    upload('voice-consent',profile_voice.CONSENTS,rows['consent'],'recording',clip(plan,'consent'))
    upload('voice-create',profile_voice.VOICES,rows['voice'],'audio_sample',clip(plan,'sample'))
    result={};previous=None;reference=None
    for label in phases:
        prefix=label+'-voice'
        request(prefix+'-consent',profile_voice.CONSENTS+'/'+consent['id'])
        require(json_response(out,prefix+'-consent')==consent,'voice consent lost during recovery')
        rejected(prefix+'-cross-consent',profile_voice.CONSENTS+'/'+consent['id'],client='beta')
        if previous is not None:
            request(prefix+'-prior-chat','/v1/chat/completions/'+previous['id'])
            saved=json_response(out,prefix+'-prior-chat')
            require(saved['id']==previous['id'] and saved['choices']==previous['choices'],
                    'registered stored Chat lost during recovery')
        request(prefix+'-speech','/v1/audio/speech',rows['speech'])
        audio,usage=sse(response(out,prefix+'-speech'))
        upload(prefix+'-speech-asr','/v1/audio/transcriptions',rows['transcription'],'file',audio)
        request(prefix+'-chat','/v1/chat/completions',rows['chat'])
        chat=json_response(out,prefix+'-chat');message=evidence.message(chat,'stop')
        request(prefix+'-stored-chat','/v1/chat/completions/'+chat['id'])
        saved=json_response(out,prefix+'-stored-chat')
        require(saved['id']==chat['id'] and saved['choices']==chat['choices'],
                'registered Chat full history differs')
        rejected(prefix+'-cross-chat','/v1/chat/completions/'+chat['id'],client='beta')
        chat_audio=base64.b64decode(message['audio']['data'],validate=True)
        upload(prefix+'-chat-asr','/v1/audio/transcriptions',rows['transcription'],'file',chat_audio)
        require(read(out/(prefix+'-config-http.json'))['http_status']==200,
                'managed voice configuration unavailable')
        config=read(out/(prefix+'-config.wire'))
        value=dict(limits=config['native_tts_limits'],
            speech=dict(audio_base64=base64.b64encode(audio).decode(),usage=usage['usage']),
            chat=chat,speech_transcript=json_response(out,prefix+'-speech-asr'),
            chat_transcript=json_response(out,prefix+'-chat-asr'))
        evidence.voice_synthesis(value,fixed)
        icl = plan.get('voice_icl', False)
        require(type(icl) is bool, 'invalid managed ICL declaration')
        if icl:
            records = read(out/(prefix+'-conditioning.json'))
            evidence.voice_icl_business(dict(value,consent=consent,voice=voice,
                                             conditioning=records))
            require(reference is None or same_json(reference,records),
                    'managed ICL reference was not preserved exactly')
            reference = records
        condition_model = fixed['model']+('-reference' if icl else '-speaker')
        for key,model in [('native_tts_base_health',fixed['model']),
                          ('native_voice_health',condition_model)]:
            health=config[key]
            fresh_condition = (key == 'native_voice_health' and
                plan['scenario'] == 'tool_version' and label == 'fallback' and
                health.get('state') == 'configured')
            if fresh_condition:
                # The same persisted speaker vector is consumed after a new
                # model process; registration's encoder need not be loaded.
                require(all(type(health.get(k)) is int and health[k] == 0
                    for k in ('device_bytes','load_attempts','load_failures',
                              'compute_failures')) and
                    health.get('asset_cleanup_pending') is False,
                    'unloaded registration encoder has work or pending failure')
            require((health.get('state') in ('ready','metadata_ready') or
                     fresh_condition) and
                health.get('model')==model and health.get('reason')=='none' and
                health.get('core_readiness_dependency') is False and
                type(health.get('compute_failures')) is int and health['compute_failures']==0,
                'managed voice lost healthy isolated backend')
            if icl:
                require(health.get('asset_cleanup_pending') is False and
                    type(health.get('load_failures')) is int and health['load_failures'] == 0 and
                    (key != 'native_voice_health' or health.get('reference_encoder') is True),
                    'managed ICL backend failed or lost reference identity')
        result[prefix]=dict(consent_id=consent['id'],voice_id=voice['id'],chat_id=chat['id'],
            speech_sha256=hashlib.sha256(audio).hexdigest(),
            chat_audio_sha256=hashlib.sha256(chat_audio).hexdigest(),
            speech_transcript=value['speech_transcript']['text'],
            chat_transcript=value['chat_transcript']['text'])
        if icl:
            result[prefix].update(reference_sha256=hashlib.sha256(
                json.dumps(reference,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                reference_frames=len(reference['voice']['data']['reference']['codes']),
                speech_input_tokens=value['speech']['usage']['input_tokens'],
                chat_input_tokens=chat['usage']['q4t_audio_synthesis']['input_tokens'])
        previous=chat
    for name,path,item in [('voice-revoke',profile_voice.CONSENTS+'/'+consent['id'],consent),
                            ('voice-delete',profile_voice.VOICES+'/'+voice['id'],voice)]:
        request(name,path,method='DELETE');deleted=json_response(out,name)
        require(deleted==dict(id=item['id'],object=item['object'],deleted=True),
                'managed voice deletion did not retire original ID')
    rejected('voice-revoked-speech','/v1/audio/speech',rows['speech'])
    rejected('voice-revoked-chat','/v1/chat/completions',rows['chat'])
    rejected('voice-revoked-consent',profile_voice.CONSENTS+'/'+consent['id'])
    return result
