"""Wait for the two original Batch terminals and hash the successful output."""
import hashlib
import json
from pathlib import Path
import ssl
import sys
import time

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_cancel_receipt import check_loopback


def settle(config, requests):
    check_loopback(config)
    def observe(reply):
        req = reply.request
        requests.append(dict(method=req.method,path=req.url.path,query=req.url.query.decode('ascii'),
                             status=reply.status_code,trace=reply.headers.get('x-request-id')))

    tls = ssl.create_default_context(cafile=config.get('certificate'))
    hooks = {'response':[observe]}
    if config.get('managed_loopback') is True:
        hooks['request'] = [lambda _: time.sleep(.25)]
    transport = openai.DefaultHttpxClient(verify=tls, trust_env=False, event_hooks=hooks)
    with openai.OpenAI(api_key=config['key'],base_url=config['base'],max_retries=0,
                       timeout=45,http_client=transport) as api:
        writer = config['writer']
        result = {}
        for label, initial, terminal in [('finished',writer['completed'],'completed'),
                                        ('stopped',writer['pending'],'cancelled')]:
            for _ in range(15):
                job = api.batches.retrieve(initial['id'])
                assert job.input_file_id == initial['input'] and job.metadata == initial['metadata']
                if job.status in ('completed','failed','expired','cancelled'):
                    assert job.status == terminal
                    result[label] = job.model_dump(mode='json',exclude_unset=True)
                    break
                time.sleep(1.5)
            else:
                raise TimeoutError('Batch did not reach terminal state')
        raw = api.files.content(result['finished']['output_file_id']).read()
        lines = raw.splitlines()
        assert len(lines)==1
        row = json.loads(lines[0])
        assert row['custom_id']=='publication-0' and row['error'] is None and row['response']['status_code']==200
        assert row['response']['body']['choices'][0]['message']['content'].strip()==str(writer['revision'])
        assert all(r['status']==200 and r['query']=='' for r in requests)
        assert all(r['cancelling_at']==result['stopped']['cancelling_at'] for r in writer['cancel'])
        return dict(**result,output_sha256=hashlib.sha256(raw).hexdigest(),
                    output_bytes=len(raw),row_id=row['id'],requests=requests,sdk='python-'+openai.__version__)


if __name__ == '__main__':
    cfg = json.load(sys.stdin)
    requests = []
    try:
        print(json.dumps(settle(cfg, requests)))
    finally:
        if cfg.get('capture'):
            with Path(cfg['capture']).open('x') as stream:
                json.dump(requests, stream)
