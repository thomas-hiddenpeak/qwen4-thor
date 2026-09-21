"""Compare packed full-row and per-sequence last-row heads after all required HTTP gates."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ['src/model/model.cu', 'include/q4t/model/model.h',
           'src/server/chat_server.cpp']
LIBS = ['model', 'ple', 'quant', 'text', 'io', 'runtime']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-root', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, out = args.accepted_root.resolve(), args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    if not json.loads((run / 'acceptance.json').read_text()).get('performance_accepted'):
        parser.error('requires full HTTP performance acceptance')
    if not json.loads((run / 'interleave/review.json').read_text())['accepted']:
        parser.error('requires accepted interleave HTTP')
    if json.loads((run / 'lifecycle/complete.json').read_text())['passed'] != 4:
        parser.error('requires four lifecycle HTTP cases')
    batch_gate = json.loads((run / 'batch-http/review.json').read_text())
    if not batch_gate['accepted'] or not batch_gate['batch_coverage_passed']:
        parser.error('requires accepted actual B=2 HTTP coverage')
    sha = digest(ROOT / 'build/q4t')
    for mode, count in [('quality', 11), ('performance', 5)]:
        gate = json.loads((run / mode / 'exit.json').read_text())
        if (gate['completed'] != count or gate['server'] != 0
                or gate['failure'] is not None or not gate['http_output_checks_passed']):
            parser.error('incomplete HTTP gate')
        if sha != (run / mode / 'binary.sha256').read_text().strip():
            parser.error('binary differs from accepted HTTP run')
    for source in SOURCES:
        if (ROOT / source).read_bytes() != (run / 'source' / source).read_bytes():
            parser.error('source differs from HTTP snapshot')
    libraries = json.loads((run / 'libraries.json').read_text())
    for name, expected in libraries.items():
        if digest(ROOT / name) != expected:
            parser.error('library differs from candidate build')
    template = Path(__file__).with_suffix('.cu.in')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'comparison.cu').write_text(template.read_text())
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(template, out / template.name)
    cache = (ROOT / 'build/CMakeCache.txt').read_text()
    compiler = re.search(r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    host = re.search(r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    command = [compiler, '-std=c++23', '--expt-relaxed-constexpr', '-O3',
               '-arch=sm_110a', '-ccbin', host, '-Xcompiler=-Wall,-Wextra',
               '-I' + str(ROOT / 'include'), str(out / 'comparison.cu'),
               '-Xlinker=--start-group']
    command += [str(ROOT / f'build/libq4t_{name}.a') for name in LIBS]
    command += ['-Xlinker=--end-group', '-lcublasLt', '-luring', '-licui18n',
                '-licuuc', '-licudata', '-ldl', '-lpthread', '-lrt',
                '-o', str(out / 'comparison')]
    (out / 'manifest.json').write_text(json.dumps({
        'binary_sha256': sha,
        'library_sha256': libraries, 'compile_command': command,
        'accepted_root': str(run)}, indent=2) + '\n')
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    if 'warning' in (out / 'build.log').read_text().lower():
        raise RuntimeError('numerical build warning')
    for length in [1024, 4096, 8192]:
        fixture = run / f'performance/context-{length}/requests.jsonl'
        prompt = json.loads(fixture.read_text().splitlines()[0])['prompt']
        (out / f'prompt-{length}.txt').write_text(prompt)
    with (out / 'result.log').open('w') as log:
        execute = [str(out / 'comparison'), str(args.model_dir), str(out)]
        result = subprocess.run(execute, stdout=log, stderr=subprocess.STDOUT)
    (out / 'exit.json').write_text(json.dumps({'numerical': result.returncode}) + '\n')
    print((out / 'result.log').read_text(), end='')
    result.check_returncode()


if __name__ == '__main__':
    main()
