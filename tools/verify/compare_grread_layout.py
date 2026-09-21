"""Check GRRead staging boundaries and compiled capacity after full HTTP."""
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
                 'include/q4t/model/hyperconnection.h']:
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
    new_layout = new[new.index('struct DecoderWorkspaceLayout'):new.index('\n}  // namespace')]
    source = r"""

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include "q4t/model/decoder_layer.h"
#include "q4t/model/model_head.h"
namespace baseline {
using namespace q4t::model;
constexpr size_t kGemmWs = 32u * 1024u * 1024u;
size_t AlignUp(size_t x) { return (x + 255u) & ~size_t(255u); }
// HELPER
// OLD_LAYOUT
}  // namespace baseline
namespace candidate {
using namespace q4t::model;
using baseline::AlignUp;
using baseline::AttnWs;
using baseline::kGemmWs;
// NEW_LAYOUT
}  // namespace candidate
void Require(bool ok, const char* what) {
  if (!ok) {
    std::fprintf(stderr, "FAILED: %s\n", what);
    std::exit(1);
  }
}
int main() {
  using namespace q4t::model;
  int cases = 0;
  for (int t : {1, 3, 4, 5, 33, 257, 8192}) {
    for (int max_len : {8192, 208896, 262144}) {
      FullAttentionWeights weights;
      weights.max_len = max_len;
      for (bool full : {false, true}) {
        for (bool ple : {false, true}) {
          for (int lowrank : {160, 320, 640}) {
            const auto old = baseline::MakeDecoderWorkspaceLayout(
                t, full, ple, 4, 2560, 512, 640, 640, 10, &weights);
            const auto now = candidate::MakeDecoderWorkspaceLayout(
                t, full, ple, 4, 2560, 512, 640, 640, 10, &weights, lowrank);
            const size_t hidden = size_t(t) * 2560 * 2, hyper = 4 * hidden;
            const size_t down_bytes = size_t(t) * lowrank * 2;
            const size_t staging = baseline::AlignUp(hyper) +
                                   baseline::AlignUp(down_bytes) +
                                   baseline::AlignUp(hyper);
            Require(now.normed == now.moe, "normed alias offset");
            Require(now.hc_down == now.normed + baseline::AlignUp(hyper),
                    "down starts after normed");
            Require(now.hc_up == now.hc_down + baseline::AlignUp(down_bytes),
                    "up starts after down");
            Require(now.hc_down_bytes == down_bytes && now.hc_up_bytes == hyper,
                    "scratch size follows actual lowrank");
            Require(now.moe_bytes >= staging,
                    "model MoE contains Read staging");
            Require(old.total_bytes == now.total_bytes,
                    "unchanged model capacity");
            Require(old.attention == now.attention && old.moe == now.moe &&
                        old.moe_gemm == now.moe_gemm &&
                        old.hc_gemm == now.hc_gemm && old.ple == now.ple &&
                        old.mixed == now.mixed && old.block == now.block &&
                        old.normed == now.normed &&
                        old.combined == now.combined &&
                        old.ple_trunk == now.ple_trunk,
                    "unchanged existing offsets");
            Require(old.attention_bytes == now.attention_bytes &&
                        old.moe_bytes == now.moe_bytes &&
                        old.ple_bytes == now.ple_bytes,
                    "unchanged submodule requirements");
            // Read views are excluded from this physical list; their ordered
            // aligned offsets above prove they are internally disjoint.
            const std::array<std::array<size_t, 2>, 9> regions = {
                {{now.attention, now.attention_bytes},
                 {now.moe, std::max(now.moe_bytes, staging)},
                 {now.moe_gemm, baseline::kGemmWs},
                 {now.hc_gemm, baseline::kGemmWs},
                 {now.ple, now.ple_bytes},
                 {now.mixed, hidden},
                 {now.block, hidden},
                 {now.combined, hyper},
                 {now.ple_trunk, ple ? hyper : 0}}};
            for (size_t i = 0; i < regions.size(); ++i) {
              const auto a = regions[i];
              Require(a[0] % 256 == 0 && a[0] <= now.total_bytes &&
                          a[1] <= now.total_bytes - a[0],
                      "alignment and bounds");
              for (size_t j = i + 1; j < regions.size(); ++j) {
                const auto b = regions[j];
                Require(!a[1] || !b[1] || a[0] + a[1] <= b[0] ||
                            b[0] + b[1] <= a[0],
                        "physical regions disjoint");
              }
            }
            for (bool known_full : {false, true}) {
              const auto* w = known_full ? &weights : nullptr;
              const auto expected = candidate::MakeDecoderWorkspaceLayout(
                  t, full, ple, 4, 2560, 512, 640, 640, 10, w, lowrank);
              Require(expected.total_bytes ==
                          DecoderLayerWorkspaceBytes(t, full, ple, 2560, 512,
                                                     640, 640, 10, w, lowrank),
                      "linked capacity");
            }
            ++cases;
          }
        }
      }
    }
  }
  FullAttentionWeights weights;
  weights.max_len = 208896;
  size_t before = ModelHeadWorkspaceBytes(8192, 2560), after = before;
  for (int kind = 0; kind < 3; ++kind) {
    const auto old = baseline::MakeDecoderWorkspaceLayout(
        8192, kind == 2, kind == 1, 4, 2560, 512, 640, 640, 10, &weights);
    const size_t now = DecoderLayerWorkspaceBytes(
        8192, kind == 2, kind == 1, 2560, 512, 640, 640, 10, &weights);
    before = std::max(before, old.total_bytes);
    after = std::max(after, now);
  }
  std::printf(
      "PASS: %d layouts; %d linked capacities; disjoint GRRead views within "
      "MoE arena\n",
      cases, cases * 2);
  std::printf("model workspace T=8192: %zu -> %zu, saved %zu bytes\n", before,
              after, before - after);
}
"""
    for tag, body in [('HELPER', helper), ('OLD_LAYOUT', old_layout), ('NEW_LAYOUT', new_layout)]:
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
        'scope': 'host alias bounds and compiled capacity, not system peak or kernel numerics'}, indent=2) + '\n')
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
