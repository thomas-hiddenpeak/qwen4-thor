"""One bounded loader correctness stage. Run the whole driver under a GPU lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/megatron-reliability-20261020'
REFERENCE = ROOT / '.q4t-work/metadata-followup-20261020/batch-http-v1'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')):
        parser.error('output must be in build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    state = dict(status='PREPARING', started=time.time(), completed=[])

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2) + '\n')

    try:
        build = out / 'candidate-build'
        build.mkdir()
        binaries = {}
        for name in ('q4t', 'q4t_load_source_test', 'q4t_moe_replay',
                     'CMakeCache.txt'):
            shutil.copy2(STAGE / 'build' / name, build / name)
            binaries[name] = sha(build / name)
        sources = {}
        paths = [p for folder in ('src', 'include')
                 for p in (ROOT / folder).rglob('*') if p.is_file()]
        paths += [ROOT / 'CMakeLists.txt', Path(__file__),
                  ROOT / 'tools/verify/moe_replay/run.py',
                  ROOT / 'tools/verify/moe_replay/main.cpp',
                  ROOT / 'tools/verify/inference_metadata/load_source_test.cpp']
        for path in paths:
            name = str(path.relative_to(ROOT))
            target = out / 'source' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            sources[name] = sha(target)
        save('sources.json', sources)
        (out / 'source.patch').write_bytes(subprocess.check_output(
            ['git', 'diff', 'HEAD'], cwd=ROOT))
        reference_manifest = json.loads((REFERENCE / 'sealed-files.json').read_text())
        for mode in ('off', 'on'):
            reference = REFERENCE / ('quality-' + mode)
            for path in [reference / 'results.json',
                         *sorted((reference / 'inputs').rglob('*'))]:
                if path.is_file() and sha(path) != reference_manifest[
                        str(path.relative_to(REFERENCE))]:
                    raise ValueError('reference identity mismatch')
            target = out / ('reference-' + mode)
            target.mkdir()
            shutil.copy2(reference / 'results.json', target / 'results.json')
            shutil.copytree(reference / 'inputs', target / 'inputs')
        save('plan.json', dict(binaries=binaries, source_commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            model=MODEL, model_loads=3, full_model_loads=2,
            quality_requests=22, performance_requests=0, retries=0,
            default_binary_sha256=sha(ROOT / 'build/q4t')))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}

        def verify():
            for name, expected in binaries.items():
                if sha(build / name) != expected:
                    raise ValueError('binary snapshot changed')
            for name, expected in sources.items():
                if sha(ROOT / name) != expected or sha(out / 'source' / name) != expected:
                    raise ValueError('source changed: ' + name)

        def run(name, command):
            verify()
            state.update(status='RUNNING', current=name)
            save('run.json', state)
            save(name + '-command.json', command)
            with (out / (name + '.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=1800, check=True)
            state['completed'].append(name)
            save('run.json', state)

        run('upload', [str(build / 'q4t_load_source_test'),
                       str(out / 'upload-fixture'), '--gpu'])
        run('moe', ['python3', '-B', 'tools/verify/moe_replay/run.py',
                    '--suite', 'invariance', '--binary', str(build / 'q4t_moe_replay'),
                    '--model', MODEL, '--fixture', 'tools/verify/moe_replay/fixture.json',
                    '--output', str(out / 'moe')])
        baseline = STAGE / 'numerical-v1/invariance'
        original_manifest = json.loads((STAGE / 'numerical-v1/files.json').read_text())
        compared = 0
        for source in sorted(baseline.iterdir()):
            if source.suffix not in ('.bf16', '.f32', '.i32'):
                continue
            if sha(source) != original_manifest['invariance/' + source.name]:
                raise ValueError('MoE reference changed')
            if source.read_bytes() != (out / 'moe/invariance' / source.name).read_bytes():
                raise ValueError('MoE parent/candidate difference: ' + source.name)
            compared += 1
        save('moe-parent-comparison.json', dict(status='PASS', files=compared))
        for mode in ('off', 'on'):
            reference = out / ('reference-' + mode)
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', 'quality', '--output', str(out / ('quality-' + mode)),
                       '--binary', str(build / 'q4t'), '--model-dir', MODEL,
                       '--fixtures', str(reference / 'inputs'),
                       '--reference', str(reference / 'results.json'),
                       '--port', '8740', '--startup-timeout', '300',
                       '--allow-unqualified-binary']
            if mode == 'on':
                command.append('--mtp')
            run('quality-' + mode, command)
            result_dir = out / ('quality-' + mode)
            rows = json.loads((result_dir / 'results.json').read_text())
            shutdown = json.loads((result_dir / 'exit.json').read_text())
            if len(rows) != 11 or not all(row['success'] == 1 and row['exact_match']
                                        and row['length_match'] for row in rows):
                raise ValueError('quality did not pass')
            if shutdown['server'] != 0 or not shutdown['http_output_checks_passed']:
                raise ValueError('quality shutdown failed')
        verify()
        state.update(status='PASS_RUNTIME_CONTRACTS', performance_acceptance=False,
                     deployed=False)
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save('run.json', state)
        save('files.json', {str(p.relative_to(out)): sha(p)
                            for p in sorted(out.rglob('*')) if p.is_file()
                            and p != out / 'files.json'})


if __name__ == '__main__':
    main()
