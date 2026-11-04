// Reuse architecture-explicit reference enumeration; production ranges are
// only used for disk IO. Reference continuation never restores a snapshot.
#define Q4T_SESSION_REFERENCE_ONLY
#include "../session_cache/state_contract.cpp"
#undef Q4T_SESSION_REFERENCE_ONLY
#include "scoring_contract.h"

namespace {
void Prefix(Fixture& f, int slot, const std::vector<int32_t>& ids, int stop,
            bool reference) {
  f.Begin(slot);
  while (f.seq[slot].position < stop) {
    const int base = f.seq[slot].position;
    const int n = std::min(4096, stop - base);
    Check(model::ModelPrefillTextChunk(f.main(), &f.seq[slot], ids.data(),
        ids.size(), n, nullptr, nullptr, f.trunk.p,
        model::LogitsRows::kLastRow));
    std::vector<int> positions(n);
    std::iota(positions.begin(), positions.end(), base);
    const bool end = base + n == stop;
    // Reference computes the ordinary final draft head. Candidate captures g
    // without head, and must not consume d0 until remaining prefill completes.
    Check(mtp::MtpDraftExtend(f.draft, ids.data() + base + 1, f.trunk.p,
        positions.data(), n, &f.d0[slot], f.G(slot), nullptr, slot,
        end && reference, end && !reference));
    Cuda(cudaStreamSynchronize(nullptr));
    if (end) Cuda(cudaMemcpy(f.H(slot), f.trunk.p + size_t(n-1)*10240,
                             10240*2, cudaMemcpyDeviceToDevice));
  }
}
// Reference replaces the shifted pending row through the primitive forward,
// using independently computed target trunk and no cache/restore/extend helper.
void ReferencePending(Fixture& f, int slot, int position, int32_t token) {
  Buffer id(2), pos(2), seq(2), sample(f.draft.cfg.hs), multi(10240);
  Cuda(cudaMemcpy(id.p, &token, 4, cudaMemcpyHostToDevice));
  Cuda(cudaMemcpy(pos.p, &position, 4, cudaMemcpyHostToDevice));
  Cuda(cudaMemcpy(seq.p, &slot, 4, cudaMemcpyHostToDevice));
  Check(mtp::MtpForward(f.draft, reinterpret_cast<int32_t*>(id.p),
      reinterpret_cast<int*>(pos.p), f.H(slot), sample.p, multi.p, nullptr, 1,
      nullptr, reinterpret_cast<int*>(seq.p), false));
  Cuda(cudaStreamSynchronize(nullptr));
}

void ObserveShape(const Image& a, const Image& b, int prefix) {
  size_t total = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    size_t differences = 0, first = a[i].bytes.size();
    for (size_t j = 0; j < a[i].bytes.size(); ++j)
      if (a[i].bytes[j] != b[i].bytes[j]) {
        ++differences;
        first = std::min(first, j);
      }
    if (differences)
      std::printf("SHAPE_OBSERVATION prefix=%d field=%zu differing_bytes=%zu "
                  "first_byte=%zu\n", prefix, i, differences, first);
    total += differences;
  }
  std::printf("SHAPE_OBSERVATION prefix=%d total_differing_bytes=%zu\n",
              prefix, total);
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Require(argc == 3 || argc == 4, "model cache-dir [subset]");
    Fixture f(argv[1], argc == 4 ? argv[3] : "");
    ScoringContract(f);
    std::filesystem::create_directory(argv[2]);
    chmod(argv[2], 0700);
    server::SessionCache cache;
    Check(cache.Open(argv[2], 1024, size_t{8} << 30));
    const auto copy = [](void* host, void* state, size_t n, bool restore) {
      const auto e = restore ? cudaMemcpy(state, host, n, cudaMemcpyHostToDevice)
                            : cudaMemcpy(host, state, n, cudaMemcpyDeviceToHost);
      return e == cudaSuccess ? Status() : Status::Fail(cudaGetErrorString(e));
    };
    const auto active = [] { return false; };
    for (const int p : {1024, 8193}) {
      std::vector<int32_t> ids(p + 17);
      for (size_t i = 0; i < ids.size(); ++i) ids[i] = 100 + i % 101;
      Prefix(f, 0, ids, p, false);
      Prefix(f, 1, ids, p, true);
      Equal(f.Capture(0, p), f.Capture(1, p));
      const std::string key = "scope:" + std::to_string(p);
      Require(cache.AcquireNew(key), "source writer");
      std::vector<server::SessionRange> ranges;
      Check(server::SessionStateRanges(f.main(), f.draft, 0, p,
                                         f.G(0), f.H(0), &ranges));
      Check(cache.Save(key, {ids.data(), size_t(p+1)}, ranges, copy, active, -1));
      cache.Release(key, true);
      // Source and destination are both overwritten before a foreign session
      // restores. Two forks reuse the immutable snapshot, including d0=-1.
      f.Begin(0); f.Prefill(0, std::vector<int32_t>(257, 444));
      for (const bool changed : {false, true}) {
        auto branch = ids;
        if (changed) branch[p] = 248000;
        std::printf("CASE prefix=%d changed_pending=%d\n", p, changed);
        std::fflush(stdout);
        Prefix(f, 1, branch, p, true);
        f.Prefill(1, branch);
        const auto fresh_shape = f.Capture(1, branch.size());
        if (changed) {
          // Preserve the originally committed draft prefix and apply the
          // branch token with the same one-row shape as the serving contract.
          // Full-chunk fresh prefill remains a separate numerical observation.
          Prefix(f, 1, ids, p, true);
          ReferencePending(f, 1, p - 1, branch[p]);
          f.Prefill(1, branch);
        }
        const auto expected_logits = Read(f.logits.p, 248320u*2);
        const auto expected = f.Capture(1, branch.size());
        if (changed) ObserveShape(fresh_shape, expected, p);
        f.Begin(2); f.Prefill(2, std::vector<int32_t>(259, 888));
        f.Begin(2);
        std::string selected;
        Require(cache.MatchShared("scope", branch, &selected) == p,
                "foreign session exact shared prefix");
        Require(cache.Match(key, branch) == p,
                "private lookup preserves identical saved prefix");
        Check(server::SessionStateRanges(f.main(), f.draft, 2, p,
                                           f.G(2), f.H(2), &ranges));
        int32_t pending = -1;
        Check(cache.Restore(selected, ranges, copy, active, &f.d0[2], &pending));
        cache.Release(selected, true);
        Require(f.d0[2] == -1, "prefill sentinel");
        if (pending != branch[p]) {
          const int position = p - 1;
          Check(mtp::MtpDraftExtend(f.draft, &branch[p], f.H(2), &position, 1,
                                     nullptr, nullptr, nullptr, 2, false));
        }
        f.seq[2].history.assign(branch.begin(), branch.begin()+p);
        f.seq[2].position = p;
        f.main().rope_delta[2] = 0;
        f.Prefill(2, branch);
        Require(Read(f.logits.p, 248320u*2) == expected_logits,
                "independent continuation logits");
        Equal(f.Capture(2, branch.size()), expected);
        Require(f.d0[2] == f.d0[1] && f.bonus[2] == f.bonus[1],
                "final draft seed established");
      }
    }
    std::printf("PASS prefix_state=1 boundaries=1024,8193 forks=4 "
                "independent_logits=1 full_state=1 head=%s\n",
                argc == 4 ? "subset" : "full");
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "FAIL prefix state: %s\n", e.what());
    return 1;
  }
}
