#include "q4t/runtime/memory_budget.h"
#include "q4t/runtime/residency_config.h"
#include "q4t/test.h"

#include <cstdlib>
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

BudgetRequest MtpRequest(int len = 262144) {
  auto request = Request(len);
  request.mtp_workspace_bytes = 5000000000ULL;
  request.mtp_weights_bytes = 6000000000ULL;
  request.mtp_load_host_bytes = 3355443200ULL;
  return request;
}

BudgetModelParams MtpParams() {
  BudgetModelParams params;
  params.has_mtp = true;
  return params;
}
}  // namespace

Q4T_TEST(budget_agent_explicit_slots_are_atomic) {
  auto request = MtpRequest(208896);
  request.max_seq = 4;
  request.max_prefill = 4096;
  request.seq_capacities = {208896, 65536, 65536, 65536};
  const auto params = MtpParams();
  const auto roomy = ComputeMemoryBudget(params, request, kWeights, kRoom);
  Q4T_CHECK(roomy.feasible && !roomy.capped && roomy.max_seq == 4);
  const auto exact =
      ComputeMemoryBudget(params, request, kWeights, roomy.estimated_total);
  Q4T_CHECK(exact.feasible && exact.max_seq == 4 && !exact.capped);
  const auto short_one =
      ComputeMemoryBudget(params, request, kWeights, roomy.estimated_total - 1);
  Q4T_CHECK(!short_one.feasible && short_one.max_seq == 0);
  auto uniform_request = request;
  uniform_request.seq_capacities.clear();
  const auto uniform =
      ComputeMemoryBudget(params, uniform_request, kWeights, kRoom);
  const size_t saved_tokens = 3u * (208896 - 65536);
  Q4T_CHECK(uniform.state_pool - roomy.state_pool ==
            13u * (saved_tokens * 2560u - 4u * sizeof(size_t)));
  Q4T_CHECK(roomy.per_request < uniform.per_request);
  request.seq_capacities[1] = 65535;
  Q4T_CHECK(!ComputeMemoryBudget(params, request, kWeights, kRoom).feasible);
  return true;
}

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
    auto request = mtp ? MtpRequest(8192) : Request(8192, 2);
    const auto pinned = ComputeMemoryBudget(params, request, kWeights, kRoom);
    request.max_len = 0;
    const auto automatic =
        ComputeMemoryBudget(params, request, kWeights, pinned.estimated_total);
    Q4T_CHECK(automatic.feasible && automatic.max_len == 8192);
    Q4T_CHECK(automatic.max_seq == request.max_seq);
    Q4T_CHECK(automatic.estimated_total <= automatic.budget);
    request.max_len = 9216;
    const auto larger = ComputeMemoryBudget(params, request, kWeights, kRoom);
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

Q4T_TEST(budget_decode_rows_bound_explicit_and_automatic_sequences) {
  for (int rows : {1, 2, 4, 8}) {
    BudgetModelParams params;
    params.max_prefill = rows;
    for (int requested_prefill : {0, rows}) {
      auto request = Request(8192, rows);
      request.max_prefill = requested_prefill;
      const auto exact = ComputeMemoryBudget(params, request, kWeights, kRoom);
      Q4T_CHECK(exact.feasible && exact.max_seq == rows);
      ++request.max_seq;
      const auto over = ComputeMemoryBudget(params, request, kWeights, kRoom);
      Q4T_CHECK(!over.feasible && over.max_seq == 0 && over.max_len == 0);
      Q4T_CHECK(over.reason == "decode_batch_exceeds_forward_rows");
      request.max_seq = 0;
      for (int length : {0, 8192}) {
        request.max_len = length;
        const auto automatic =
            ComputeMemoryBudget(params, request, kWeights, kRoom);
        Q4T_CHECK(automatic.feasible && automatic.max_seq == rows);
        Q4T_CHECK(automatic.requested_max_seq == 0);
        Q4T_CHECK(automatic.max_prefill == rows);
      }
    }
  }
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
  const auto request = MtpRequest();
  BudgetModelParams params;
  params.has_mtp = false;
  const auto plain = ComputeMemoryBudget(params, request, kWeights, kRoom);
  params.has_mtp = true;
  const auto draft = ComputeMemoryBudget(params, request, kWeights, kRoom);
  Q4T_CHECK(plain.feasible && draft.feasible);
  Q4T_CHECK(plain.mtp_workspace == 0 && plain.per_request == 0);
  Q4T_CHECK(draft.mtp_workspace == request.mtp_workspace_bytes);
  Q4T_CHECK(draft.fixed - plain.fixed == 11353926264ULL);
  Q4T_CHECK(plain.mtp_weights == 0 && plain.mtp_scratch == 0);
  Q4T_CHECK(plain.mtp_checkpoints == 0 && plain.loading_host_peak == 0);
  // Full prompt trunk plus all draft-extend buffers, rolling g and host/IO
  // scratch. Independent weights and rollback scratch are persistent.
  Q4T_CHECK(draft.per_request == 9665896592ULL);
  // One extra full-attention pool plus an independent three-axis RoPE.
  Q4T_CHECK(draft.state_pool - plain.state_pool == 675282944ULL);
  Q4T_CHECK(draft.report.find("MTP allocation scope") != std::string::npos);
  Q4T_CHECK(plain.report.find("MTP allocation scope") == std::string::npos);
  const auto no_mtp = ComputeMemoryBudget({}, Request(), kWeights, kRoom);
  Q4T_CHECK(plain.estimated_total == no_mtp.estimated_total);
  return true;
}

Q4T_TEST(budget_mtp_accounts_for_loaded_and_lazy_allocations) {
  const auto budget =
      ComputeMemoryBudget(MtpParams(), MtpRequest(), kWeights, kRoom);
  Q4T_CHECK(budget.feasible);
  Q4T_CHECK(budget.mtp_weights == 6000000000ULL);
  // LoadMtp: sample[2560], trunk[10240], logits[248320], all BF16.
  Q4T_CHECK(budget.mtp_buffers == 522240);
  // ReserveScratch(k_max=4): 2,191,392 single-step bytes and 4,285,528
  // batched bytes. The batched allocations exist even at max_seq=1.
  Q4T_CHECK(budget.mtp_scratch == 6476920);
  // The 16-byte device offset charge moved from transient to persistent;
  // host gather_off remains charged. Runtime total is unchanged.
  Q4T_CHECK(budget.mtp_scratch + budget.mtp_forward_temp == 23291144);
  // Three checkpoints of 36 linear SSM/conv pools and one PLE conv pool.
  Q4T_CHECK(budget.mtp_checkpoints == 346927104);
  // Async counts/token lists are additional to their unused d_ws reserve;
  // include counts_h, attention host positions and decode metadata too.
  Q4T_CHECK(budget.mtp_forward_temp == 16814224);
  Q4T_CHECK(budget.loading_host_peak == 3355443200ULL);
  Q4T_CHECK(budget.estimated_total == budget.runtime_peak);
  Q4T_CHECK(budget.runtime_peak > budget.startup_peak);
  return true;
}

Q4T_TEST(budget_mtp_loading_and_request_peaks_are_separate) {
  auto request = MtpRequest(2048);
  const auto budget =
      ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
  Q4T_CHECK(budget.feasible);
  Q4T_CHECK(budget.loading_host_peak > budget.per_request);
  Q4T_CHECK(budget.startup_peak ==
            budget.fixed + budget.state_pool + budget.loading_host_peak);
  Q4T_CHECK(budget.estimated_total == budget.startup_peak);
  Q4T_CHECK(budget.estimated_total < budget.fixed + budget.state_pool +
                                         budget.loading_host_peak +
                                         budget.per_request);
  // Larger provided host staging must win over the known gate/up floor.
  request.mtp_load_host_bytes += 1000000;
  const auto bigger =
      ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
  Q4T_CHECK(bigger.fixed == budget.fixed);
  Q4T_CHECK(bigger.runtime_peak == budget.runtime_peak);
  Q4T_CHECK(bigger.startup_peak - budget.startup_peak == 1000000);
  return true;
}

Q4T_TEST(budget_mtp_loading_peak_exact_and_one_byte_short) {
  const auto request = MtpRequest(2048);
  const auto roomy = ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
  const auto exact = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                         roomy.estimated_total);
  Q4T_CHECK(exact.feasible && exact.max_len == 2048);
  const auto short_one = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                             roomy.estimated_total - 1);
  Q4T_CHECK(!short_one.feasible);
  Q4T_CHECK(short_one.max_len == 0 && short_one.max_seq == 0);
  return true;
}

Q4T_TEST(budget_mtp_runtime_peak_caps_length_without_dropping_charges) {
  const auto request = MtpRequest();
  const auto roomy = ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
  const auto exact = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                         roomy.estimated_total);
  Q4T_CHECK(exact.feasible && !exact.capped && exact.max_len == 262144);
  const auto short_one = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                             roomy.estimated_total - 1);
  Q4T_CHECK(short_one.feasible && short_one.capped);
  Q4T_CHECK(short_one.max_len < exact.max_len && short_one.max_seq == 1);
  Q4T_CHECK(short_one.estimated_total <= short_one.budget);
  Q4T_CHECK(short_one.mtp_weights == exact.mtp_weights);
  Q4T_CHECK(short_one.mtp_scratch == exact.mtp_scratch);
  Q4T_CHECK(short_one.mtp_checkpoints == exact.mtp_checkpoints);
  return true;
}

Q4T_TEST(budget_mtp_chunk_bound_is_monotone_across_full_last_chunk) {
  const auto before =
      ComputeMemoryBudget(MtpParams(), MtpRequest(8191), kWeights, kRoom);
  const auto full =
      ComputeMemoryBudget(MtpParams(), MtpRequest(8192), kWeights, kRoom);
  const auto after =
      ComputeMemoryBudget(MtpParams(), MtpRequest(8193), kWeights, kRoom);
  Q4T_CHECK(before.feasible && full.feasible && after.feasible);
  // One more trunk/host row plus one draft-extend row until chunk capacity.
  Q4T_CHECK(full.per_request - before.per_request == 542736);
  // At larger capacity the full chunk remains a possible request peak;
  // using max_len % chunk here would incorrectly erase most draft logits.
  Q4T_CHECK(after.per_request - full.per_request == 20488);
  Q4T_CHECK(before.estimated_total < full.estimated_total);
  Q4T_CHECK(full.estimated_total < after.estimated_total);
  return true;
}

Q4T_TEST(budget_mtp_requires_independent_weight_staging_and_workspace_inputs) {
  for (int missing = 0; missing < 3; ++missing) {
    auto request = MtpRequest();
    if (missing == 0) request.mtp_weights_bytes = 0;
    if (missing == 1) request.mtp_load_host_bytes = 0;
    if (missing == 2) request.mtp_workspace_bytes = 0;
    const auto budget =
        ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.reason == "missing_mtp_budget_inputs");
    Q4T_CHECK(budget.max_len == 0 && budget.max_seq == 0);
  }
  return true;
}

Q4T_TEST(budget_mtp_scope_rejects_unsupported_configurations) {
  for (int seq : {0, 2049, std::numeric_limits<int>::max()}) {
    auto request = MtpRequest();
    request.max_seq = seq;
    const auto budget =
        ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.reason == "unsupported_mtp_configuration");
  }
  for (int k : {0, 17, std::numeric_limits<int>::max()}) {
    auto request = MtpRequest();
    request.mtp_k = k;
    const auto budget =
        ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.reason == "unsupported_mtp_configuration");
  }
  for (int prefill : {1, 3, 8193, std::numeric_limits<int>::max()}) {
    auto request = MtpRequest();
    request.max_prefill = prefill;
    const auto budget =
        ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.reason == "unsupported_mtp_configuration");
  }
  return true;
}

Q4T_TEST(budget_mtp_overflowed_weight_or_loading_peak_fails_closed) {
  for (bool host : {false, true}) {
    auto request = MtpRequest();
    if (host) {
      request.mtp_load_host_bytes = std::numeric_limits<size_t>::max();
    } else {
      request.mtp_weights_bytes = std::numeric_limits<size_t>::max();
    }
    const auto budget =
        ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(!budget.feasible);
    Q4T_CHECK(budget.max_len == 0 && budget.max_seq == 0);
    Q4T_CHECK(budget.estimated_total == std::numeric_limits<size_t>::max());
  }
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

Q4T_TEST(residency_capacity_counts_distinct_hot_experts) {
  ResidencyConfig config;
  Q4T_CHECK(q4t::runtime::ParseResidencyConfig(
      R"({"0":[0,0,1],"1":[3,3,3],"2":[4,4,2,2,1,0]})", 4, 8, 4, &config));
  Q4T_CHECK(config.hot_lists[0] == std::vector<int>({0, 1}));
  Q4T_CHECK(config.hot_lists[1] == std::vector<int>({3}));
  Q4T_CHECK(config.hot_lists[2] == std::vector<int>({4, 2, 1, 0}));
  Q4T_CHECK(config.layer_slots == std::vector<int>({2, 1, 4, 4}));
  Q4T_CHECK(config.total_slots == 11);
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

Q4T_TEST(budget_mtp_concurrent_held_trunks_and_pooled_scratch) {
  auto one = MtpRequest(16384);
  const auto baseline = ComputeMemoryBudget(MtpParams(), one, kWeights, kRoom);
  for (int slots : {2, 4, 16, 32}) {
    auto request = one;
    request.max_seq = slots;
    const auto budget = ComputeMemoryBudget(MtpParams(), request, kWeights, kRoom);
    Q4T_CHECK(budget.feasible && budget.max_seq == slots && !budget.capped);
    Q4T_CHECK(budget.mtp_scratch == 2191392u + 4285528u * slots);
    Q4T_CHECK(budget.mtp_checkpoints == baseline.mtp_checkpoints * slots);
    Q4T_CHECK(budget.state_pool == baseline.state_pool * slots);
    // Simultaneous prompt trunks/g/host history; only one serialized extend.
    const size_t held = size_t(16384) * (10240 * 2 + 8) + 10240 * 2;
    Q4T_CHECK(budget.per_request == baseline.per_request + held * (slots - 1) +
                  budget.mtp_forward_temp - baseline.mtp_forward_temp);
    const auto exact = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                           budget.estimated_total);
    Q4T_CHECK(exact.feasible && !exact.capped && exact.max_seq == slots);
    const auto short_one = ComputeMemoryBudget(MtpParams(), request, kWeights,
                                               budget.estimated_total - 1);
    Q4T_CHECK(short_one.feasible && short_one.capped &&
                  short_one.max_seq == slots - 1 &&
                  short_one.estimated_total <= short_one.budget);
  }
  one.max_seq = 4;
  one.max_prefill = 15;
  Q4T_CHECK(!ComputeMemoryBudget(MtpParams(), one, kWeights, kRoom).feasible);
  return true;
}
