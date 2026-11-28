"""Read JSON and SSE using the selected stock Python SDK."""
import json
from pathlib import Path
import ssl
import sys
import openai

plan = json.load(sys.stdin)
assert plan['sdk'] == 'python-' + openai.__version__
requests = []
def record(r):
    requests.append(dict(method=r.request.method,path=r.request.url.path,
                         status=r.status_code,trace=r.headers.get('x-request-id')))
def dump(value):
    return value.model_dump(mode='json', exclude_unset=True)
with openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=plan['ca']),
                               timeout=90,trust_env=False,event_hooks={'response':[record]}) as transport:
    with openai.OpenAI(base_url=plan['base'],api_key=plan['key'],http_client=transport,
                       timeout=90,max_retries=0) as client:
        with Path(plan['audio']).open('rb') as audio:
            answer=client.audio.transcriptions.create(file=('mixed.wav',audio,'audio/wav'),**plan['request'])
            unary=dump(answer)
        with Path(plan['audio']).open('rb') as audio:
            with client.audio.transcriptions.create(file=('mixed.wav',audio,'audio/wav'),stream=True,
                                                    **plan['request']) as stream:
                events=[dump(event) for event in stream]
print(json.dumps(dict(sdk=plan['sdk'],requests=requests,unary=unary,events=events)))
