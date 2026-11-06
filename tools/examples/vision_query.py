"""One bounded image/video request, with discovery and expanded preflight.

The optional video adapter uses CPU decoding. No automatic retries or tool
execution; JSON/SSE responses are committed only after a complete terminal.
Use explicit media service profile and review its release limitations.
"""
import argparse
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


def build_request(question, images=(), video=None, frames=8, stream=True):
    require(isinstance(question,str) and question.strip(),'nonempty question required')
    require(1 <= len(images)+int(video is not None) <= 8,'one to eight media items required')
    parts=[image_part(p) for p in images]
    if video is not None:parts.append(sample_video(video,frames=frames,backend='cpu'))
    parts.append(dict(type='text',text=question))
    return make_request([dict(role='user',content=parts)],[],stream=stream,
                        thinking=False,max_tokens=256,timeout_ms=120000)


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
    p.add_argument('--ca-file',type=Path)
    p.add_argument('--api-key-file',type=Path)
    args=p.parse_args()
    key=None
    if args.api_key_file:
        from tools.serve_gateway.gateway import read_file
        key=read_file(args.api_key_file,maximum=4096,private=True).decode().strip()
    client=ChatClient(args.base_url,api_key=key,ca_file=str(args.ca_file) if args.ca_file else None)
    body=build_request(args.question,args.image,args.video,args.frames,not args.no_stream)
    print(json.dumps(query(client,body),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
