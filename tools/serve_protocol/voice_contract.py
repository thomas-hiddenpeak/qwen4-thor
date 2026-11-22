"""HTTP declarations for the implemented speech, classification and socket endpoints."""


def add_voice_routes(paths, response, error):
    text = {'type': 'string'}
    ident = dict(type='string', pattern='^[A-Za-z0-9_-]{1,128}$')
    binary = dict(type='string', format='binary')

    def obj(properties, required):
        return dict(type='object', properties=properties,
                    additionalProperties=False, required=required)

    def param(name, location, schema, **extra):
        return dict(name=name, **{'in': location}, required=location=='path',
                    schema=schema, **extra)

    def content(media, schema):
        return {media: dict(schema=schema)}

    def operation(method, path, success, *, body=None, parameters=(), **extra):
        ids = [param(part[1:-1], 'path', ident) for part in path.split('/')
               if part.startswith('{')]
        identifier = method+'_'+path.strip('/').replace('/', '_').replace('{','').replace('}','')
        op = dict(operationId=identifier, parameters=[*ids,*parameters],
                  responses={**success, 'default':response(error, 'Native API error')}, **extra)
        if body:
            op['requestBody'] = dict(required=True, content=body)
        paths.setdefault(path,{})[method] = op

    def json_result(schema):
        return {'200': response(schema)}

    def decision(name):
        return {'$ref':'serve-application-v1.schema.json#/$defs/decisions_'+name}

    operation('post','/v1/decisions', json_result(decision('decision')),
              body=content('application/json', decision('request')))
    speech = obj(dict(model=dict(type='string',minLength=1,maxLength=256),
        input=dict(type='string',minLength=1,maxLength=4096),
        voice=dict(anyOf=[dict(type='string',minLength=1,maxLength=256),
                          obj({'id':dict(type='string',minLength=1,maxLength=256)},['id'])]),
        instructions=dict(type='string',maxLength=4096),
        speed=dict(type='number',minimum=.25,maximum=4,default=1),
        response_format=dict(enum=['mp3','opus','aac','flac','wav','pcm'],default='mp3'),
        stream_format=dict(enum=['audio','sse'],default='audio')), ['model','input','voice'])
    speech_types = ('audio/mpeg','audio/ogg','audio/aac','audio/flac','audio/wav','audio/pcm')
    body = {mime:dict(schema=binary) for mime in speech_types}
    body.update(content('text/event-stream',text))
    operation('post','/v1/audio/speech',{'200':dict(description='Audio or server-sent speech events',
              content=body)},body=content('application/json',speech))

    name = dict(type='string',minLength=1,maxLength=256)
    props = dict(id=ident,name=name,created_at=dict(type='integer'))
    consent = obj(dict(props,object=dict(const='audio.voice_consent'),language=text),
                  ['id','name','created_at','object','language'])
    voice = obj(dict(props,object=dict(const='audio.voice'),type=dict(const='audio_sample')),
                ['id','name','created_at','object','type'])
    consents = '/v1/audio/voice_consents'
    voices = '/v1/audio/voices'
    page = dict(type='integer',minimum=1,maximum=100,default=20)
    pagination = [param('limit','query',page),param('after','query',ident)]
    def listing(item):
        return dict(type='object',required=['object','data','has_more'],properties=dict(
            object=dict(const='list'),data=dict(type='array',items=item),has_more=dict(type='boolean')))
    def deleted(kind):
        return obj(dict(id=ident,object=dict(const=kind),deleted=dict(const=True)),['id','object','deleted'])
    operation('get',consents,json_result(listing(consent)),parameters=pagination)
    operation('post',consents,json_result(consent),body=content('multipart/form-data',
        obj(dict(name=name,language=text,recording=binary),['name','language','recording'])))
    update = obj(dict(name=name),['name'])
    operation('post',consents+'/{consent_id}',json_result(consent),body={
        **content('application/json',update),**content('multipart/form-data',update)})
    operation('get',consents+'/{consent_id}',json_result(consent))
    operation('delete',consents+'/{consent_id}',json_result(deleted('audio.voice_consent')))
    sample = obj(dict(type=dict(const='audio_sample'),name=name,consent=ident,audio_sample=binary),
                 ['name','consent','audio_sample'])
    prompt = obj(dict(type=dict(const='prompt'),name=name, model=name,
                      prompt=dict(type='string',minLength=1,maxLength=4096),
                      script_hint=dict(type='string',minLength=1,maxLength=2000)),['name','type','prompt'])
    operation('post',voices,json_result(voice),body={
        **content('multipart/form-data',dict(oneOf=[sample,prompt])),**content('application/json',prompt)},
        description='Sample voices require verified consent and audio. Prompt voice requests currently return 503 voice_design_not_configured.')
    operation('delete',voices+'/{voice_id}',json_result(deleted('audio.voice')))
    upgrade = {'101':dict(description='Authenticated WebSocket upgrade',
        headers={'Upgrade':dict(schema=dict(const='websocket')),
                 'Sec-WebSocket-Accept':dict(schema=text)})}
    operation('get','/v1/realtime',upgrade,parameters=[
        param('intent','query',dict(enum=['realtime','transcription'])),
        param('model','query',text,description='Conversation only; transcription uses the exact intent=transcription query.')])
    live = 'serve-application-v1.schema.json#/$defs/live_'
    operation('post','/v1/live/sessions',{'201':response({'$ref':live+'media_created'}, 'Live session created')},
              body=content('application/json', {'$ref':live+'media_create'}))
    operation('get','/v1/live/sessions',upgrade,
              description='Start a native primary Live WebSocket with session.start.')
    operation('post', '/v1/live/sessions/{session_id}/reject',
              {'200': {'description': 'Carrier acknowledged the SIP rejection; empty body'}},
              body=content('application/json', {'$ref':live+'session_reject'}))
    operation('post', '/v1/live/sessions/{session_id}/accept',
              {'200': {'description': 'Carrier confirmed the answer; empty body'}},
              body=content('application/json', {'$ref':live+'session_accept'}))
    for path in ('/v1/live/sessions/{session_id}/hangup', '/v1/realtime/calls/{call_id}/hangup'):
        operation('post', path, {'200': {'description': 'Carrier acknowledged hangup; empty body'}})
    realtime_reject = '/v1/realtime/calls/{call_id}/reject'
    status = dict(type='integer', minimum=300, maximum=699, default=603)
    operation('post', realtime_reject,
              {'200': {'description': 'Carrier acknowledged the SIP rejection; empty body'}},
              body=content('application/json', {'anyOf':[obj({'status_code':status}, []), {'type':'null'}]}))
    paths[realtime_reject]['post']['requestBody']['required'] = False
    fork = '/v1/live/sessions/{session_id}/fork'
    operation('get', fork, upgrade,
              description='Open a new Live WebSocket from a completed recording owned by this client. Send session.start with fork overrides.')
    operation('post', fork, {'201':response({'$ref':live+'session_forked'}, 'Forked media session created')},
              body=content('application/json', {'$ref':live+'session_fork'}))
    operation('get','/v1/live/sessions/{session_id}/attach',upgrade,
              parameters=[param('graceful_close','query',dict(type='boolean'))],
              description='Authenticated sideband for a running media session. Replays three seconds of events and audio before live delivery.')
    operation('get','/v1/live/sessions/{session_id}/content',{'200':dict(
        description='Completed owner-scoped stereo PCM WAV, input left and output right',
        headers={'Content-Length':dict(schema=dict(type='integer',minimum=44))},
        content=content('audio/wav',binary))})
    traces = '/v1/realtime/traces'
    trace = dict(type='object',properties=dict(id=ident,object=dict(const='realtime.trace'),session_id=text,
                 status=text,spans=dict(type='array',items=dict(type='object'))),
                 required=['id','object','session_id','status','spans'])
    operation('get',traces,json_result(listing(trace)),parameters=[*pagination,
        param('before','query',ident),param('order','query',dict(enum=['asc','desc'])),
        param('session_id','query',ident,description='Used alone.')])
    operation('get',traces+'/{trace_id}',json_result(trace))
    operation('delete',traces+'/{trace_id}',json_result(deleted('realtime.trace.deleted')))
