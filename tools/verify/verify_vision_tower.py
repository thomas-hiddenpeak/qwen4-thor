"""Post-HTTP full vision-tower reference, retaining existing tolerances."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from verify_vision_attention import ROOT, require_http, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    binary_sha, libraries = require_http(args.accepted_root.resolve())
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    shutil.copy2(ROOT / 'tools/verify/verify_vision_attention.py', out)
    shutil.copy2(ROOT / 'tools/vision_reference.py', out)
    shutil.copy2(ROOT / 'tools/vision_ref.json', out / 'small-reference.json')
    # This imports and executes the existing CPU reference only after HTTP.
    sys.path.insert(0, str(ROOT / 'tools'))
    import vision_reference as reference
    weights = reference.load_vision_weights(reference.make_loader())
    data = {}
    for name, shape in [('image', (1, 16, 16, 1234)),
                        ('video', (2, 8, 8, 777))]:
        t, h, w, seed = shape
        pixels = reference.make_pixels(t, h, w, seed)
        result = reference.vision_forward(weights, pixels, [[t, h, w]])
        data[name] = {'grid_thw':[[t,h,w]], 'pixel_values':pixels.tolist(),
                      'output':result.tolist(), 'output_shape':list(result.shape)}
        print('CPU reference', name, shape, flush=True)
    (out / 'large-reference.json').write_text(json.dumps(data))
    del weights, data
    # Reuse the existing full-tower test and its 0.05 criteria unchanged.
    # Only parameterize artifacts and add a direct, non-skipping entry point.
    original = ROOT / 'tests/vision_forward_test.cpp'
    source = original.read_text()
    source = source.replace('bool RunCase(const std::string& case_name,',
                            'std::string output_prefix;\n\nbool RunCase(const std::string& case_name,')
    begin = source.index('  if (case_name == "video") {')
    end = source.index('  std::printf("  [%s] output[0,:4]', begin)
    source = source[:begin] + '''  const std::string raw_path = output_prefix + "-" + case_name + ".bf16";
  FILE* raw = std::fopen(raw_path.c_str(), "wb");
  Q4T_CHECK(raw != nullptr);
  const size_t written = std::fwrite(out_bf16.data(), sizeof(uint16_t), out_n, raw);
  const int close_result = std::fclose(raw);
  Q4T_CHECK(written == out_n && close_result == 0);
''' + source[end:]
    source += '''
int main(int argc, char** argv) {
  if (argc != 3 || !CudaAvailable() || !FileExists(kIndex) ||
      !FileExists(argv[1])) return 2;
  kRefJson = argv[1];
  output_prefix = argv[2];
  return q4t_test_fn_vision_forward() ? 0 : 1;
}
'''
    (out / 'comparison.cpp').write_text(source)
    shutil.copy2(original, out / 'original-test.cpp')
    cache = (ROOT / 'build/CMakeCache.txt').read_text()
    compiler = re.search(r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    host = re.search(r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)', cache, re.M)[1]
    command = [compiler, '-std=c++23', '--expt-relaxed-constexpr', '-O3',
        '-arch=sm_110a', '-ccbin', host, '-Xcompiler=-Wall,-Wextra',
        '-I' + str(ROOT / 'include'), str(out / 'comparison.cpp'),
        '-Xlinker=--start-group']
    command += [str(ROOT / f'build/libq4t_{name}.a') for name in
                ['model', 'ple', 'quant', 'text', 'io', 'runtime']]
    command += ['-Xlinker=--end-group', '-lcublasLt', '-luring', '-licui18n',
                '-licuuc', '-licudata', '-ldl', '-lpthread', '-lrt',
                '-o', str(out / 'comparison')]
    (out / 'manifest.json').write_text(json.dumps({'binary_sha256':binary_sha,
        'libraries':libraries,'command':command,'original_test_sha256':sha(original),
        'reference_script_sha256':sha(ROOT / 'tools/vision_reference.py')}, indent=2))
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    records = []
    for fixture, repeats in [('small', 1), ('large', 3)]:
        for repeat in range(repeats):
            prefix = out / f'{fixture}-{repeat}'
            with Path(str(prefix) + '.log').open('w') as log:
                result = subprocess.run([str(out / 'comparison'),
                    str(out / (fixture + '-reference.json')), str(prefix)],
                    stdout=log, stderr=subprocess.STDOUT)
            records.append({'fixture':fixture,'repeat':repeat,'exit':result.returncode})
            (out / 'exits.json').write_text(json.dumps(records, indent=2))
            result.check_returncode()
            assert '(skipped' not in Path(str(prefix) + '.log').read_text()
    for case in ['image', 'video']:
        first = (out / f'large-0-{case}.bf16').read_bytes()
        assert first
        for repeat in [1, 2]:
            assert first == (out / f'large-{repeat}-{case}.bf16').read_bytes()
    (out / 'review.json').write_text(json.dumps({'passed':True,
        'limits':'Existing NumPy FP32 full-tower reference and unchanged test criteria; synthetic pixels, not a new universal visual quality claim.'}))


if __name__ == '__main__':
    main()
