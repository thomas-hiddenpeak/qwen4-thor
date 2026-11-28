"""Run ordinary interpreter creation, streaming and owner-isolated reads with the selected SDK."""
import json
import ssl
import sys
import openai

plan = json.load(sys.stdin)
assert plan['sdk'] == 'python-' + openai.__version__
requests = []

def observe(reply):
    url = reply.request.url
    requests.append(dict(method=reply.request.method, path=url.path, query=url.query.decode('ascii'),
                         status=reply.status_code, trace=reply.headers.get('x-request-id')))

with openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=plan['ca']), trust_env=False,
        timeout=90, event_hooks={'response':[observe]}) as http:
    client = openai.OpenAI(base_url=plan['base'], api_key=plan['key'], http_client=http, max_retries=0)
    container = client.containers.create(name='producer-sdk-read').id
    body = dict(plan['request'], tools=[dict(type='code_interpreter',container=container)])
    with client.responses.create(**body) as stream:
        events = [e.model_dump(mode='json',exclude_unset=True) for e in stream]
    response = events[-1]['response']
    stored = client.responses.retrieve(response['id'], include=['code_interpreter_call.outputs'])
    try:
        client.with_options(api_key=plan['foreign']).responses.retrieve(response['id'])
    except openai.NotFoundError:
        pass
    else:
        raise AssertionError('foreign SDK retrieved owned response')
    print(json.dumps(dict(sdk=plan['sdk'], requests=requests, container=container, events=events,
                          stored=stored.model_dump(mode='json',exclude_unset=True))))
