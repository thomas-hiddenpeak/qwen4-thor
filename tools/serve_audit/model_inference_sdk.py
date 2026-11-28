"""Read the model catalogue and use its model ID for text and embedding requests."""
import json
import ssl
import sys

import openai


def run(config):
    calls = []

    def observe(reply):
        url = reply.request.url
        calls.append(dict(method=reply.request.method, path=url.path,
                          query=url.query.decode('ascii'), status=reply.status_code,
                          trace=reply.headers.get('x-request-id')))

    tls = ssl.create_default_context(cafile=config['certificate'])
    transport = openai.DefaultHttpxClient(verify=tls, trust_env=False, event_hooks={'response': [observe]})
    with openai.OpenAI(base_url=config['base'], api_key=config['key'], http_client=transport,
                       max_retries=0, timeout=60) as client:
        catalogue = [m.model_dump(mode='json') for m in client.models.list().data]
        selected = next(m['id'] for m in catalogue if m['id'] == config['model'])
        model = client.models.retrieve(selected).model_dump(mode='json')
        try:
            client.models.retrieve('q4t-missing-business-model')
        except openai.NotFoundError as exc:
            assert exc.status_code == 404
        else:
            raise ValueError('Unknown model was retrievable')
        body = dict(config['completion'], model=selected)
        completion = client.completions.create(**body).model_dump(mode='json', exclude_unset=True)
        with client.completions.create(**body, stream=True, stream_options={'include_usage': True}) as stream:
            chunks = [chunk.model_dump(mode='json', exclude_unset=True) for chunk in stream]
        embeddings = {}
        for encoding in ('float', 'base64'):
            response = client.embeddings.create(model=selected, input=config['documents'],
                                                encoding_format=encoding, dimensions=config['dimensions'])
            embeddings[encoding] = dict(object=response.object, model=response.model,
                usage=response.usage.model_dump(mode='json'),
                data=[dict(object=r.object, index=r.index, embedding=r.embedding) for r in response.data])
    return dict(sdk='python-'+openai.__version__, catalogue=catalogue, model=model, completion=completion,
                chunks=chunks, embeddings=embeddings, requests=calls)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin)), allow_nan=False))
