"""HTTP declarations for the implemented speech, classification and socket endpoints."""


def speech_voice_schema():
    text = dict(type='string', minLength=1, maxLength=256,
                **{'not': {'pattern': r'\u0000'}})
    identifier = dict(type='object', properties={'id': text}, required=['id'],
                      additionalProperties=False)
    return dict(anyOf=[text, identifier])


def add_voice_routes(paths, response, error):
    from tools.serve_protocol.event_schema import event_stream
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
        voice=speech_voice_schema(),
        instructions=dict(type='string',maxLength=4096),
        speed=dict(type='number',minimum=.25,maximum=4,default=1),
        response_format=dict(enum=['mp3','opus','aac','flac','wav','pcm'],default='mp3'),
        stream_format=dict(enum=['audio','sse'],default='audio')), ['model','input','voice'])
    speech_types = ('audio/mpeg','audio/ogg','audio/opus','audio/aac','audio/flac',
                    'audio/wav','audio/pcm','application/octet-stream')
    body = {mime:dict(schema=binary) for mime in speech_types}
    count = dict(type='integer', minimum=0)
    usage = obj(dict(input_tokens=count, output_tokens=count, total_tokens=count),
                ['input_tokens', 'output_tokens', 'total_tokens'])
    delta = obj(dict(type=dict(const='speech.audio.delta'),
                     audio=dict(type='string', minLength=1, contentEncoding='base64')),
                ['type', 'audio'])
    done = obj(dict(type=dict(const='speech.audio.done'), usage=usage),
               ['type', 'usage'])
    failure = {'$ref': 'serve-application-v1.schema.json#/$defs/transcription_error'}
    body['text/event-stream'] = event_stream(dict(oneOf=[delta, done, failure]))
    operation('post','/v1/audio/speech',{'200':dict(description='Audio or server-sent speech events',
              headers={'Transfer-Encoding': dict(schema=dict(type='string', const='chunked'),
                  description='Present for chunk-framed HTTP/1.1 audio or event streams.')},
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
    consent_page = listing(consent)
    cursor = dict(anyOf=[ident, dict(type='null')])
    consent_page['properties'].update(first_id=cursor, last_id=cursor)
    operation('get',consents,json_result(consent_page),parameters=pagination)
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
        param('call_id','query',dict(type='string',pattern='^rtc_[A-Za-z0-9_-]+$',maxLength=128),
              description='Attach to the owning Realtime call; mutually exclusive with model and intent.'),
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
        operation('post', path, {'200': {'description': 'Call and media transport closed; empty body'}})
    realtime = 'serve-application-v1.schema.json#/$defs/realtime_'
    secrets = '/v1/realtime/client_secrets'
    operation('post', secrets, json_result({'$ref':realtime+'client_secret_created'}),
              body=content('application/json', {'$ref':realtime+'client_secret_create'}))
    paths[secrets]['post']['requestBody']['required'] = False
    operation('post', '/v1/realtime/translations/client_secrets',
              json_result({'$ref':realtime+'translation_secret_created'}),
              body=content('application/json', {'$ref':realtime+'translation_secret_create'}))
    operation('get', '/v1/realtime/translations', upgrade,
              parameters=[param('model', 'query', dict(const='gpt-realtime-translate'))],
              **{'x-q4t-client-event':{'$ref':realtime+'translation_client_event'},
                 'x-q4t-server-event':{'$ref':realtime+'translation_server_event'}})
    legacy = '/v1/realtime/transcription_sessions'
    # Reuse the nested transcription input contracts from the generated GA schema.
    def input_field(name):
        return {'$ref':realtime+'508/properties/'+name}
    transcription = {'input_audio_format':dict(enum=['pcm16','g711_ulaw','g711_alaw']),
                     'input_audio_transcription':{'$ref':realtime+'198'},
                     'input_audio_noise_reduction':input_field('noise_reduction'),
                     'turn_detection':dict(anyOf=[input_field('turn_detection'),
                         obj(dict(type=dict(const='semantic_vad'),
                                  eagerness=dict(enum=['auto','low','medium','high'])), ['type'])]),
                     'include':dict(type='array',maxItems=1,
                                    items=dict(const='item.input_audio_transcription.logprobs'))}
    ticket = obj(dict(value=dict(type='string',minLength=1), expires_at=dict(type='integer')),
                 ['value','expires_at'])
    created = obj(dict(transcription, id=ident, object=dict(const='realtime.transcription_session'),
                       modalities=dict(const=['text']), client_secret=ticket),
                  ['id','object','client_secret','input_audio_format'])
    duration = obj(dict(anchor=dict(const='created_at'), seconds=dict(type='integer',minimum=10,maximum=7200)), [])
    request = obj(dict(transcription, client_secret=obj({'expires_at':duration}, []), modalities=dict(const=['text'])), [])
    operation('post', legacy, json_result(created), body=content('application/json', request))
    paths[legacy]['post']['requestBody']['required'] = False
    conversation = '/v1/realtime/sessions'
    def setting(name):
        return {'$ref':realtime+'187/properties/'+name}
    options = {k:setting(k) for k in ('model','instructions','prompt','tools','tool_choice','tracing','truncation')}
    options.update({k:v for k,v in transcription.items() if k != 'include'})
    options.update(modalities=dict(type='array', minItems=1, maxItems=2, uniqueItems=True,
                                   items=dict(enum=['text','audio'])),
                   output_audio_format=transcription['input_audio_format'],
                   temperature=dict(type='number', minimum=.6, maximum=1.2, default=.8),
                   speed=dict(type='number', minimum=.25, maximum=1.5),
                   voice={'$ref':realtime+'218/properties/voice'},
                   max_response_output_tokens=dict(anyOf=[dict(const='inf'),
                       dict(type='integer',minimum=1,maximum=4096)]))
    legacy_body = obj(dict(options, client_secret=obj({'expires_after':duration}, [])), [])
    legacy_resource = obj(dict(options, id=ident, object=dict(const='realtime.session'),
                               client_secret=ticket), ['client_secret','id','object','temperature','modalities'])
    operation('post', conversation, json_result(legacy_resource),
              body=content('application/json', legacy_body))
    paths[conversation]['post']['requestBody']['required'] = False
    sdp = dict(type='string',minLength=1,maxLength=65535)
    for path, session_shape in (('/v1/realtime/calls', 'session'),
                                ('/v1/realtime/translations/calls', 'translation_session_create')):
        form = obj(dict(sdp=sdp, session={'$ref':realtime+session_shape}), ['sdp'])
        operation('post',path, {'201':dict(description='WebRTC SDP answer',
                  headers={'Location':dict(required=True, schema=dict(type='string',
                      pattern='^/v1/realtime/calls/rtc_[A-Za-z0-9_-]+$'))},
                  content=content('application/sdp',sdp))}, body={
                      **content('application/sdp',sdp),
                      'multipart/form-data':dict(schema=form, encoding={
                          'sdp':dict(contentType='application/sdp'),
                          'session':dict(contentType='application/json')})})
    operation('post', '/v1/realtime/calls/{call_id}/accept',
              {'200': {'description': 'Realtime session ready and SIP answer acknowledged; empty body'}},
              body=content('application/json', {'$ref':realtime+'call_accept'}))
    for path, schema in [('/v1/live/sessions/{session_id}/refer', live+'session_refer'),
                         ('/v1/realtime/calls/{call_id}/refer', realtime+'call_refer')]:
        operation('post', path, {'200': {'description': 'Carrier accepted the SIP REFER; empty body'}},
                  body=content('application/json', {'$ref':schema}))
    realtime_reject = '/v1/realtime/calls/{call_id}/reject'
    operation('post', realtime_reject,
              {'200': {'description': 'Carrier acknowledged the SIP rejection; empty body'}},
              body=content('application/json', {'$ref':realtime+'call_reject'}))
    paths[realtime_reject]['post']['requestBody']['required'] = False
    fork = '/v1/live/sessions/{session_id}/fork'
    operation('get', fork, upgrade,
              description='Open a new Live WebSocket from a completed recording owned by this client. Send session.start with fork overrides.')
    operation('post', fork, {'201':response({'$ref':live+'session_forked'}, 'Forked media session created')},
              body=content('application/json', {'$ref':live+'session_fork'}))
    operation('get','/v1/live/sessions/{session_id}/attach',upgrade,
              parameters=[param('graceful_close','query',dict(type='boolean'))],
              description='Authenticated sideband for a running media session. Replays three seconds of events and audio before live delivery.')
    unavailable = response(error, 'Recording export temporarily unavailable')
    unavailable['headers']['Retry-After'] = dict(schema=dict(type='integer', minimum=1),
        description='Minimum delay in seconds for a temporary export admission or recovery failure.')
    operation('get','/v1/live/sessions/{session_id}/content',{'503': unavailable, '200':dict(
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
