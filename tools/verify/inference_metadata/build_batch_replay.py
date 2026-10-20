"""Build the same replay against an archived parent and the current candidate."""
import concurrent.futures
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/metadata-followup-20261020'
OUT = STAGE / 'batch-replay-build-v1'


def build(label, source, build_dir):
    commands = [
        ['cmake', '-S', str(source), '-B', str(build_dir),
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=g++-14',
         '-DCMAKE_CUDA_HOST_COMPILER=g++-14', '-DQ4T_CUDA_ARCHITECTURES=110a'],
        ['cmake', '--build', str(build_dir), '--target',
         'q4t_metadata_batch_replay', '--parallel', '8']]
    (OUT / (label + '-commands.json')).write_text(json.dumps(commands, indent=2) + '\n')
    for index, command in enumerate(commands):
        with (OUT / f'{label}-{index}.log').open('w') as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log,
                                    stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'{label} build step {index} exit {result.returncode}')
    print(label + ' built', flush=True)


def main():
    OUT.mkdir()
    parent = STAGE / 'batch-parent-source'
    parent.mkdir()
    archive = subprocess.check_output(['git', 'archive', '48185ed'], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(parent, filter='data')
    fixture = ROOT / 'tools/verify/inference_metadata/batch_replay.cpp'
    # Identical fixture copied into the parent; runtime source remains exact.
    (parent / 'tools/verify/inference_metadata/batch_replay.cpp').write_bytes(fixture.read_bytes())
    target = '''\nadd_executable(q4t_metadata_batch_replay EXCLUDE_FROM_ALL
  tools/verify/inference_metadata/batch_replay.cpp)
target_include_directories(q4t_metadata_batch_replay PRIVATE include)
target_link_libraries(q4t_metadata_batch_replay PRIVATE q4t_model)
target_compile_options(q4t_metadata_batch_replay PRIVATE -Wall -Wextra -Werror)
'''
    with (parent / 'CMakeLists.txt').open('a') as stream:
        stream.write(target)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(build, 'parent', parent, STAGE / 'batch-parent-build'),
                executor.submit(build, 'candidate', ROOT, STAGE / 'build')]
        for job in jobs:
            job.result()


if __name__ == '__main__':
    main()
