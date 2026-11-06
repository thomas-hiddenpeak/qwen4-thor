"""One bounded image/video request, with discovery and expanded preflight.

The optional video adapter uses CPU decoding. No automatic retries or tool
execution; JSON/SSE responses are committed only after a complete terminal.
Use explicit media service profile and review its release limitations.
"""
import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

try:
    from .chat_client import ChatClient, make_request, require
    from .vision_input import image_part, sample_video
except ImportError:
    from chat_client import ChatClient, make_request, require
    from vision_input import image_part, sample_video


def with_json_schema(body, schema):
    """Make the caller's schema visible to the model as well as the decoder.

    Constrained decoding only restricts legal tokens. It cannot communicate
    enum meanings or guarantee a correct observation. No response rewriting.
    """
    require(isinstance(schema, dict) and schema.get('type') == 'object',
            'an object JSON Schema is required')
    encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False)
    require(len(encoded.encode('utf-8')) <= 65536, 'schema exceeds 64 KiB')
    result = deepcopy(body)
    require('response_format' not in result, 'response_format already set')
    last = result['messages'][-1]
    require(last.get('role') == 'user' and isinstance(last.get('content'), list),
            'a final user content-parts message is required')
    last['content'].append(dict(type='text', text=(
        'Return exactly one JSON object matching the following JSON Schema. '
        'Use its descriptions to interpret the fields and enum values. '
        'Do not include markdown fences.\n' + encoded)))
    result['response_format'] = dict(type='json_schema', json_schema=dict(
        name='visual_answer', strict=True, schema=deepcopy(schema)))
    return result


def build_request(question, images=(), video=None, frames=8, stream=True,
                  schema=None):
    require(isinstance(question,str) and question.strip(),'nonempty question required')
    require(1 <= len(images)+int(video is not None) <= 8,'one to eight media items required')
    parts=[image_part(p) for p in images]
    if video is not None:parts.append(sample_video(video,frames=frames,backend='cpu'))
    parts.append(dict(type='text',text=question))
    body = make_request([dict(role='user',content=parts)],[],stream=stream,
                        thinking=False,max_tokens=256,timeout_ms=120000)
    return with_json_schema(body, schema) if schema is not None else body


def query(client,body):
    caps=client.capabilities(model=body['model'])
    runtime=caps.get('runtime',{})
    require(runtime.get('media_enabled') is True and runtime.get('mtp_enabled') is False,
            'explicit no-MTP media service required')
    pre=client.preflight(body,protocol_sha256=caps['protocol_source_sha256'])
    require(pre.get('media',{}).get('token_ids')=='expanded_for_inference',
            'expanded media preflight required')
    result=client.complete(body)
    require(result.usage['prompt_tokens']==pre['prompt_tokens'],'input token accounting mismatch')
    return dict(completion=asdict(result),preflight=pre,automatic_retry=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:8080')
    p.add_argument('--image',type=Path,action='append',default=[])
    p.add_argument('--video',type=Path)
    p.add_argument('--frames',type=int,default=8)
    p.add_argument('--question',required=True)
    p.add_argument('--no-stream',action='store_true')
    p.add_argument('--schema-file',type=Path,
                   help='optional object schema, also included in the prompt')
    p.add_argument('--ca-file',type=Path)
    p.add_argument('--api-key-file',type=Path)
    args=p.parse_args()
    key=None
    if args.api_key_file:
        from tools.serve_gateway.gateway import read_file
        key=read_file(args.api_key_file,maximum=4096,private=True).decode().strip()
    client=ChatClient(args.base_url,api_key=key,ca_file=str(args.ca_file) if args.ca_file else None)
    schema = None
    if args.schema_file:
        with args.schema_file.open('rb') as source:
            raw = source.read(65537)
        require(len(raw) <= 65536, 'schema file exceeds 64 KiB')
        schema = json.loads(raw.decode('utf-8'))
    body=build_request(args.question,args.image,args.video,args.frames,
                       not args.no_stream,schema)
    print(json.dumps(query(client,body),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
