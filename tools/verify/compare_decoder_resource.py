"""Check linked decoder resource descriptions after complete HTTP acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-root', type=Path, required=True)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, out = args.accepted_root.resolve(), args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    assert json.loads((run / 'acceptance.json').read_text())['performance_accepted']
    sha = digest(ROOT / 'build/q4t')
    for mode, count in [('quality', 11), ('performance', 5)]:
        gate = json.loads((run / mode / 'exit.json').read_text())
        assert gate['completed'] == count and gate['server'] == 0
        assert gate['failure'] is None and gate['http_output_checks_passed']
        assert (run / mode / 'binary.sha256').read_text().strip() == sha
    for name in ['src/model/decoder_layer.cu', 'include/q4t/model/decoder_layer.h',
                 'src/model/model.cu', 'src/model/hyperconnection.cu',
                 'include/q4t/model/hyperconnection.h',
                 'include/q4t/model/decoder_workspace.h']:
        assert (ROOT / name).read_bytes() == (run / 'source' / name).read_bytes()
    libraries = json.loads((run / 'libraries.sha256.json').read_text())
    for name, expected in libraries.items():
        assert digest(ROOT / name) == expected
    baseline_ref = subprocess.check_output(
        ['git', 'rev-parse', args.baseline_ref + '^{commit}'], text=True, cwd=ROOT).strip()
    old = subprocess.check_output(
        ['git', 'show', baseline_ref + ':src/model/decoder_layer.cu'],
        text=True, cwd=ROOT)
    new = (ROOT / 'src/model/decoder_layer.cu').read_text()
    helper = old[old.index('size_t AttnWs('):old.index('// One source for both')]
    old_layout = old[old.index('struct DecoderWorkspaceLayout'):old.index('\n}  // namespace')]
    # Forward implementation must remain identical to the audited baseline.
    forward_marker = 'Status DecoderLayerForward('
    assert old[old.index(forward_marker):] == new[new.index(forward_marker):]
    fields = re.findall(r'^  size_t (\w+) = 0;', old_layout, re.M)
    assert len(fields) == 20, fields
    checks = '\n'.join(f'Require(old.{f} == now.{f}, "unchanged {f}");' for f in fields)
    template = Path(__file__).with_suffix('.cu.in')
    source = template.read_text()
    for tag, body in [('HELPER', helper), ('OLD_LAYOUT', old_layout),
                      ('FIELD_COMPARISONS', checks)]:
        source = source.replace('// ' + tag, body)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'comparison.cu').write_text(source)
    (out / 'baseline.cu.txt').write_text(old)
    (out / 'candidate.cu.txt').write_text(new)
    (out / Path(__file__).name).write_text(Path(__file__).read_text())
    (out / template.name).write_text(template.read_text())
    cache = (ROOT / 'build/CMakeCache.txt').read_text()
    compiler = re.search(r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    host = re.search(r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    command = [compiler, '-std=c++23', '--expt-relaxed-constexpr', '-O3',
               '-arch=sm_110a', '-ccbin', host, '-Xcompiler=-Wall,-Wextra',
               '-I' + str(ROOT / 'include'), str(out / 'comparison.cu'),
               '-Xlinker=--start-group']
    command += [str(ROOT / p) for p in libraries]
    command += ['-Xlinker=--end-group', '-lcublasLt', '-luring', '-licui18n',
                '-licuuc', '-licudata', '-ldl', '-lpthread', '-lrt',
                '-o', str(out / 'comparison')]
    (out / 'manifest.json').write_text(json.dumps({
        'baseline_ref': baseline_ref, 'binary_sha256': sha,
        'library_sha256': libraries, 'compile_command': command,
        'scope': 'host alias bounds and compiled capacity, not system peak or kernel numerics'}, indent=2) + '\n')
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    with (out / 'result.log').open('w') as log:
        result = subprocess.run([str(out / 'comparison')], stdout=log,
                                stderr=subprocess.STDOUT)
    (out / 'exit.json').write_text(json.dumps({'layout': result.returncode}) + '\n')
    output = (out / 'result.log').read_text()
    records = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
    (out / 'resource-views.json').write_text(json.dumps(records, indent=2) + '\n')
    print('\n'.join(line for line in output.splitlines() if not line.startswith('{')))

    result.check_returncode()


if __name__ == '__main__':
    main()
