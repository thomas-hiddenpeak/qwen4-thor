// Targeted verification of the long-context one-pass decode top-k
// (SelectDecodeTopk), the path used by MTP draft/verify decode at context
// lengths beyond 8192 tokens (e.g. the 45K tier: 11264 groups).
//
// Cross-checks the GPU hierarchical selection against a CPU reference that
// computes the exact global top-512 blocks per token, then expands to token
// positions + the current-group tail. Compares the *set* of visible selected
// positions (order-independent), which is what the sparse attention consumes.
#include "q4t/model/decode_topk.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <random>
#include <map>
#include <set>
#include <vector>

namespace {

constexpr int kTopk = 512;
constexpr int kCompress = 4;
constexpr int kMaxTopk = 2052;  // idx_budget + compress - 1

// CPU reference: exact global top-kTopk blocks for one token row, then
// expand to visible token positions + current-group tail.
std::set<int> ReferenceSelected(const std::vector<float>& row, int groups,
                                int pos) {
  std::vector<std::pair<float, int>> v;
  v.reserve(groups);
  for (int g = 0; g < groups; ++g) v.emplace_back(row[g], g);
  // Descending by value; ties broken by ascending index (deterministic).
  std::sort(v.begin(), v.end(), [](const auto& a, const auto& b) {
    if (a.first != b.first) return a.first > b.first;
    return a.second < b.second;
  });
  std::set<int> sel;
  for (int i = 0; i < kTopk && i < groups; ++i) {
    const int base = v[i].second * kCompress;
    for (int j = 0; j < kCompress; ++j) {
      const int p = base + j;
      if (p <= pos) sel.insert(p);
    }
  }
  // Current-group visible tail (matches kernel: (pos+1)%compress tokens).
  const int tail = (pos + 1) % kCompress;
  for (int j = 0; j < tail; ++j) sel.insert(pos + 1 - tail + j);
  return sel;
}

bool CheckGroups(int groups, int tokens, bool ties) {
  const int max_pos = groups * kCompress - 1;
  std::mt19937 rng(0x9e3779b9u ^ static_cast<unsigned>(groups));
  std::vector<int> positions(tokens);
  for (int t = 0; t < tokens; ++t)
    positions[t] = max_pos - tokens + 1 + t;  // last `tokens` positions

  std::vector<float> h_logits(static_cast<size_t>(tokens) * groups);
  if (ties) {
    // Sparse ties: only 8 distinct score levels -> heavy ties at the boundary.
    for (auto& x : h_logits)
      x = static_cast<float>(rng() % 8) - 4.f;
  } else {
    std::uniform_real_distribution<float> dist(-1000.f, 1000.f);
    for (auto& x : h_logits) x = dist(rng);
  }

  float* d_logits = nullptr;
  int* d_pos = nullptr;
  float* d_v0 = nullptr;
  int* d_i0 = nullptr;
  float* d_v1 = nullptr;
  int* d_i1 = nullptr;
  int* d_topk = nullptr;
  int* d_topk_len = nullptr;
  const size_t cand = static_cast<size_t>(tokens) * 16384;
  Q4T_CHECK(cudaMalloc(&d_logits, h_logits.size() * sizeof(float)) ==
            cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_pos, tokens * sizeof(int)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_v0, cand * sizeof(float)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_i0, cand * sizeof(int)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_v1, cand * sizeof(float)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_i1, cand * sizeof(int)) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_topk,
                       static_cast<size_t>(tokens) * kMaxTopk * sizeof(int)) ==
            cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_topk_len, tokens * sizeof(int)) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(d_logits, h_logits.data(),
                       h_logits.size() * sizeof(float),
                       cudaMemcpyHostToDevice) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(d_pos, positions.data(), tokens * sizeof(int),
                       cudaMemcpyHostToDevice) == cudaSuccess);

  const auto status = q4t::model::SelectDecodeTopk(
      d_logits, groups, tokens, d_pos, kCompress, kMaxTopk, d_v0, d_i0, d_v1,
      d_i1, d_topk, d_topk_len, nullptr);
  Q4T_CHECK(cudaGetLastError() == cudaSuccess);
  if (!status.ok()) {
    std::printf("  groups=%d: SelectDecodeTopk failed: %s\n", groups,
                status.message().c_str());
    return false;
  }

  std::vector<int> h_topk(static_cast<size_t>(tokens) * kMaxTopk);
  std::vector<int> h_len(tokens);
  Q4T_CHECK(cudaMemcpy(h_topk.data(), d_topk,
                       h_topk.size() * sizeof(int), cudaMemcpyDeviceToHost) ==
            cudaSuccess);
  Q4T_CHECK(cudaMemcpy(h_len.data(), d_topk_len, tokens * sizeof(int),
                       cudaMemcpyDeviceToHost) == cudaSuccess);

  bool ok = true;
  for (int t = 0; t < tokens && ok; ++t) {
    std::set<int> actual;
    for (int i = 0; i < h_len[t]; ++i) actual.insert(h_topk[t * kMaxTopk + i]);
    const std::set<int> expected =
        ReferenceSelected(std::vector<float>(h_logits.begin() +
                                                 static_cast<size_t>(t) *
                                             groups,
                                             h_logits.begin() +
                                                 static_cast<size_t>(t + 1) *
                                             groups),
                          groups, positions[t]);
    if (actual != expected) {
      std::set<int> missing, extra;
      std::set_difference(expected.begin(), expected.end(), actual.begin(),
                          actual.end(), std::inserter(missing, missing.begin()));
      std::set_difference(actual.begin(), actual.end(), expected.begin(),
                          expected.end(), std::inserter(extra, extra.begin()));
      if (!ties) {
        std::printf("  groups=%d t=%d pos=%d: |missing|=%zu |extra|=%zu "
                    "(len=%d)\n",
                    groups, t, positions[t], missing.size(), extra.size(),
                    h_len[t]);
        ok = false;
      } else {
        // Tie regime: the selection *count* must match (512 groups); the
        // specific tied groups may differ (tie-breaking). Verify both sides
        // select exactly kTopk distinct groups and that every differing
        // position shares the tie-boundary score.
        const auto row = [&](int p) {
          return h_logits[static_cast<size_t>(t) * groups + p / kCompress];
        };
        std::set<int> a_groups, e_groups;
        for (int p : actual) a_groups.insert(p / kCompress);
        for (int p : expected) e_groups.insert(p / kCompress);
        // The correct invariant in the tie regime: GPU and reference select
        // the same number of groups at each score level (tie-breaking may
        // pick different tied groups, but the per-score counts must match).
        auto hist = [&](const std::set<int>& g) {
          std::map<float, int> h;
          for (int b : g) h[row(b * kCompress)]++;
          return h;
        };
        const auto ha = hist(a_groups), he = hist(e_groups);
        bool same = (ha == he);
        std::printf("  groups=%d ties t=%d: a_groups=%zu e_groups=%zu "
                    "|missing|=%zu |extra|=%zu hist_match=%d\n",
                    groups, t, a_groups.size(), e_groups.size(),
                    missing.size(), extra.size(), same);
        if (!same) {
          for (auto& kv : he)
            std::printf("    ref score %.1f: %d  gpu: %d\n", kv.first,
                        kv.second, (ha.count(kv.first) ? ha.at(kv.first) : 0));
          for (auto& kv : ha)
            if (!he.count(kv.first))
              std::printf("    gpu-only score %.1f: %d\n", kv.first,
                          kv.second);
          ok = false;
        } else {
          std::printf("  groups=%d ties t=%d: per-score group counts match "
                      "(tie-breaking only)\n",
                      groups, t);
        }
      }
    }
  }
  cudaFree(d_logits);
  cudaFree(d_pos);
  cudaFree(d_v0);
  cudaFree(d_i0);
  cudaFree(d_v1);
  cudaFree(d_i1);
  cudaFree(d_topk);
  cudaFree(d_topk_len);
  if (ok)
    std::printf("  groups=%d tokens=%d ties=%d: exact top-512 selection "
                "matches reference\n",
                groups, tokens, ties);
  return ok;
}

}  // namespace

Q4T_TEST(decode_topk_exact) {
  int devices = 0;
  Q4T_CHECK(cudaGetDeviceCount(&devices) == cudaSuccess && devices > 0);
  // 45K tier group count (45056/4) and neighbours; 200K tier; boundary.
  for (int groups : {2049, 4096, 11264, 20480, 51200}) {
    Q4T_CHECK(CheckGroups(groups, 1, false));
    Q4T_CHECK(CheckGroups(groups, 4, false));
  }
  // Tie stress at the 45K group count (draft/verify shape T=4).
  Q4T_CHECK(CheckGroups(11264, 4, true));
  return true;
}
