"""Replay the four CPU audit probes in a fresh .q4t-work child directory.

This does not load the model, link CUDA runtime libraries, or run real HTTP.
Probe exit zero means the observation completed, not that the defect is fixed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def residency_fixture(root):
    root.mkdir()
    prefix = 'model.language_model.layers.0.mlp.experts.0.'
    metadata, index = {}, {}

    def tensor(name, start, end, shape, dtype='U8', shard='a'):
        name = prefix + name
        metadata.setdefault(shard, {})[name] = {
            'dtype': dtype, 'shape': shape, 'data_offsets': [start, end]}
        index[name] = shard

    w, s = 4096, 512
    for i, proj in enumerate(('down_proj', 'gate_proj', 'up_proj')):
        tensor(proj + '.weight', i*w, (i+1)*w,
               [128, 32] if i == 0 else [64, 64],
               shard='a' if i == 0 else 'b')
        tensor(proj + '.weight_scale', 3*w+i*s, 3*w+(i+1)*s,
               [128, 4] if i == 0 else [64, 8], 'F8_E4M3')
        for j, field in enumerate(('input_scale', 'weight_scale_2')):
            start = 3*w+3*s+(2*i+j)*4
            tensor(proj + '.' + field, start, start+4, [], 'F32')
    metadata['a']['padding'] = {
        'dtype': 'U8', 'shape': [2*w], 'data_offsets': [w, 3*w]}
    metadata['b']['padding'] = {
        'dtype': 'U8', 'shape': [w], 'data_offsets': [0, w]}
    payloads = {
        'a': bytes([17])*w + bytes([161])*w + bytes([162])*w +
             bytes([56])*(3*s) + struct.pack('<6f', *([1.0]*6)),
        'b': bytes(w) + bytes([34])*w + bytes([51])*w}
    for shard, payload in payloads.items():
        header = json.dumps(metadata[shard], separators=(',', ':')).encode()
        (root/shard).write_bytes(struct.pack('<Q', len(header)) + header + payload)
    (root/'index.json').write_text(json.dumps({'weight_map': index}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    # The frozen watchdog probe locates the repo two parents above its file.
    if out.parent != ROOT/'.q4t-work':
        parser.error('output must be a new direct child of the repo .q4t-work')
    out.mkdir(exist_ok=False)
    for name in ('io_probe.cpp', 'capacity_probe.cpp', 'residency_probe.cpp',
                 'watchdog_probe.py'):
        shutil.copy2(HERE/name, out/name)
    residency_fixture(out/'residency-fixture')
    common = ['g++-14', '-O2', '-std=c++23', '-Wall', '-Wextra', '-Werror',
              '-ffunction-sections', '-fdata-sections', '-Iinclude',
              '-I/usr/local/cuda/include']
    io_sources = ['src/io/safetensors.cpp', 'src/io/json.cpp']
    probes = {
        'io_probe': (io_sources, [str(out/'io-fixture')]),
        'capacity_probe': (['src/server/server_options.cpp',
                            'src/runtime/memory_budget.cpp'], []),
        'residency_probe': (['src/quant/moe_residency.cpp',
                             'src/io/weight_loader.cpp'] + io_sources,
                            [str(out/'residency-fixture')])}
    commands = []
    identity = {}
    env = dict(os.environ, Q4T_MOE_LOAD_THREADS='1', Q4T_MOE_L2_SLOTS='1',
               Q4T_MOE_MIRROR_K='0', PYTHONDONTWRITEBYTECODE='1')
    # Inherited residency fault injection would change this synthetic case.
    env.pop('Q4T_RESIDENCY_FAIL_EXPERT', None)
    env.pop('LD_PRELOAD', None)

    def run(name, command):
        with (out/(name+'.log')).open('w') as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=120)
        commands.append({'name': name, 'command': command,
                         'exit': result.returncode})
        (out/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')
        result.check_returncode()

    for name, (sources, arguments) in probes.items():
        for path in sources:
            identity[path] = hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
        command = common + [str(out/(name+'.cpp'))] + sources + [
            '-Wl,--gc-sections', '-pthread', '-o', str(out/name)]
        run(name+'-build', command)
        run(name, [str(out/name)] + arguments)
    run('watchdog_probe', ['/usr/bin/python3', '-B', str(out/'watchdog_probe.py')])
    (out/'source-identity.json').write_text(json.dumps(identity, indent=2)+'\n')
    print(out)


if __name__ == '__main__':
    main()
