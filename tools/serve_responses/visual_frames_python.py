"""Read a real Node-created video response in the ordinary and strict Python SDK."""
import json
import ssl
import sys
from urllib.parse import urlsplit

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


def observe(plan, strict):
    context = ssl.create_default_context(cafile=plan['certificate'])
    result = dict(strict=strict)
    transports = [httpx.Client(verify=context, trust_env=False, timeout=15) for _ in range(2)]
    owner, peer = [openai.OpenAI(base_url=plan['base'], api_key=plan[key],
                                 http_client=transport, max_retries=0,
                                 _strict_response_validation=strict)
                   for key, transport in zip(('key', 'other'), transports)]
    try:
        try:
            value = owner.responses.retrieve(plan['id'])
        except openai.APIResponseValidationError as error:
            if not strict:
                raise
            result['validation_errors'] = [
                dict(path=list(item['loc']), kind=item['type']) for item in error.__cause__.errors()]
        else:
            parts = [part for item in value.output if item.type == 'message' for part in item.content]
            if (value.id != plan['id'] or value.status != 'completed' or len(parts) != 1
                    or parts[0].type != 'output_text' or parts[0].text != plan['text']):
                raise ValueError('Python SDK returned different stored video content')
            result.update(id=value.id, part=parts[0].type, text=parts[0].text,
                          retention=value.prompt_cache_retention)
        try:
            peer.responses.retrieve(plan['id'])
        except openai.NotFoundError as error:
            result['peer_status'] = error.status_code
        else:
            raise ValueError('Another account retrieved the stored video response')
    finally:
        owner.close()
        peer.close()
    return result


def main():
    plan = json.load(sys.stdin)
    target = urlsplit(plan['base'])
    if target.scheme != 'https' or target.hostname != '127.0.0.1' or target.path != '/v1':
        raise ValueError('Expected the local native TLS SDK fixture')
    rows = [observe(plan, strict) for strict in (False, True)]
    print(json.dumps(dict(sdk=openai.__version__, observations=rows), ensure_ascii=False))


if __name__ == '__main__':
    main()
