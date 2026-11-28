"""Use the native gateway to check five project records before and after recovery."""
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import openai

config=json.load(sys.stdin)
out=Path(config['out'])
mode=config['mode']
target=urlsplit(config['base'])
assert mode in ('prepare','restore') and target.hostname=='127.0.0.1' and target.scheme in ('http','https')
assert openai.__version__=='3.55.1' and len(config['records'])==5
def save(name,obj):
    (out/(mode+'-'+name+'.json')).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
calls=[]
def sending(request):
    assert len(calls)<12 and request.url.host==target.hostname and request.url.port==target.port
def observed(response):
    request=response.request
    response.read()
    value=response.json()
    scope='owner' if request.headers['authorization']=='Bearer '+config['key'] else 'foreign'
    if scope=='foreign':assert request.headers['authorization']=='Bearer '+config['other_key']
    calls.append(dict(method=request.method,path=request.url.path,query=request.url.query.decode(),
        body=json.loads(request.content) if request.content else None,scope=scope,
        status=response.status_code,trace=response.headers.get('x-request-id'),
        response=value))
    save('requests',calls)
    time.sleep(.125)
kwargs={}
if target.scheme=='https':
    import ssl
    kwargs['verify']=ssl.create_default_context(cafile=config['ca'])
with openai.DefaultHttpxClient(trust_env=False,timeout=35,event_hooks={'request':[sending],'response':[observed]},**kwargs) as http:
    sdk=openai.OpenAI(base_url=config['base'],api_key=config['key'],http_client=http,max_retries=0)
    other=openai.OpenAI(base_url=config['base'],api_key=config['other_key'],http_client=http,max_retries=0)
    if mode=='prepare':
        order=[]
        for name,digest in config['records'].items():
            store=sdk.vector_stores.create(name=name,metadata={'record_sha256':digest,'revision':'608'})
            order.append((store.created_at,store.id))
        ids=[id for _,id in sorted(order)]
        assert sdk.vector_stores.update(ids[2],name=None).name==''
    else:ids=config['previous']['ids']
    assert len(set(ids))==5 and sdk.vector_stores.retrieve(ids[2]).name==''
    for order in ('asc','desc'):
        values=ids if order=='asc' else list(reversed(ids))
        cursor=values[-1]
        for end in (4,2):
            page=sdk.vector_stores.list(limit=2,before=cursor,order=order)
            assert [r.id for r in page.data]==values[end-2:end]
            cursor=page.first_id
    try:other.vector_stores.retrieve(ids[2])
    except openai.NotFoundError:pass
    else:raise AssertionError('foreign client received a stored project record')
    if mode=='restore':
        for id in ids:assert sdk.vector_stores.delete(id).deleted
        assert not sdk.vector_stores.list(limit=1).data
assert len(calls)==12
save('actor',dict(sdk='python-'+openai.__version__,ids=ids,stock_sdk_indices=list(range(12))))
