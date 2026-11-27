"""Read the q4t event catalogue with an installed stock Python SDK."""
import json
import ssl
import sys

import openai


def main():
    config = json.load(sys.stdin)
    ca = ssl.create_default_context(cafile=config['certificate'])
    with openai.DefaultHttpxClient(verify=ca, trust_env=False) as transport:
        with openai.OpenAI(api_key=config['key'], base_url=config['base'],
                           http_client=transport, max_retries=0, timeout=8,
                           _strict_response_validation=True) as client:
            resource = getattr(getattr(client, 'webhooks', None), 'event_types', None)
            if resource is None:
                data = client.get('/webhook_event_types', cast_to=dict)
                mode = 'generic'
            else:
                data = resource.list().model_dump()
                mode = 'resource'
    assert data['object'] == 'list'
    print(json.dumps(dict(language='python', version=openai.__version__,
                          mode=mode, data=data['data'])))


if __name__ == '__main__':
    main()
