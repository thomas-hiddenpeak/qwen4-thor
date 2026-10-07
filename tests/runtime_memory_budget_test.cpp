#include "q4t/runtime/memory_budget.h"
#include "q4t/test.h"

#include <limits>

namespace {
using q4t::runtime::BudgetModelParams;
using q4t::runtime::BudgetRequest;
using q4t::runtime::ComputeMemoryBudget;

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

Q4T_TEST(budget_minimum_boundaries_fail_without_capacity_fallback) {
  for (int length : {1, 1024, 2048}) {
    const auto request = Request(length);
    const auto roomy = ComputeMemoryBudget({}, request, kWeights, kRoom);
    const auto exact =
        ComputeMemoryBudget({}, request, kWeights, roomy.estimated_total);
    Q4T_CHECK(exact.feasible && exact.max_len == length);
    const auto short_one =
        ComputeMemoryBudget({}, request, kWeights, roomy.estimated_total - 1);
    Q4T_CHECK(!short_one.feasible);
    Q4T_CHECK(short_one.max_len == 0 && short_one.max_seq == 0);
  }
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

Q4T_TEST(budget_auto_minimum_preserves_requested_sequence_count) {
  const auto minimum =
      ComputeMemoryBudget({}, Request(1024, 2), kWeights, kRoom);
  const auto exact =
      ComputeMemoryBudget({}, Request(0, 2), kWeights, minimum.estimated_total);
  Q4T_CHECK(exact.feasible && exact.max_len == 1024 && exact.max_seq == 2);
  const auto short_one = ComputeMemoryBudget({}, Request(0, 2), kWeights,
                                             minimum.estimated_total - 1);
  Q4T_CHECK(!short_one.feasible);
  Q4T_CHECK(short_one.max_len == 0 && short_one.max_seq == 0);
  return true;
}

Q4T_TEST(budget_default_fraction_and_prefill_use_effective_parameters) {
  BudgetModelParams params;
  params.max_prefill = 2048;
  auto request = Request(8192);
  request.mem_fraction = 0.0;
  request.max_prefill = 0;
  const auto budget = ComputeMemoryBudget(params, request, kWeights, kRoom);
  Q4T_CHECK(budget.feasible && budget.budget == 115200000000ULL);
  Q4T_CHECK(budget.requested_max_prefill == 0 && budget.max_prefill == 2048);
  request.max_prefill = 2048;
  const auto explicit_size =
      ComputeMemoryBudget(params, request, kWeights, kRoom);
  Q4T_CHECK(budget.fixed == explicit_size.fixed);
  return true;
}

Q4T_TEST(budget_text_baseline_tracks_resident_allocations) {
  const auto budget =
      ComputeMemoryBudget({}, Request(208896), 84000000000ULL, kRoom);
  Q4T_CHECK(budget.feasible && !budget.capped);
  Q4T_CHECK(budget.max_len == 208896 && budget.max_seq == 1);
  // Main allocations: 12 KV/indexer pools, 36 SSM/conv pools, one PLE
  // conv, three-axis RoPE, two selected-logit rows, and three sequence ints.
  Q4T_CHECK(budget.state_pool == 6546454540ULL);
  // 8192 rows: five int arrays, embedding, two trunks and 2560 PLE values;
  // plus the terminal ragged sequence offset.
  Q4T_CHECK(budget.forward_buffers == 419594244ULL);
  Q4T_CHECK(budget.ple_working == 76571648ULL);
  Q4T_CHECK(budget.per_request == 0 && budget.mtp_workspace == 0);
  Q4T_CHECK(budget.weights == 84000000000ULL);
  Q4T_CHECK(budget.estimated_total == budget.fixed + budget.state_pool);
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
  // One extra full-attention pool plus an independent three-axis RoPE.
  Q4T_CHECK(draft.state_pool - plain.state_pool == 675282944ULL);
  Q4T_CHECK(draft.report.find("MTP subset only") != std::string::npos);
  Q4T_CHECK(plain.report.find("MTP subset only") == std::string::npos);
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
  for (int field = 0; field < 3; ++field) {
    auto request = Request();
    if (field == 0) request.max_len = -1;
    if (field == 1) request.max_seq = -1;
    if (field == 2) request.max_prefill = -1;
    const auto bad = ComputeMemoryBudget({}, request, kWeights, kRoom);
    Q4T_CHECK(!bad.feasible && bad.reason == "invalid_budget_request");
  }
  BudgetModelParams params;
  params.hs = std::numeric_limits<int>::max();
  const auto invalid_shape =
      ComputeMemoryBudget(params, Request(), kWeights, kRoom);
  Q4T_CHECK(!invalid_shape.feasible);
  Q4T_CHECK(invalid_shape.reason == "invalid_budget_request");
  params = {};
  params.nv = std::numeric_limits<int>::max();
  params.kd = std::numeric_limits<int>::max();
  params.vd = std::numeric_limits<int>::max();
  const auto overflow = ComputeMemoryBudget(params, Request(), kWeights, kRoom);
  Q4T_CHECK(!overflow.feasible);
  Q4T_CHECK(overflow.max_len == 0 && overflow.max_seq == 0);
  return true;
}
