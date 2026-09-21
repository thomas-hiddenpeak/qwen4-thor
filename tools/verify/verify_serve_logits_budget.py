"""Post-HTTP budget delta and auto-capacity arithmetic, not resident memory."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from serve_logits_gate import ROOT, require_http

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--accepted-root', type=Path, required=True)
ap.add_argument('--output', type=Path, required=True)
a = ap.parse_args()
run, out = a.accepted_root.resolve(), a.output.resolve()
assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
sha, libraries = require_http(run)
out.mkdir(parents=True, exist_ok=False)
shutil.copy2(__file__, out)
parent = subprocess.check_output(['git', 'show', '00bddff:src/runtime/memory_budget.cpp'], cwd=ROOT, text=True)
parent = parent.replace('namespace runtime {', 'namespace parent_runtime {\nusing namespace q4t::runtime;')
(out / 'parent.cpp').write_text(parent)
shutil.copy2(ROOT / 'src/runtime/memory_budget.cpp', out / 'candidate.cpp')
(out / 'driver.cpp').write_text(r'''
#include "q4t/runtime/memory_budget.h"
#include <cstdio>
namespace q4t::parent_runtime {
q4t::runtime::MemoryBudget ComputeMemoryBudget(
    const q4t::runtime::BudgetModelParams&, const q4t::runtime::BudgetRequest&,
    size_t, size_t);
}
int main() {
  using namespace q4t::runtime;
  for (int mtp : {0, 1}) for (int prefill : {1024, 8192})
  for (int slots : {1, 2, 4, 8}) for (int length : {0, 8192, 65536, 208896}) {
    BudgetModelParams p;
    p.has_mtp = mtp != 0;
    BudgetRequest r;
    r.max_seq = slots; r.max_len = length; r.max_prefill = prefill;
    for (int version : {0, 1}) {
      const auto b = version == 0
          ? q4t::parent_runtime::ComputeMemoryBudget(p, r, 84000000000ULL, ReadMemTotal())
          : ComputeMemoryBudget(p, r, 84000000000ULL, ReadMemTotal());
      std::printf("%d %d %d %d %d %d %d %zu %zu %zu %zu\n",
                  version, mtp, prefill, slots, length, b.max_len, b.max_seq,
                  b.fixed, b.state_pool, b.per_request, b.budget);
    }
  }
}
''')
cmd = ['g++-14', '-std=c++23', '-O2', '-Wall', '-Wextra', '-I'+str(ROOT/'include'),
       str(out/'driver.cpp'), str(out/'parent.cpp'), str(out/'candidate.cpp'), '-o', str(out/'budget')]
with (out/'build.log').open('w') as log:
    subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)
assert 'warning' not in (out/'build.log').read_text().lower()
raw = subprocess.check_output([str(out/'budget')], text=True)
(out/'results.txt').write_text(raw)
rows = [list(map(int, line.split())) for line in raw.splitlines()]
assert len(rows) == 128
for old, new in zip(rows[::2], rows[1::2]):
    _, mtp, prefill, slots, length, nlen, nseq, fixed, state, transient, budget = new
    assert old[1:5] == new[1:5]
    assert old[7] - fixed == prefill * 248320 * 2
    # Independent sum of the documented state terms, including changed logits.
    token_bytes = (12 + mtp) * (2*2*256*2 + 4 + 128*2 + 128*2) + 12
    seq_bytes = 36*(48*128*128*4 + 10240*3*2) + 10240*9*2 + 248320*4 + 4
    assert state == nseq*(nlen*token_bytes + seq_bytes)
    assert transient == nlen*10240*2 + prefill*248320*2
    assert fixed + state + transient <= budget
    assert nlen >= old[5] and nseq >= old[6]
    if length == 0:
        assert nseq == slots
        max_len = min(262144, (budget-fixed-prefill*248320*2-slots*seq_bytes)
                      // (slots*token_bytes+10240*2))
        assert nlen == max_len//1024*1024
review = {'accepted': True, 'configurations': 64, 'binary_sha256': sha,
          'compile_command': cmd, 'library_sha256': libraries,
          'limits': 'Budget arithmetic only, weights fixed at serve default 84 GB. Existing workspace estimates are not certified; no device allocation or residency claim.'}
(out/'review.json').write_text(json.dumps(review, indent=2)+'\n')
print(json.dumps(review))
