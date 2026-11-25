"""Check an existing video character and a Python-created reference across recovery."""

import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


def check(client, resource, clip, previous=None):
    received = client.videos.get_character(resource['id'])
    if received.model_dump(mode='json') != resource:
        raise ValueError('The Python SDK did not recover the original character')
    if previous is None:
        with Path(clip).open('rb') as video:
            created = client.videos.create_character(
                name='Python 双轨参考', video=('reference.mp4', video, 'video/mp4'))
        previous = created.model_dump(mode='json')
        if (previous['name'] != 'Python 双轨参考' or
                not previous['id'].startswith('char_') or
                previous['id'] == resource['id']):
            raise ValueError('The new character has an unexpected identity')
    retained = client.videos.get_character(previous['id'])
    if retained.model_dump(mode='json') != previous:
        raise ValueError('The Python-created reference changed on retrieval')
    return dict(version=openai.__version__, node_resource=resource,
                python_resource=previous, retrieved_type=type(retained).__name__)


def main():
    plan = json.load(sys.stdin)
    endpoint = urlsplit(plan['base'])
    if endpoint.scheme == 'https':
        verify = ssl.create_default_context(cafile=plan['ca'])
    elif endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1':
        verify = True
    else:
        raise ValueError('Expected HTTPS or the loopback managed acceptance service')
    with httpx.Client(verify=verify, trust_env=False, timeout=30) as transport:
        client = openai.OpenAI(base_url=plan['base'], api_key=plan['key'],
                               http_client=transport, max_retries=0,
                               _strict_response_validation=True)
        result = check(client, plan['resource'], plan['clip'], plan.get('previous'))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
