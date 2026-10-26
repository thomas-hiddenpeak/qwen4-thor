// CPU-only capacity review using the production planner and observed sizing.
// Arguments: main weights, main workspace, MTP workspace, MTP weights, MemTotal.
#include <cstdio>
#include <cstdlib>

#include "q4t/mtp/subset_head.h"
#include "q4t/runtime/memory_budget.h"

int main(int argc, char** argv) {
  if (argc != 6) return 2;
  const size_t weights = std::strtoull(argv[1], nullptr, 10);
  const size_t main_workspace = std::strtoull(argv[2], nullptr, 10);
  const size_t mtp_workspace = std::strtoull(argv[3], nullptr, 10);
  const size_t mtp_weights = std::strtoull(argv[4], nullptr, 10);
  const size_t mem_total = std::strtoull(argv[5], nullptr, 10);
  const int lengths[] = {16384, 32768, 65536, 98304, 131072, 208896};
  const char* modes[] = {"plain", "full", "subset"};
  for (int mode = 0; mode < 3; ++mode) {
    for (int len : lengths) {
      q4t::runtime::BudgetModelParams params;
      params.has_mtp = mode != 0;
      q4t::runtime::BudgetRequest request;
      request.max_len = len;
      request.max_seq = 4;
      request.max_prefill = 8192;
      request.main_workspace_bytes = main_workspace;
      request.mtp_workspace_bytes = mtp_workspace;
      request.mtp_weights_bytes = mtp_weights;
      // Largest tensor staging from the same captured startup reports.
      request.mtp_load_host_bytes = 3355443200u;
      if (mode != 0) request.extra_fixed_bytes = 4 * (size_t{2} << 20) +
                                                (size_t{256} << 10);
      if (mode == 2) {
        request.extra_fixed_bytes += q4t::mtp::kSubsetReadCacheBudget;
        request.mtp_weights_bytes += q4t::mtp::kSubsetWeightBytes +
                                     q4t::mtp::kSubsetIdBytes;
      }
      const auto actual = q4t::runtime::ComputeMemoryBudget(
          params, request, weights, mem_total);
      // Analytical requested-allocation estimate, without capping to RAM.
      // This does not allocate anything or claim that capacity is runnable.
      const auto requested = q4t::runtime::ComputeMemoryBudget(
          params, request, weights, size_t{1} << 50);
      if (!requested.feasible || requested.capped) return 3;
      if (mode != 0 && len == 16384 &&
          (actual.max_seq != 4 || actual.estimated_total !=
              (mode == 1 ? 107162592180u : 111793365940u))) return 4;
      std::printf(
          "{\"mode\":\"%s\",\"requested_len\":%d,\"requested_slots\":4,"
          "\"max_prefill\":8192,\"mem_total\":%zu,\"budget\":%zu,"
          "\"effective_len\":%d,\"effective_slots\":%d,"
          "\"effective_peak\":%zu,\"requested_peak\":%zu,"
          "\"requested_state\":%zu,\"requested_transient\":%zu,"
          "\"requested_fixed\":%zu,\"reason\":\"%s\"}\n",
          modes[mode], len, mem_total, actual.budget, actual.max_len,
          actual.max_seq, actual.estimated_total, requested.estimated_total,
          requested.state_pool, requested.per_request, requested.fixed,
          actual.reason.c_str());
    }
  }
}
