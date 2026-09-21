"""Compare source-extracted old carve with the new layout after full HTTP."""
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
    for name in ['src/model/decoder_layer.cu', 'include/q4t/model/decoder_layer.h']:
        assert (ROOT / name).read_bytes() == (run / Path(name).name).read_bytes()
    libraries = json.loads((run / 'libraries.sha256.json').read_text())
    for name, expected in libraries.items():
        assert digest(ROOT / name) == expected
    baseline_ref = subprocess.check_output(
        ['git', 'rev-parse', args.baseline_ref + '^{commit}'], text=True, cwd=ROOT).strip()
    old = subprocess.check_output(
        ['git', 'show', baseline_ref + ':src/model/decoder_layer.cu'],
        text=True, cwd=ROOT)
    new = (ROOT / 'src/model/decoder_layer.cu').read_text()
    helper = old[old.index('size_t AttnWs('):old.index('\n}  // namespace')]
    layout = new[new.index('struct DecoderWorkspaceLayout'):new.index('\n}  // namespace')]
    a = old.index('size_t DecoderLayerWorkspaceBytes(')
    budget = old[a:old.index('\nvoid DecoderLayer::Free()', a)]
    a = old.index('  // Carve the workspace into per-submodule regions.')
    carve = old[a:old.index('\n  Status s;', a)]
    # Retain the old expressions and sequence; replace pointer types with
    # byte offsets so the oracle needs no multi-GiB host/GPU allocation.
    carve = carve.replace('char* base = static_cast<char*>(workspace);', 'size_t base = 0;')
    carve = carve.replace('char* p =', 'size_t p =')
    carve = carve.replace('void* d_', 'size_t d_').replace('uint16_t* d_', 'size_t d_')
    carve = carve.replace('reinterpret_cast<uint16_t*>(p)', 'p')
    carve = carve.replace('return Status::Fail("DecoderLayerForward: workspace too small");',
                          'std::abort();')
    source = r'''
#include <array>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include "q4t/model/decoder_layer.h"
namespace candidate {
using namespace q4t::model;
constexpr size_t kGemmWs = 32u * 1024u * 1024u;
size_t AlignUp(size_t x) { return (x + 255u) & ~size_t(255u); }
// HELPER
// LAYOUT
}
namespace baseline {
using namespace q4t::model;
using candidate::AlignUp;
using candidate::AttnWs;
using candidate::kGemmWs;
// BUDGET
std::array<size_t, 14> Carve(const DecoderLayer& layer, int T) {
  const int hs = layer.hs, hc = layer.hc, hc_dim = layer.hc_dim;
  const size_t workspace_bytes = std::numeric_limits<size_t>::max();
// CARVE
  return {d_attn_ws, d_moe_ws, d_moe_gemm, d_hc_ws, d_ple_ws,
          d_mixed, d_block, d_normed, d_combined, p,
          attn_ws, moe_carve, ple_ws, total_ws};
}
}
int main() {
  using namespace q4t::model;
  int cases = 0;
  for (int t : {1, 3, 4, 5, 33, 257, 8192}) {
    for (int max_len : {8192, 208896, 262144}) {
      for (bool full : {false, true}) {
        for (bool ple : {false, true}) {
          DecoderLayer layer;
          layer.is_full_attention = full;
          layer.has_ple = ple;
          layer.full.max_len = max_len;
          layer.routed.E = 512;
          layer.routed.moe_is = 640;
          layer.mlp.shared_is = 640;
          const auto got = candidate::MakeDecoderWorkspaceLayout(
              t, full, ple, 4, 2560, 512, 640, 640, 10, &layer.full);
          const std::array<size_t, 14> actual = {
              got.attention, got.moe, got.moe_gemm, got.hc_gemm, got.ple,
              got.mixed, got.block, got.normed, got.combined, got.ple_trunk,
              got.attention_bytes, got.moe_bytes, got.ple_bytes, got.total_bytes};
          if (baseline::Carve(layer, t) != actual) return 1;
          for (int i = 0; i < 10; ++i) {
            if (actual[i] % 256 || actual[i] > got.total_bytes) return 2;
          }
          // Link the actual HTTP candidate library for the public API.
          for (bool known_full : {false, true}) {
            const auto* weights = known_full ? &layer.full : nullptr;
            const size_t before = baseline::DecoderLayerWorkspaceBytes(
                t, full, ple, 2560, 512, 640, 640, 10, weights);
            const size_t now = DecoderLayerWorkspaceBytes(
                t, full, ple, 2560, 512, 640, 640, 10, weights);
            if (before != now) return 3;
          }
          ++cases;
        }
      }
    }
  }
  std::printf("PASS: %d layouts, 14 fields each; %d linked budget comparisons; "
              "model shape hs=2560 hc=4\n", cases, cases * 2);
}
'''
    for tag, body in [('HELPER', helper), ('LAYOUT', layout), ('BUDGET', budget), ('CARVE', carve)]:
        source = source.replace('// ' + tag, body)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'comparison.cu').write_text(source)
    (out / 'baseline.cu.txt').write_text(old)
    (out / 'candidate.cu.txt').write_text(new)
    (out / Path(__file__).name).write_text(Path(__file__).read_text())
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
        'scope': 'host workspace offsets and capacities, not numerical kernels'}, indent=2) + '\n')
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    with (out / 'result.log').open('w') as log:
        result = subprocess.run([str(out / 'comparison')], stdout=log,
                                stderr=subprocess.STDOUT)
    (out / 'exit.json').write_text(json.dumps({'layout': result.returncode}) + '\n')
    print((out / 'result.log').read_text(), end='')
    result.check_returncode()


if __name__ == '__main__':
    main()
