#include "q4t/runtime/memory_budget.h"
#include "q4t/runtime/residency_config.h"
#include "q4t/test.h"

#include <filesystem>
#include <fstream>
#include <limits>

namespace {
using q4t::runtime::BudgetModelParams;
using q4t::runtime::BudgetRequest;
using q4t::runtime::ComputeMemoryBudget;
using q4t::runtime::ResidencyConfig;

constexpr size_t kRoom = 128000000000;
constexpr size_t kWeights = 1000000000;

BudgetRequest Request(int len = 262144, int seq = 1) {
  BudgetRequest request;
  request.mem_fraction = 1.0;
  request.max_len = len;
  request.max_seq = seq;
  request.max_prefill = 8192;
  return request;
}
}  // namespace

Q4T_TEST(budget_infeasible_capacity_never_becomes_a_minimum) {
  for (int length : {0, 1024, 262144}) {
    const auto budget = ComputeMemoryBudget({}, Request(length), kWeights, 1);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.max_len == 0 && budget.max_seq == 0);
    Q4T_CHECK(budget.requested_max_len == length);
    Q4T_CHECK(budget.reason == "no_supported_capacity_fits_estimate");
  }
  return true;
}

Q4T_TEST(budget_exact_fit_and_one_byte_short) {
  const auto request = Request();
  const auto roomy = ComputeMemoryBudget({}, request, kWeights, kRoom);
  Q4T_CHECK(roomy.feasible && !roomy.capped);
  const auto exact =
      ComputeMemoryBudget({}, request, kWeights, roomy.estimated_total);
  Q4T_CHECK(exact.feasible && !exact.capped && exact.max_len == 262144);
  const auto short_one =
      ComputeMemoryBudget({}, request, kWeights, roomy.estimated_total - 1);
  Q4T_CHECK(short_one.feasible && short_one.capped);
  Q4T_CHECK(short_one.max_len < request.max_len);
  Q4T_CHECK(short_one.estimated_total <= short_one.budget);
  return true;
}

Q4T_TEST(budget_sequence_cap_keeps_requested_and_effective_distinct) {
  const auto request = Request(8192, 8);
  const auto roomy = ComputeMemoryBudget({}, request, kWeights, kRoom);
  Q4T_CHECK(roomy.feasible && roomy.max_seq == 8);
  const size_t three_slots =
      roomy.fixed + roomy.per_request + (roomy.state_pool / 8) * 3;
  const auto budget = ComputeMemoryBudget({}, request, kWeights, three_slots);
  Q4T_CHECK(budget.feasible && budget.capped);
  Q4T_CHECK(budget.requested_max_seq == 8 && budget.max_seq == 3);
  Q4T_CHECK(budget.max_len == request.max_len);
  Q4T_CHECK(budget.report.find("requested max_len=8192 max_seq=8") !=
            std::string::npos);
  return true;
}

Q4T_TEST(budget_auto_search_finds_largest_supported_capacity) {
  for (bool mtp : {false, true}) {
    BudgetModelParams params;
    params.has_mtp = mtp;
    const auto pinned =
        ComputeMemoryBudget(params, Request(8192, 2), kWeights, kRoom);
    const auto automatic = ComputeMemoryBudget(params, Request(0, 2), kWeights,
                                               pinned.estimated_total);
    Q4T_CHECK(automatic.feasible && automatic.max_len == 8192);
    Q4T_CHECK(automatic.max_seq == 2);
    Q4T_CHECK(automatic.estimated_total <= automatic.budget);
    const auto larger =
        ComputeMemoryBudget(params, Request(9216, 2), kWeights, kRoom);
    Q4T_CHECK(larger.estimated_total > automatic.budget);
  }
  return true;
}

Q4T_TEST(budget_context_ceiling_is_applied_to_explicit_requests) {
  const auto budget = ComputeMemoryBudget({}, Request(262145), kWeights, kRoom);
  Q4T_CHECK(budget.feasible && budget.capped);
  Q4T_CHECK(budget.max_len == 262144 && budget.requested_max_len == 262145);
  return true;
}

Q4T_TEST(budget_mtp_off_has_no_draft_reservation) {
  auto request = Request();
  request.mtp_workspace_bytes = 5000000000;
  BudgetModelParams params;
  params.has_mtp = false;
  const auto plain = ComputeMemoryBudget(params, request, kWeights, kRoom);
  params.has_mtp = true;
  const auto draft = ComputeMemoryBudget(params, request, kWeights, kRoom);
  Q4T_CHECK(plain.feasible && draft.feasible);
  Q4T_CHECK(plain.mtp_workspace == 0 && plain.per_request == 0);
  Q4T_CHECK(draft.mtp_workspace == request.mtp_workspace_bytes);
  Q4T_CHECK(draft.fixed - plain.fixed == request.mtp_workspace_bytes);
  // Full prompt trunk [262144,10240] + chunk logits [8192,248320], BF16.
  Q4T_CHECK(draft.per_request == 9437184000ULL);
  Q4T_CHECK(draft.state_pool > plain.state_pool);
  return true;
}

Q4T_TEST(budget_ple_counts_all_ngram_heads_and_registered_pool) {
  BudgetModelParams params;
  Q4T_CHECK(params.ple_heads == 16 && params.ple_embed_dim() == 2560);
  const auto with_ple = ComputeMemoryBudget(params, Request(), kWeights, kRoom);
  params.has_ple = false;
  const auto without = ComputeMemoryBudget(params, Request(), kWeights, kRoom);
  // Actual allocations: two FP8 buffers, pinned int64 row IDs, 32 MiB
  // registered page pool and the documented io_uring ring estimate.
  constexpr size_t kRows = 8192 * 16;
  constexpr size_t kExpected =
      kRows * (2 * 160 + 8) + 32 * 1024 * 1024 + 256 * (64 + 16 + 4) + 4096;
  Q4T_CHECK(with_ple.ple_working == kExpected && without.ple_working == 0);
  Q4T_CHECK(with_ple.forward_buffers - without.forward_buffers ==
            8192 * 2560 * 2);
  return true;
}

Q4T_TEST(budget_runtime_workspace_sizes_override_reference_estimates) {
  auto request = Request();
  request.main_workspace_bytes = 123456789;
  const auto first = ComputeMemoryBudget({}, request, kWeights, kRoom);
  request.main_workspace_bytes += 4096;
  const auto second = ComputeMemoryBudget({}, request, kWeights, kRoom);
  Q4T_CHECK(first.main_workspace == 123456789);
  Q4T_CHECK(second.fixed - first.fixed == 4096);
  request.max_prefill = 4096;
  const auto half = ComputeMemoryBudget({}, request, kWeights, kRoom);
  Q4T_CHECK(half.main_workspace == second.main_workspace);
  Q4T_CHECK(half.forward_buffers * 2 - 4 == second.forward_buffers);
  Q4T_CHECK(half.max_prefill == 4096);
  return true;
}

Q4T_TEST(budget_kv_page_rounding_matches_allocated_state) {
  const auto sixteen = ComputeMemoryBudget({}, Request(16), kWeights, kRoom);
  const auto seventeen = ComputeMemoryBudget({}, Request(17), kWeights, kRoom);
  Q4T_CHECK(sixteen.feasible && seventeen.feasible);
  // 12 full layers: one new 16-token KV page, one page-table/indexer row;
  // one extra position in the main three-axis int32 RoPE table.
  Q4T_CHECK(seventeen.state_pool - sixteen.state_pool ==
            12 * (16 * 2 * 2 * 256 * 2 + 4 + 2 * 128 * 2) + 3 * 4);
  return true;
}

Q4T_TEST(budget_invalid_or_overflowed_estimate_fails_closed) {
  const auto huge = ComputeMemoryBudget(
      {}, Request(), std::numeric_limits<size_t>::max() - 1, kRoom);
  Q4T_CHECK(!huge.feasible && huge.max_len == 0 && huge.max_seq == 0);
  for (double fraction : {-1.0, 1.1, std::numeric_limits<double>::infinity(),
                          std::numeric_limits<double>::quiet_NaN()}) {
    auto request = Request();
    request.mem_fraction = fraction;
    const auto bad = ComputeMemoryBudget({}, request, kWeights, kRoom);
    Q4T_CHECK(!bad.feasible && bad.reason == "invalid_budget_request");
  }
  return true;
}

Q4T_TEST(residency_capacity_empty_missing_and_nonuniform_layers) {
  ResidencyConfig config;
  Q4T_CHECK(q4t::runtime::ParseResidencyConfig(
      R"({"0":[],"1":[4,5],"2":[0,1,2,3,4,5]})", 4, 8, 4, &config));
  Q4T_CHECK(config.layer_slots == std::vector<int>({4, 2, 4, 4}));
  Q4T_CHECK(config.total_slots == 14);
  Q4T_CHECK(config.hot_lists[0].empty() && config.hot_lists[3].empty());
  Q4T_CHECK(config.hot_lists[2].size() == 6);
  return true;
}

Q4T_TEST(residency_capacity_rejects_ambiguous_or_invalid_lists) {
  for (const char* text :
       {"[]", R"({"0":null})", R"({"0":[1.5]})", R"({"0":[true]})",
        R"({"0":[8]})", R"({"0junk":[]})", R"({"4":[]})", R"({"0":[],"0":[]})",
        R"({"0":[],"00":[]})"}) {
    ResidencyConfig config;
    Q4T_CHECK(!q4t::runtime::ParseResidencyConfig(text, 4, 8, 4, &config));
  }
  return true;
}

Q4T_TEST(residency_capacity_file_errors_fail_before_loading) {
  char pattern[] = ".q4t-work/budget-config-XXXXXX";
  char* name = mkdtemp(pattern);
  Q4T_CHECK(name != nullptr);
  struct Cleanup {
    std::filesystem::path path;
    ~Cleanup() { std::filesystem::remove_all(path); }
  } cleanup{std::filesystem::path(name)};
  const auto file = cleanup.path / "hot.json";
  ResidencyConfig config;
  Q4T_CHECK(!q4t::runtime::LoadResidencyConfig(file, 4, 8, 4, &config));
  std::ofstream(file).close();
  Q4T_CHECK(!q4t::runtime::LoadResidencyConfig(file, 4, 8, 4, &config));
  {
    std::ofstream stream(file);
    stream << R"({"0":[]})";
  }
  Q4T_CHECK(q4t::runtime::LoadResidencyConfig(file, 4, 8, 4, &config));
  Q4T_CHECK(config.total_slots == 16);
  return true;
}
