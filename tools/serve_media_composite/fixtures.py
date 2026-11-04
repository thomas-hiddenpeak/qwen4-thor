"""Fixed malformed media inputs and a bounded, streaming-generated IDAT fixture."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import zlib

from tools.serve_media_failure import fixtures as previous
from tools.serve_media_failure.verify import media_body

FAULT_REQUEST = 'srv02i-admission'
CANCEL_REQUEST = 'srv02i-media-cancel'
DECODER_LIMIT = 256*1024*1024
NAMES = ('base64', 'mime', 'detail', 'source', 'frame-size', 'rgb-total',
         'patch-total', 'prefill', 'context', 'attention', 'decoder-quota')


def chunk(kind, data):
    return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data))


def header(width, height):
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',
        struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))


def url(data, mime='png'):
    return 'data:image/'+mime+';base64,'+base64.b64encode(data).decode()


def image(data):
    return dict(type='image_url', image_url=dict(url=url(data)))


def cases(folder):
    folder = Path(folder)
    rows = []
    part = 'messages[0].content[0]'
    for name in NAMES:
        body = media_body(folder)
        body.update(request_id=FAULT_REQUEST,
                    cancel_token=hashlib.sha256(('srv02i:'+name).encode()).hexdigest())
        parts = body['messages'][0]['content']
        param, code = part+'.image_url', None
        if name == 'base64':
            parts[0]['image_url']['url'] = 'AB=='
            code = 'media_base64_invalid'
        elif name == 'mime':
            parts[0]['image_url']['url'] = url((folder/'red.png').read_bytes(), 'jpeg')
            code = 'media_format_mismatch'
        elif name == 'detail':
            parts[0]['image_url']['detail'] = 'high'
            code = 'invalid_request'
        elif name == 'source':
            parts[0] = image(header(16385, 32)); code = 'media_source_limit'
        elif name in ('frame-size', 'rgb-total'):
            data = ([header(256, 256), header(320, 256)] if name == 'frame-size'
                    else [header(4096, 4096)]*3)
            parts[0] = dict(type='video_frames', video_frames=[url(p) for p in data])
            code = 'media_frame_dimensions' if name == 'frame-size' else 'media_decoded_limit'
            param = part+('.video_frames[1]' if name == 'frame-size' else '.video_frames[2]')
        elif name == 'patch-total':
            parts[:] = [image(header(2048, 2048)) for _ in range(5)]+parts[1:]
            code, param = 'media_patch_limit', 'messages[0].content[4].image_url'
        elif name in ('prefill', 'context'):
            count = 1 if name == 'prefill' else 4
            parts[:] = [image(header(1536, 1536)) for _ in range(count)]+parts[1:]
            code = 'media_prefill_limit' if count == 1 else 'context_length_exceeded'
            param = 'messages'
            body['stream'] = count == 1
        elif name == 'attention':
            parts[0] = image(header(2048, 2048)); code = 'media_attention_limit'
        elif name == 'decoder-quota':
            parts[0] = image((folder/'decoder-quota.png').read_bytes())
            code = 'media_decode_limit'; body['stream'] = True
        rows.append(dict(name=name, body=body, code=code, param=param,
                         status=200 if body['stream'] else 400))
    return rows


def prepare(folder):
    folder = Path(folder)
    previous.prepare(folder)
    compressor = zlib.compressobj(9)
    block = b'\0'*(1024*1024)
    compressed = bytearray()
    for _ in range(DECODER_LIMIT//len(block)):
        compressed.extend(compressor.compress(block))
    compressed.extend(compressor.compress(b'\0'))
    compressed.extend(compressor.flush())
    payload = header(1, 1)+chunk(b'IDAT', bytes(compressed))+chunk(b'IEND', b'')
    assert len(payload) < 1024*1024
    (folder/'decoder-quota.png').write_bytes(payload)
    (folder/'cases.json').write_text(json.dumps(cases(folder), indent=2, ensure_ascii=False)+'\n')
    (folder/'fixture-contract.json').write_text(json.dumps(dict(
        decoder_owned_limit=DECODER_LIMIT, idat_expanded_bytes=DECODER_LIMIT+1,
        generation_block_bytes=len(block), compressed_png_bytes=len(payload),
        claims_whole_process_ram_limit=False), indent=2)+'\n')
