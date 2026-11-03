"""Small synthetic weights/images, only written inside the stage work tree."""
import json
from pathlib import Path
import struct
import zlib


def prepare(folder):
    folder = Path(folder)
    folder.mkdir()
    sizes = {'patch_embed.proj.weight': 8*3*2*2*2,
             'patch_embed.proj.bias': 8, 'pos_embed.weight': 16*8}
    block = dict(zip(('norm1.weight', 'norm1.bias', 'attn.qkv.weight',
        'attn.qkv.bias', 'attn.proj.weight', 'attn.proj.bias', 'norm2.weight',
        'norm2.bias', 'mlp.linear_fc1.weight', 'mlp.linear_fc1.bias',
        'mlp.linear_fc2.weight', 'mlp.linear_fc2.bias'),
        (8, 8, 24*8, 24, 8*8, 8, 8, 8, 16*8, 16, 8*16, 8)))
    for i in range(2): sizes.update({f'blocks.{i}.{k}': v for k, v in block.items()})
    sizes.update({'merger.norm.weight': 8, 'merger.norm.bias': 8,
                  'merger.linear_fc1.weight': 32*32, 'merger.linear_fc1.bias': 32,
                  'merger.linear_fc2.weight': 8*32, 'merger.linear_fc2.bias': 8})
    for missing in (False, True):
        path = folder/('missing' if missing else 'tiny'); path.mkdir()
        header, offset, mapping = {}, 0, {}
        for name, size in sizes.items():
            if missing and name == 'blocks.1.mlp.linear_fc2.bias': continue
            key = 'model.visual.'+name
            header[key] = dict(dtype='BF16', shape=[size], data_offsets=[offset, offset+size*2])
            mapping[key] = 'vision.safetensors'
            offset += size*2
        raw = json.dumps(header).encode(); raw += b' '*((-len(raw)) % 8)
        (path/'vision.safetensors').write_bytes(struct.pack('<Q', len(raw))+raw+b'\0'*offset)
        (path/'model.safetensors.index.json').write_text(json.dumps(dict(weight_map=mapping)))
    def chunk(kind, raw):
        return struct.pack('>I', len(raw))+kind+raw+struct.pack('>I', zlib.crc32(kind+raw))
    rgb = (b'\0'+b'\xff\0\0'*256)*256
    png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 256, 8, 2, 0, 0, 0))+
           chunk(b'IDAT', zlib.compress(rgb))+chunk(b'IEND', b''))
    (folder/'red.png').write_bytes(png)
